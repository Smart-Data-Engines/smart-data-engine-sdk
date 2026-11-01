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

#include "sde/books.hpp"
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
/// generations must offer it; one without may. It refuses (`EngineError`) a table missing a column
/// the layout names or holding it as another type. Given the keys, it also returns how each table's
/// key and indexes differ from the design; given none, it checks the columns and reads nothing
/// else, as the reference does when it is passed no keys.
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
/// The days and instants an engine holds, read from its server (format contract section 8b). The
/// contract admits years 0001 to 9999 in UTC; ClickHouse 24.8 holds `Date32` and `DateTime64` from
/// 1900-01-01 to 2299-12-31 and moves a value outside to the nearer end without an error, and 26.9
/// holds the whole range.
struct TemporalRange {
  Date first_day;
  Date last_day;
  /// Microseconds from 1970-01-01T00:00:00 UTC, as `Timestamp` and `TimestampTz` hold them.
  std::int64_t first_micros = 0;
  std::int64_t last_micros = 0;

  /// Whether a value is inside: a `Date` by its day, a `Timestamp` or a `TimestampTz` by its
  /// microseconds, and text by the type of field it is given for (`kind`). Anything else is not a
  /// date or a time and is not this range's to judge.
  [[nodiscard]] bool holds(std::string_view kind, const Value& value) const;
  /// The range as a refusal says it: by its days, a timestamp's as well.
  [[nodiscard]] std::string describe() const;
  /// The range from four answers to `range_sql()`, or nothing for the contract's whole range.
  [[nodiscard]] static std::optional<TemporalRange> from_engine_text(std::string_view first_day,
                                                                     std::string_view last_day,
                                                                     std::string_view first,
                                                                     std::string_view last);
  /// ClickHouse's own ends: the contract's, moved as an insert would move them.
  [[nodiscard]] static std::string_view range_sql() noexcept;
};

/// An engine that takes a book by price and composes one (the orderbook engine's #233): a book
/// stream writes through it, and `Session::book_at` reads through it.
class BookWritable {
 public:
  virtual ~BookWritable() = default;
  /// Whether the server replaces a side of a book whole (`MREPLACE`, capability `replace_side`),
  /// which came with reads by price.
  [[nodiscard]] virtual bool supports_replace_side() = 0;
  /// Refuses the names of a book that a write to it would refuse, for a caller that holds them
  /// across many writes and should hear it before the first one.
  virtual void check_book(const std::string& symbol, const std::string& exchange) = 0;
  /// One update by price: each level sets its price's quantity, a quantity of 0 removes the price,
  /// and a price it does not name stays. The levels go in the order given. No levels is no update.
  virtual void apply_levels(const std::string& table, const std::string& symbol,
                            const std::string& exchange, BookSide side, std::int64_t timestamp_ns,
                            const std::vector<BookLevel>& levels) = 0;
  /// The whole side: the engine removes every price the side holds and these levels do not name.
  /// No levels empties the side.
  virtual void replace_levels(const std::string& table, const std::string& symbol,
                              const std::string& exchange, BookSide side, std::int64_t timestamp_ns,
                              const std::vector<BookLevel>& levels) = 0;
  /// The book as of `at_ns`, as the engine composes it from every update up to it, in time order:
  /// each side best first, at most `depth` levels a side.
  [[nodiscard]] virtual Book book_at(const std::string& table, const std::string& symbol,
                                     const std::string& exchange, std::int64_t at_ns,
                                     std::optional<std::int64_t> depth) = 0;
};

/// `a date` or `a timestamp`, for a field of `kind`.
[[nodiscard]] std::string_view temporal_noun(std::string_view kind) noexcept;

/// An engine whose server may hold fewer days and instants than the contract admits.
class TemporalBounded {
 public:
  virtual ~TemporalBounded() = default;
  /// The server's range, or nothing for the contract's whole range. Read once per connection.
  [[nodiscard]] virtual std::optional<TemporalRange> temporal_range() = 0;
};

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
  TemporalBounded* temporal = nullptr;
  BookWritable* books = nullptr;
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
