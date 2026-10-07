#pragma once

/// Bounded logical reads (Tier 2): a page in a stable order, a count, an exact numeric summary.
///
/// A read plan is built and checked before any engine is called, and the `query/` vectors pin it:
/// which filters, in which order, over which normalised values; which columns order the page; where
/// the next page starts. **The values stay in the application's process.** A plan's values reach an
/// engine as parameters and never a shape, a telemetry window or a message.

#include <concepts>
#include <cstdint>
#include <functional>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

#include "sde/errors.hpp"
#include "sde/value.hpp"

namespace sde {

inline constexpr std::int64_t MAX_PAGE_ROWS = 1000;
inline constexpr std::size_t MAX_ORDER_FIELDS = 32;

/// A half-open range over one field: `low <= field < high`. A bound left `Null` is open.
struct Range {
  std::string field;
  Value low = Null{};
  Value high = Null{};
};

/// One column a read may name: the field and its neutral type.
struct ReadColumn {
  std::string name;
  std::string type;

  friend bool operator==(const ReadColumn&, const ReadColumn&) = default;
};

enum class ReadOperation { eq, ge, lt };

/// `eq`, `ge`, `lt`: the names the `query/` vectors use.
[[nodiscard]] std::string_view operation_name(ReadOperation operation) noexcept;

struct ReadFilter {
  ReadColumn column;
  ReadOperation operation = ReadOperation::eq;
  Value value;  ///< normalised to the column's type (`query_value`); `Null` means IS NULL
};

/// What a read will do, checked: every value normalised, every name a declared field.
struct ReadPlan {
  std::vector<ReadColumn> columns;
  std::vector<ReadFilter> filters;
  std::vector<ReadColumn> order;  ///< empty for an aggregate
  bool descending = false;
  std::optional<std::vector<Value>> after;  ///< the position, one value per `order` column
  std::int64_t limit = 100;
};

/// One page of a scan, and the position the next one starts after (empty on the last page).
struct ScanPage {
  std::vector<Row> rows;
  std::optional<Row> next_after;
};

/// A page size. Any integer type converts to it and `bool` does not: `limit = true` is a compile
/// error here, where the reference refuses it when the read is planned (`query/012`).
class PageLimit {
 public:
  constexpr PageLimit() noexcept = default;
  template <std::integral T>
    requires(!std::same_as<T, bool>)
  constexpr PageLimit(T rows) noexcept  // NOLINT(google-explicit-constructor)
      : rows_(static_cast<std::int64_t>(rows)) {}
  PageLimit(bool) = delete;

  [[nodiscard]] constexpr std::int64_t rows() const noexcept { return rows_; }

 private:
  std::int64_t rows_ = 100;
};

/// What a read asks for, in the client's terms.
struct ReadOptions {
  /// Equality on each field named; `Null` matches NULL.
  std::optional<Row> where;
  std::optional<Range> bounds;
  /// The field the page is ordered by first; the key follows it. Empty for the key alone.
  std::optional<std::string> order_by;
  bool descending = false;
  /// The position to continue after: exactly the ordering fields (`order_by` and the key).
  std::optional<Row> after;
  PageLimit limit;
  /// False for a count or a summary: no order, no position.
  bool paginate = true;
};

/// A value for a predicate or a position, normalised to the column's type without any engine:
/// decimal text, an integer or a `Decimal` to `Decimal` (at most 76 digits); a canonical UUID text
/// to `Uuid`; `YYYY-MM-DD` to `Date`; ISO text or either timestamp to `Timestamp` (UTC, no zone)
/// for a `timestamp` column and `TimestampTz` for a `timestamptz` one. Refuses (`QueryRefused`) a
/// value of the wrong kind and a type that has no predicates (`json`).
[[nodiscard]] Value query_value(const ReadColumn& column, const Value& value);

/// A read checked against an entity's columns and key, in the reference's order of refusals.
[[nodiscard]] ReadPlan plan_read(const std::vector<ReadColumn>& columns,
                                 const std::vector<std::string>& key,
                                 const ReadOptions& options = {});

/// The scale an exact summary of the column computes at: 0 for an integer, the declared scale for
/// a decimal of precision up to 56. Refuses (`QueryRefused`) any other column, and a `mean_scale`
/// outside 0 to 38.
[[nodiscard]] int summary_scale(const ReadColumn& column, int mean_scale);

/// An engine's answer to a summary, as text: the server casts every aggregate to text so that no
/// driver passes it through a binary float. `minimum`, `maximum` and `total` are empty for NULL.
struct SummaryRecord {
  std::string count;
  std::string present;
  std::optional<std::string> minimum;
  std::optional<std::string> maximum;
  std::optional<std::string> total;
};

/// Exact statistics of an integer or decimal column. Minimum, maximum and total come back at the
/// column's scale (0 for an integer); the mean at `mean_scale` digits, rounded half to even from the
/// exact rational total / non-null count. All four are empty when no row had a value.
struct NumericSummary {
  std::uint64_t count = 0;
  std::uint64_t non_null_count = 0;
  std::optional<Decimal> minimum;
  std::optional<Decimal> maximum;
  std::optional<Decimal> total;
  std::optional<Decimal> mean;
};

/// Decodes an engine's summary answer. Refuses (`QueryRefused`) a column or scale a summary does
/// not support; an answer an engine could not have given (counts that disagree, a value with digits
/// past the column's scale) is the engine's error (`EngineError`).
[[nodiscard]] NumericSummary numeric_summary(const SummaryRecord& record, const ReadColumn& column,
                                             int mean_scale);

/// A row as an engine returned it, with each decimal put back at its column's declared scale: an
/// engine may return `8.5` for a `decimal(12,2)` that holds `8.50`.
[[nodiscard]] Row read_row(const std::vector<ReadColumn>& columns, Row row);

/// Binds one value and returns the placeholder the statement writes for it. The callback owns the
/// driver's representation, as the reference's `parameter` does: a libpq adapter keeps the value's
/// text and returns `$n`, a ClickHouse one a named server-side parameter. It is called once per
/// value, in the order the statement uses them, which positional placeholders depend on.
using Parameter = std::function<std::string(const Value& value)>;

/// One native query of a planned read in `postgres` or `clickhouse` SQL - the page, or with `count`
/// the number of its rows - every value bound through `parameter`. Byte for byte the reference's
/// `read_sql`; refuses (`QueryRefused`) a decimal comparison that needs more than 76 digits.
[[nodiscard]] std::string read_sql(std::string_view table, const ReadPlan& plan,
                                   std::string_view dialect, const Parameter& parameter,
                                   bool count = false);

/// The exact summary of one integer or decimal column of a planned read, every aggregate cast to
/// text by the server so that no driver passes it through a binary float. Byte for byte the
/// reference's `summary_sql`; refuses (`QueryRefused`) a column `summary_scale` refuses.
[[nodiscard]] std::string summary_sql(std::string_view table, const ReadPlan& plan,
                                      const ReadColumn& column, std::string_view dialect,
                                      const Parameter& parameter);

}  // namespace sde
