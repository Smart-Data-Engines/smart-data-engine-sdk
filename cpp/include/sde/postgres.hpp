#pragma once

/// The PostgreSQL adapter, dialect `postgres`, over libpq. A thin one: it executes decisions and
/// makes none. It links as `sde::postgres`, so an application that places nothing in PostgreSQL
/// carries no libpq, and the core of this library links no network library at all.
///
/// Two rules run through it, both the reference's. **A failed write is reported, never worked
/// around**: no retry, no rerouting, no reconnect - a write that did not happen is not this
/// library's internal problem, and reporting success for it would be the worst thing it could do.
/// **Identifiers are always quoted**, by the one function the DDL quotes with.
///
/// One thread uses an engine at a time, like the connection it holds: a second is refused
/// (`ResourceBusy`) rather than raced, and so is any other thread while one is inside a
/// transaction. After a `fork` the inherited connection is refused (`ResourceClosed`).

#include <atomic>
#include <cstdint>
#include <functional>
#include <map>
#include <memory>
#include <optional>
#include <string>
#include <string_view>
#include <thread>
#include <utility>
#include <vector>

#include "sde/engine.hpp"
#include "sde/log.hpp"
#include "sde/write_fence.hpp"

namespace sde {

namespace detail::postgres {
class Connection;
class Fences;
}  // namespace detail::postgres

struct PostgresOptions {
  /// The events of the closed vocabulary this adapter writes (`sde.schema.applied`,
  /// `sde.schema.extra_columns`, `sde.schema.text_collation`, `sde.write.failed`); none of them
  /// carries a value of the client's.
  LogSink log;
};

class PostgresEngine final : public Engine,
                             public Queryable,
                             public Countable,
                             public Summarizable,
                             public BulkWritable,
                             public WatermarkStore,
                             public Migratable,
                             public Fencable,
                             public SchemaValidator,
                             public StorageMeasurable {
 public:
  /// Nothing is opened until `connect`.
  explicit PostgresEngine(std::string dsn, PostgresOptions options = {});
  ~PostgresEngine() override;
  PostgresEngine(const PostgresEngine&) = delete;
  PostgresEngine& operator=(const PostgresEngine&) = delete;

  /// Opens the connection, bounded by the DSN's `connect_timeout` or else ten seconds: without a
  /// bound, a host that accepts the connection and never answers hangs the call for ever. Refused
  /// (`EngineError`) after a transaction whose completion was uncertain, until `close`.
  void connect();
  /// Closes the connection; nothing happens when none is open.
  void close() noexcept;
  [[nodiscard]] bool connected() const noexcept;

  // Engine
  [[nodiscard]] std::string_view dialect() const noexcept override { return "postgres"; }
  /// Creates what is missing and changes nothing that exists: tables, then the indexes of the tables
  /// whose key is the declared one. Refuses (`EngineError`) a table missing a column or holding one
  /// as another type; returns how keys and indexes differ from the design.
  std::vector<PhysicalFinding> ensure_schema(const PhysicalLayout& layout,
                                             const Keys& keys) override;
  void insert(const std::string& table, const Row& values) override;
  std::optional<Row> get(const std::string& table, const Row& key) override;
  /// One transaction of this engine, nested ones as savepoints. A transaction the server aborted
  /// is refused rather than reported as committed.
  void transaction(const std::function<void()>& body) override;
  [[nodiscard]] Capabilities capabilities() noexcept override;

  // Logical reads
  std::vector<Row> select_rows(const std::string& table, const ReadPlan& plan) override;
  std::uint64_t count_rows(const std::string& table, const ReadPlan& plan) override;
  SummaryRecord summarize_rows(const std::string& table, const ReadPlan& plan,
                               const ReadColumn& column) override;

  // Batches: one INSERT, in which a key already present is an error.
  void insert_many(const std::string& table, const std::vector<Row>& rows) override;

  // The forward-only bookkeeping, appended to and never updated.
  std::optional<std::int64_t> map_watermark() override;
  void record_map_version(std::int64_t version, const std::string& model_version) override;

  // Migration
  std::vector<Row> key_range(const std::string& table, const std::vector<std::string>& order,
                             const std::optional<std::vector<Value>>& after,
                             const std::optional<std::vector<Value>>& upto,
                             std::optional<std::size_t> limit) override;
  std::optional<std::vector<Value>> nth_key(const std::string& table,
                                            const std::vector<std::string>& order,
                                            std::int64_t position) override;
  void copy_in(const std::string& table, const std::vector<Row>& rows) override;
  std::uint64_t count(const std::string& table) override;
  std::int64_t backfill_marker(const std::string& materialization,
                               const std::string& entity) override;
  void record_backfill_marker(const std::string& materialization, const std::string& entity,
                              std::int64_t rows) override;

  /// Native write barriers: CHECK constraints and table locks. DDL - use a connection kept apart
  /// from the application's traffic, as the reference requires.
  WriteFence write_fence(const std::string& table, const std::string& project_id) override;
  std::vector<PhysicalFinding> validate_schema(const PhysicalLayout& layout,
                                               const Keys& keys) override;
  /// Each table's bytes and its indexes' bytes but the primary key's, from the catalogue; a table
  /// the connection does not see is absent from the answer, not zero.
  std::map<std::string, std::pair<std::int64_t, std::int64_t>> storage_sizes(
      const std::vector<std::string>& tables) override;

 private:
  friend class detail::postgres::Fences;
  class Use;

  [[nodiscard]] detail::postgres::Connection& cx();
  [[nodiscard]] std::string explain(const std::string& message) const;
  std::vector<Row> rows_of(const std::string& sql,
                           const std::vector<std::optional<std::string>>& parameters,
                           const std::string& refused);
  void execute_schema(const std::vector<std::string>& statements);
  void verify_schema(const PhysicalLayout& layout);
  [[nodiscard]] bool same_type(const std::string& declared, const std::string& reported);
  std::vector<PhysicalFinding> physical_findings(const PhysicalLayout& layout, const Keys& keys);

  std::string dsn_;
  PostgresOptions options_;
  std::unique_ptr<detail::postgres::Connection> connection_;
  std::unique_ptr<detail::postgres::Fences> fences_;
  bool unusable_ = false;
  int transactions_ = 0;
  int savepoints_ = 0;
  std::atomic<std::thread::id> owner_{};
  int depth_ = 0;
  long process_ = 0;
};

}  // namespace sde
