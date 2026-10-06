#pragma once

/// What a session needs from an engine adapter, and what an adapter may offer besides (Tier 2).
///
/// **A capability is data on the value, not a method that might be missing.** The reference asks
/// an adapter whether it will answer a call by looking for the member; C++ cannot ask that, and
/// should not try. So `Engine::capabilities()` returns which of the optional interfaces this adapter
/// implements, as pointers - an empty one means "this engine does not take part", and that is
/// reported by name wherever it matters: an engine that cannot keep the bookkeeping has no rollback
/// protection, one that cannot be migrated refuses a migration, one without counts refuses a count.
///
/// Every interface here moves rows only between the client's process and the client's engine. Not
/// one of them has a parameter through which a value could reach the control plane.

#include <cstdint>
#include <functional>
#include <map>
#include <optional>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

#include "sde/physical.hpp"
#include "sde/query.hpp"
#include "sde/value.hpp"

namespace sde {

class WriteFence;

/// Each entity's key, in declared order.
using Keys = std::map<std::string, std::vector<std::string>>;

/// One way an existing table differs from the physical design its layout declares. Reported, never
/// refused at run time: a table with another sort key stores and returns the same rows.
struct PhysicalFinding {
  std::string table;
  std::string aspect;
  std::string declared;
  std::string found;

  /// `<table>: <aspect> is <found> and the map declares <declared>`.
  [[nodiscard]] std::string to_string() const;

  friend bool operator==(const PhysicalFinding&, const PhysicalFinding&) = default;
};

/// A bounded page of rows, in the plan's order, at most `plan.limit + 1` of them: the one beyond
/// the limit says that another page exists.
class Queryable {
 public:
  virtual ~Queryable() = default;
  virtual std::vector<Row> select_rows(const std::string& table, const ReadPlan& plan) = 0;
};

/// An exact count of the rows a plan's filters select.
class Countable {
 public:
  virtual ~Countable() = default;
  virtual std::uint64_t count_rows(const std::string& table, const ReadPlan& plan) = 0;
};

/// An exact numeric summary of one column over the rows a plan's filters select, as the engine's
/// text (`numeric_summary` decodes it).
class Summarizable {
 public:
  virtual ~Summarizable() = default;
  virtual SummaryRecord summarize_rows(const std::string& table, const ReadPlan& plan,
                                       const ReadColumn& column) = 0;
};

/// One native insert of a bounded batch: no conflict suppression, no splitting, no retry.
class BulkWritable {
 public:
  virtual ~BulkWritable() = default;
  virtual void insert_many(const std::string& table, const std::vector<Row>& rows) = 0;
};

/// The forward-only bookkeeping of signed maps (format contract section 7): append-only, and the
/// watermark is the highest version recorded.
class WatermarkStore {
 public:
  virtual ~WatermarkStore() = default;
  virtual std::optional<std::int64_t> map_watermark() = 0;
  virtual void record_map_version(std::int64_t version, const std::string& model_version) = 0;
};

/// What a migration needs of an engine, source or target: keyset scans, the row at a position, an
/// idempotent copy, a count and the backfill marker - a row count, never a key.
class Migratable {
 public:
  virtual ~Migratable() = default;
  /// Rows in `order`, strictly after `after` and at most `upto`, at most `limit` of them; every
  /// bound has one value per ordering column.
  virtual std::vector<Row> key_range(const std::string& table, const std::vector<std::string>& order,
                                     const std::optional<std::vector<Value>>& after,
                                     const std::optional<std::vector<Value>>& upto,
                                     std::optional<std::size_t> limit) = 0;
  /// The key of the row at a one-based position in `order`; empty past the end.
  virtual std::optional<std::vector<Value>> nth_key(const std::string& table,
                                                    const std::vector<std::string>& order,
                                                    std::int64_t position) = 0;
  /// Inserts rows, absorbing a row that is already there whole: a recopied chunk.
  virtual void copy_in(const std::string& table, const std::vector<Row>& rows) = 0;
  virtual std::uint64_t count(const std::string& table) = 0;
  virtual std::int64_t backfill_marker(const std::string& materialization,
                                       const std::string& entity) = 0;
  virtual void record_backfill_marker(const std::string& materialization,
                                      const std::string& entity, std::int64_t rows) = 0;
};

/// Native write generations (format contract sections 7c and 7d): a fence per table.
class Fencable {
 public:
  virtual ~Fencable() = default;
  virtual WriteFence write_fence(const std::string& table, const std::string& project_id) = 0;
};

/// A check of the tables against a layout that creates nothing: how a session started past its
/// first release learns that a table differs from the declared design. An engine with write
/// generations must offer it; one without may.
class SchemaValidator {
 public:
  virtual ~SchemaValidator() = default;
  virtual std::vector<PhysicalFinding> validate_schema(const PhysicalLayout& layout,
                                                       const Keys& keys) = 0;
};

/// Each named table's total and secondary-index bytes, from the engine's catalogue. A table it does
/// not report is absent from the answer.
class StorageMeasurable {
 public:
  virtual ~StorageMeasurable() = default;
  virtual std::map<std::string, std::pair<std::int64_t, std::int64_t>> storage_sizes(
      const std::vector<std::string>& tables) = 0;
};

/// Which optional interfaces an adapter implements. Null means it does not; a count or a summary it
/// refuses for a reason of its own says so in `count_refusal` or `summary_refusal`.
struct Capabilities {
  Queryable* query = nullptr;
  Countable* count = nullptr;
  std::string count_refusal;
  Summarizable* summary = nullptr;
  std::string summary_refusal;
  BulkWritable* bulk = nullptr;
  WatermarkStore* watermark = nullptr;
  Migratable* migration = nullptr;
  Fencable* fences = nullptr;
  SchemaValidator* schema = nullptr;
  StorageMeasurable* storage = nullptr;
};

/// One engine, as a session uses it. Not shared between threads unless the adapter says it may be.
class Engine {
 public:
  virtual ~Engine() = default;

  /// `postgres`, `clickhouse`, `orderbook`, or a dialect this library holds no facts about.
  [[nodiscard]] virtual std::string_view dialect() const noexcept = 0;
  /// Creates what is missing, and returns how existing tables differ from the declared design.
  virtual std::vector<PhysicalFinding> ensure_schema(const PhysicalLayout& layout,
                                                     const Keys& keys) = 0;
  virtual void insert(const std::string& table, const Row& values) = 0;
  /// The row with exactly this key, or nothing.
  virtual std::optional<Row> get(const std::string& table, const Row& key) = 0;
  /// Runs `body` in one transaction of this engine: committed when it returns, rolled back when it
  /// throws, and the exception passed on. A callback, so a transaction cannot be left open.
  virtual void transaction(const std::function<void()>& body) = 0;
  /// The optional interfaces this adapter implements.
  [[nodiscard]] virtual Capabilities capabilities() noexcept { return {}; }
};

}  // namespace sde
