#pragma once

/// A session: a model, a placement map and the engines it points at, tied together (Tier 2).
///
/// It routes each operation to the engine the map names and refuses the operations that cannot be
/// answered. The one guarantee no part delivers alone is enforced here: **a transaction is one
/// group's, so one engine's, and nothing wider.** A client needing two groups to commit together
/// declares the atomicity, and the planner colocates them; the error says so when the transaction
/// is opened, because a failure at commit would be a production incident.
///
/// **Opening a session checks what a map cannot check alone**, before anything is routed: every
/// engine the map names is supplied; a fan-out does not truncate a value between two dialects; the
/// write generations of a contract-4 map are the ones the engines enforce; and a signed map is not
/// older than one these engines have already seen. An unsigned map costs nothing - no table, no
/// query.
///
/// A session is one unit of work, like a connection: one thread uses it at a time, and a second
/// thread entering it while it is in use is refused (`ResourceBusy`) rather than raced. The model,
/// the map and the engines must outlive it; it copies none of them.

#include <cstdint>
#include <functional>
#include <map>
#include <optional>
#include <set>
#include <string>
#include <string_view>
#include <thread>
#include <atomic>
#include <tuple>
#include <vector>

#include "sde/engine.hpp"
#include "sde/hashing.hpp"
#include "sde/log.hpp"
#include "sde/model.hpp"
#include "sde/placement.hpp"
#include "sde/query.hpp"
#include "sde/telemetry.hpp"
#include "sde/watermark.hpp"

namespace sde {

struct SessionOptions {
  /// Telemetry, off unless a recorder is handed in: measurement begins at a visible line in the
  /// client's code. Not owned.
  Recorder* recorder = nullptr;
  /// The name map of a hashed model (section 2a): the application keeps saying its own names, and
  /// everything past the session speaks digests. Not owned.
  const NameMap* names = nullptr;
  /// The project of local enrollment configuration, never learned from a document. Required by a
  /// generation-bearing map (contract 4 and later).
  std::optional<std::string> project_id;
  LogSink log;
};

/// A bounded page, read where the map routes it.
struct ScanOptions {
  std::optional<Row> where;
  std::optional<Range> bounds;
  std::optional<std::string> order_by;
  bool descending = false;
  std::optional<Row> after;
  PageLimit limit;
  /// Read the source, not a copy that may be behind.
  bool fresh = false;
};

struct CountOptions {
  std::optional<Row> where;
  std::optional<Range> bounds;
  bool fresh = false;
};

struct SummaryOptions {
  std::optional<Row> where;
  std::optional<Range> bounds;
  int mean_scale = 6;
  bool fresh = false;
};

/// One group's size on its source materialisation, from its engine's catalogue.
struct StorageSize {
  std::string group;
  std::string materialization;
  std::string engine;
  std::int64_t total_bytes = 0;
  std::int64_t secondary_index_bytes = 0;
};

struct StorageMeasurement {
  std::vector<StorageSize> sizes;  ///< by group
  /// Group to why its size is unknown: `unsupported`, `refused`, `failed` or `missing_table`.
  std::map<std::string, std::string> unavailable;
};

class Session {
 public:
  /// Opens a session, refusing what this set of engines cannot serve (see the file's comment).
  Session(const Model& model, const PlacementMap& placement, std::map<std::string, Engine*> engines,
          SessionOptions options = {});
  ~Session();
  Session(const Session&) = delete;
  Session& operator=(const Session&) = delete;

  [[nodiscard]] const Model& model() const noexcept { return model_; }
  [[nodiscard]] const PlacementMap& placement() const noexcept { return placement_; }
  [[nodiscard]] const std::map<std::string, Engine*>& engines() const noexcept { return engines_; }
  [[nodiscard]] const std::optional<std::string>& project_id() const noexcept { return project_id_; }
  /// How existing tables differ from the declared physical design: reported, never refused.
  [[nodiscard]] const std::vector<PhysicalFinding>& physical() const noexcept { return physical_; }
  /// Whether an older map could be loaded over this one, and why.
  [[nodiscard]] const WatermarkCheck& rollback_protection() const noexcept { return forward_only_; }

  /// The colocation group of an entity, named as the application names it.
  [[nodiscard]] const Group& group_of(std::string_view entity) const;

  /// Creates what each engine is missing for the groups placed in it. From contract 4 the schema is
  /// provisioned outside the application and this only checks it.
  void ensure_schema();

  /// Writes one row where the map routes the entity's writes, then to every copy the map fans
  /// writes out to - after the transaction commits, when inside one. A copy's failure does not fail
  /// the write: the source is authoritative, and verifying the copy is the gate.
  void save(std::string_view entity, const Row& values);
  /// One bounded batch, checked whole before any engine is called; no splitting, no retry.
  void save_many(std::string_view entity, const std::vector<Row>& rows);
  /// The row with exactly this key, or nothing.
  std::optional<Row> get(std::string_view entity, const Row& key, bool fresh = false);
  ScanPage scan(std::string_view entity, const ScanOptions& options = {});
  std::uint64_t count(std::string_view entity, const CountOptions& options = {});
  NumericSummary summarize(std::string_view entity, std::string_view field,
                           const SummaryOptions& options = {});
  /// Each group's size on its source, from the engines' catalogues. Never refused because an engine
  /// could not answer: that group's size stays unknown, and says why.
  StorageMeasurement measure_storage();

  /// Runs `body` in one transaction of the engine holding the entities' group. Entities of two
  /// groups are refused before anything is opened (`ModelPlanningError`). Empty means the whole
  /// model, which must then be one group.
  void transaction(const std::vector<std::string>& entities, const std::function<void()>& body);
  void transaction(const std::function<void()>& body) { transaction({}, body); }

 private:
  friend struct BackfillProgress backfill(Session& session, const std::string& group,
                                          const struct BackfillOptions& options);
  friend struct VerifyReport verify(Session& session, const std::string& group,
                                    const struct VerifyOptions& options);

  struct Admission {
    std::set<std::string> declared;
    std::vector<std::string> required;  ///< code point order: the order a refusal names
  };
  struct Deferred {
    std::string engine;
    std::string table;
    std::string group;
    std::string materialization;
    std::int64_t queued_ns;
    std::vector<Row> rows;
    bool batch;
  };
  class Use;

  // The hashing boundary: names in, names out.
  [[nodiscard]] std::string target_of(std::string_view entity) const;
  [[nodiscard]] Row fields_in(std::string_view entity, const Row& values) const;
  [[nodiscard]] Row fields_out(std::string_view entity, Row row) const;
  [[nodiscard]] std::string client_name(std::string_view entity, const std::string& field) const;

  /// `sde.schema.physical_mismatch`, when the tables differ from the design: on opening and on
  /// `ensure_schema`, as the reference reports it.
  void report_physical() const;
  void admit(std::string_view entity, const std::string& target, const Row& values) const;
  [[nodiscard]] const OperationShape& shape(const std::string& entity, std::string_view kind,
                                            const std::vector<std::string>& fields = {}) const;
  [[nodiscard]] std::pair<Engine*, const Materialization*> target(const OperationShape& shape,
                                                                  bool fresh) const;
  /// A point read from where it goes on: read, record, hand back the client's row.
  std::optional<Row> read_one(const OperationShape& read, std::string_view entity, Engine& engine,
                              const std::string& table, const Row& given);
  void fan_out(const std::string& entity, const std::string& group, const std::vector<Row>& rows,
               bool batch);
  void replay_one(const Deferred& write);
  void observe(const OperationShape& shape, std::int64_t started, std::uint64_t rows, bool failed,
               const ReadPlan* plan = nullptr);
  [[nodiscard]] std::pair<std::string, ReadPlan> prepare_read(
      std::string_view entity, const std::optional<Row>& where, const std::optional<Range>& bounds,
      const std::optional<std::string>& order_by, bool descending, const std::optional<Row>& after,
      PageLimit limit, bool paginate) const;
  [[nodiscard]] std::int64_t now() const;

  const Model& model_;
  const PlacementMap& placement_;
  std::map<std::string, Engine*> engines_;
  Recorder* recorder_;
  const NameMap* names_;
  std::optional<std::string> project_id_;
  LogSink log_;
  std::vector<Group> groups_;
  std::map<std::tuple<std::string, std::string, std::vector<std::string>>, const OperationShape*>
      shapes_;
  std::map<std::string, Admission> admission_;
  /// The point read of each entity of a model whose names nothing translates, decided once: its key
  /// in code point order, its shape, and where it goes - routed, or to the source when fresh or in
  /// a write transaction - as `resolve` decides it. `get` takes it when the key given is exactly
  /// that one.
  struct PointRead {
    struct Place {
      Engine* engine = nullptr;
      const std::string* table = nullptr;
    };
    std::vector<std::string> key;
    const OperationShape* shape = nullptr;
    Place routed;
    Place source;
  };
  std::map<std::string, PointRead, std::less<>> point_reads_;
  std::map<std::string, std::map<std::string, std::string>> input_fields_;    ///< client entity
  std::map<std::string, std::map<std::string, std::string>> reverse_fields_;  ///< hashed entity
  std::vector<std::string> declared_;  ///< the client's entity names, with hashing on
  std::vector<PhysicalFinding> physical_;
  WatermarkCheck forward_only_;
  bool in_write_transaction_ = false;
  std::vector<Deferred> deferred_;
  std::atomic<std::thread::id> owner_{};
  int depth_ = 0;
};

}  // namespace sde
