#pragma once

/// An in-memory engine, for the `migration/` conformance vectors and for anybody's adapter tests.
///
/// **Why this is in the library rather than in a test file.** The `migration/` vectors pin
/// behaviour that only happens against an engine - the order of the calls a backfill makes, the
/// arithmetic of the resume marker - so every implementation needs an engine to run them against.
/// A runner that writes its own is a runner whose fixture can be the thing that differs, and then a
/// red vector says "one of two tables disagreed" rather than "one of two libraries disagreed".
///
/// **What it is not.** It implements nothing of the format contract. It stores rows in lists and
/// answers questions about them; every rule the vectors check lives in the session, the migration
/// and the watermark. The one property it must get right is a keyset scan, and the vectors pin the
/// calls as well as the results.

#include <cstdint>
#include <functional>
#include <map>
#include <memory>
#include <optional>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

#include "sde/engine.hpp"
#include "sde/json.hpp"
#include "sde/value.hpp"
#include "sde/write_fence.hpp"

namespace sde::testing {

/// The calls a set of engines received, in one sequence, each naming its engine: a guarantee across
/// two engines - a row reaches the source before anything is tried against the copy - needs one.
class Recorded {
 public:
  /// Appends `{"engine": engine, "call": method, ...arguments}`.
  void note(std::string_view engine, std::string_view method, Json arguments = Json::object());
  [[nodiscard]] const std::vector<Json>& calls() const noexcept { return calls_; }
  /// The calls as one JSON array, as `calls.json` holds them.
  [[nodiscard]] Json as_json() const;

 private:
  std::vector<Json> calls_;
};

/// The vectors' value encoding: JSON scalars as the obvious value - an integer as `int64`, other
/// numbers as `double`, text as `string`, `null` as `Null` - and arrays and objects as JSON.
[[nodiscard]] Value value_from_json(const Json& value);
/// The other way, for comparing what an engine holds with a vector: typed values as their text.
[[nodiscard]] Json value_to_json(const Value& value);
[[nodiscard]] Row row_from_json(const Json& object);
[[nodiscard]] Json row_to_json(const Row& row);

/// A fence backend that keeps a table's constraints in memory and records every DDL call as an
/// array - `["add", table, name, expression]`, `["drain", table, project, hold]` and so on.
class MemoryFences final : public FenceBackend {
 public:
  std::string identity = "table-identity";
  ColumnState column = ColumnState::absent;
  std::map<std::string, std::string> constraints;
  std::vector<Json> calls;
  /// Throws after this many calls, as a lost response after the DDL took effect does.
  std::optional<std::size_t> fail_after;
  /// Told about every call as it is recorded.
  std::function<void(const Json& call)> on_call;

  FenceMetadata metadata(const std::string& table) override;
  void add_column(const std::string& table) override;
  void add_constraint(const std::string& table, const std::string& name,
                      const std::string& expression) override;
  void drop_constraint(const std::string& table, const std::string& name) override;
  void drain(const std::string& table, const std::string& project_id,
             const std::string& hold) override;
  void restore(const std::string& table, const std::string& project_id,
               const std::string& hold) override;

 private:
  void done(Json call);
};

struct MemoryEngineOptions {
  std::string dialect = "postgres";
  std::string name = "engine";
  /// One journal for the whole engine set; a fresh one when none is given.
  std::shared_ptr<Recorded> journal;
  std::map<std::string, std::vector<Row>> tables;
  /// Which optional interfaces the engine offers. An engine built with one off does not offer it
  /// at all, which is what a real adapter without the capability looks like.
  bool can_keep_bookkeeping = true;
  bool can_migrate = true;
  bool can_bulk_write = true;
  std::optional<std::int64_t> watermark;
  /// `(materialization, entity)` to the rows a backfill has copied.
  std::map<std::pair<std::string, std::string>, std::int64_t> markers;
  /// How many of the next inserts into each table must fail.
  std::map<std::string, std::int64_t> fail_inserts;
};

/// One dialect, a set of named tables, and the optional interfaces an adapter may offer.
class MemoryEngine final : public Engine,
                           public WatermarkStore,
                           public BulkWritable,
                           public Migratable,
                           public Fencable,
                           public SchemaValidator {
 public:
  explicit MemoryEngine(MemoryEngineOptions options = {});

  /// The rows of each table, in insertion order.
  std::map<std::string, std::vector<Row>> tables;

  [[nodiscard]] const std::string& name() const noexcept { return name_; }
  [[nodiscard]] Recorded& recorded() noexcept { return *journal_; }
  [[nodiscard]] const std::shared_ptr<Recorded>& journal() const noexcept { return journal_; }

  /// Gives the engine write fences, one backend per table, and a schema check that finds nothing:
  /// what a generation-bearing vector configures. Without it the engine offers neither.
  void bind_fences(std::map<std::string, std::shared_ptr<MemoryFences>> backends);

  // Engine
  [[nodiscard]] std::string_view dialect() const noexcept override { return dialect_; }
  std::vector<PhysicalFinding> ensure_schema(const PhysicalLayout& layout, const Keys& keys) override;
  void insert(const std::string& table, const Row& values) override;
  std::optional<Row> get(const std::string& table, const Row& key) override;
  void transaction(const std::function<void()>& body) override;
  [[nodiscard]] Capabilities capabilities() noexcept override;

  // WatermarkStore
  std::optional<std::int64_t> map_watermark() override;
  void record_map_version(std::int64_t version, const std::string& model_version) override;

  // BulkWritable
  void insert_many(const std::string& table, const std::vector<Row>& rows) override;

  // Migratable
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

  // Fencable and SchemaValidator, once bound
  WriteFence write_fence(const std::string& table, const std::string& project_id) override;
  std::vector<PhysicalFinding> validate_schema(const PhysicalLayout& layout,
                                               const Keys& keys) override;

 private:
  bool fail_insert(const std::string& table);

  std::string dialect_;
  std::string name_;
  std::shared_ptr<Recorded> journal_;
  bool can_keep_bookkeeping_;
  bool can_migrate_;
  bool can_bulk_write_;
  std::vector<std::int64_t> watermarks_;
  std::map<std::pair<std::string, std::string>, std::vector<std::int64_t>> markers_;
  std::map<std::string, std::int64_t> fail_inserts_;
  std::map<std::string, std::shared_ptr<MemoryFences>> fences_;
};

/// The engine set a `migration/` case describes (`engines.json`), sharing one journal. Read here
/// rather than in each runner, so the document a vector carries is read by one piece of code.
[[nodiscard]] std::map<std::string, std::unique_ptr<MemoryEngine>> engines_from(
    const Json& spec, std::shared_ptr<Recorded> journal = nullptr);

}  // namespace sde::testing
