#include "sde/query.hpp"

#include <algorithm>
#include <charconv>
#include <cmath>
#include <limits>
#include <set>
#include <string>
#include <utility>

#include "bignum.hpp"
#include "python_compat.hpp"
#include "sde/schema.hpp"
#include "sde/unicode.hpp"
#include "unicode_internal.hpp"
#include "value_internal.hpp"

namespace sde {

namespace {

using detail::BigUnsigned;

/// The widest decimal any engine here compares or sums exactly: ClickHouse's `Decimal256`.
constexpr std::int64_t kDecimalDigits = 76;

bool starts_with(std::string_view text, std::string_view prefix) noexcept {
  return text.substr(0, prefix.size()) == prefix;
}

/// `decimal(p,s)` taken apart. Column types come from a checked model, so a malformed one is a
/// defect in the caller rather than a refusal to explain.
std::pair<int, int> decimal_type(std::string_view type) {
  const std::string_view inner = type.substr(8, type.size() - 9);  // "decimal(" ... ")"
  const std::size_t comma = inner.find(',');
  int precision = -1;
  int scale = -1;
  if (comma != std::string_view::npos) {
    std::from_chars(inner.data(), inner.data() + comma, precision);
    std::from_chars(inner.data() + comma + 1, inner.data() + inner.size(), scale);
  }
  if (precision < 0 || scale < 0) {
    throw std::invalid_argument("not a decimal type: " + std::string(type));
  }
  return {precision, scale};
}

/// The reference's `_decimal`: text, an integer or a `Decimal`, finite, at most 76 digits.
Decimal query_decimal(const Value& value) {
  if (const auto* decimal = std::get_if<Decimal>(&value)) {
    const std::int64_t digits =
        static_cast<std::int64_t>(decimal->integer_digits()) + decimal->scale();
    if (digits > kDecimalDigits || decimal->scale() > kDecimalDigits) {
      throw QueryRefused("decimal query values may use at most 76 digits");
    }
    return *decimal;
  }
  if (const auto* integer = std::get_if<std::int64_t>(&value)) return Decimal(*integer);
  const auto* text = std::get_if<std::string>(&value);
  if (text == nullptr) {
    throw QueryRefused("decimal query values require Decimal, integer or decimal text");
  }
  // Unicode's `White_Space` may surround the text; nothing else may.
  const auto parts = detail::decimal_parts(detail::strip_white_space(*text));
  if (!parts) throw QueryRefused("invalid decimal query value");
  const detail::DecimalCounts counts = detail::decimal_counts(*parts);
  // What CPython's `Decimal` refuses to construct, before the planner's own limit is reached.
  if (detail::python_decimal_invalid(counts)) throw QueryRefused("invalid decimal query value");
  // Checked before the digits are written out: a compact exponent must not allocate.
  if (counts.digits > kDecimalDigits || counts.scale > kDecimalDigits) {
    throw QueryRefused("decimal query values may use at most 76 digits");
  }
  return *detail::decimal_from_parts(*parts);
}

/// `\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2}(?::\d{2})?)?` with
/// Python's `\d`, which is every decimal digit and not only ASCII's. Text that has this shape and
/// is still not a timestamp gets the second of the reference's two refusals.
bool timestamp_shape(std::string_view text) {
  const auto points = detail::decode_utf8(text);
  if (!points) return false;
  const std::u32string& s = *points;
  std::size_t i = 0;
  const auto digits = [&](std::size_t count) {
    for (std::size_t k = 0; k < count; ++k, ++i) {
      if (i >= s.size() || !detail::is_decimal_digit(s[i])) return false;
    }
    return true;
  };
  const auto literal = [&](char32_t c) {
    if (i >= s.size() || s[i] != c) return false;
    ++i;
    return true;
  };
  if (!digits(4) || !literal(U'-') || !digits(2) || !literal(U'-') || !digits(2)) return false;
  if (i >= s.size() || (s[i] != U'T' && s[i] != U' ')) return false;
  ++i;
  if (!digits(2) || !literal(U':') || !digits(2) || !literal(U':') || !digits(2)) return false;
  if (i < s.size() && s[i] == U'.') {
    ++i;
    std::size_t count = 0;
    while (i < s.size() && detail::is_decimal_digit(s[i]) && count < 6) {
      ++i;
      ++count;
    }
    if (count == 0) return false;
  }
  if (i < s.size()) {
    if (s[i] == U'Z') {
      ++i;
    } else if (s[i] == U'+' || s[i] == U'-') {
      ++i;
      if (!digits(2) || !literal(U':') || !digits(2)) return false;
      if (i < s.size() && s[i] == U':') {
        ++i;
        if (!digits(2)) return false;
      }
    }
  }
  return i == s.size();
}

/// The reference's timestamp rule: ISO text or either timestamp, then the instant in UTC.
Value query_timestamp(const ReadColumn& column, const Value& value) {
  std::int64_t utc = 0;
  if (const auto* text = std::get_if<std::string>(&value)) {
    if (!timestamp_shape(*text)) {
      throw QueryRefused("timestamp query text needs at most six fractional digits");
    }
    const auto parsed = detail::parse_iso_timestamp(*text);
    if (!parsed) throw QueryRefused("invalid timestamp query value");
    utc = parsed->local_micros - parsed->offset_seconds.value_or(0) * detail::kMicrosPerSecond;
  } else if (const auto* naive = std::get_if<Timestamp>(&value)) {
    utc = naive->micros();  // a reading without a zone is taken as UTC
  } else if (const auto* aware = std::get_if<TimestampTz>(&value)) {
    utc = aware->micros();
  } else {
    throw QueryRefused("a timestamp query value must be datetime or ISO text");
  }
  if (!detail::micros_in_range(utc)) {
    throw QueryRefused("timestamp query value is outside UTC years 0001 through 9999");
  }
  if (column.type == "timestamp") return *Timestamp::from_micros(utc);
  return *TimestampTz::from_micros(utc);
}

/// The integer a decimal is at `scale`, as a sign and magnitude: the reference's `_decimal_integer`,
/// refusing digits past the scale rather than rounding them away.
std::pair<bool, BigUnsigned> decimal_integer(const Decimal& number, int scale) {
  std::string digits = number.coefficient();
  const int shift = scale - number.scale();
  if (shift < 0) {
    const auto dropped = static_cast<std::size_t>(-shift);
    if (number.is_zero()) {
      digits = "0";
    } else if (dropped >= digits.size() ||
               digits.find_first_not_of('0', digits.size() - dropped) != std::string::npos) {
      throw EngineError("summary result has fractional digits outside the stored scale");
    } else {
      digits.erase(digits.size() - dropped);
    }
  } else if (!number.is_zero()) {
    digits.append(static_cast<std::size_t>(shift), '0');
  }
  const BigUnsigned magnitude = *BigUnsigned::from_decimal(digits);
  return {number.negative() && !magnitude.is_zero(), magnitude};
}

/// A decimal from a sign, a magnitude and a scale; `-0` survives, as Python's tuple constructor
/// keeps the sign it is given.
Decimal scaled_decimal(bool negative, const BigUnsigned& magnitude, int scale) {
  return detail::make_decimal(negative, magnitude.to_decimal(), scale);
}

std::uint64_t summary_count(const std::string& text) {
  if (text.empty() || text.find_first_not_of("0123456789") != std::string::npos) {
    if (text.size() > 1 && text[0] == '-' &&
        text.find_first_not_of("0123456789", 1) == std::string::npos) {
      throw EngineError("summary counts are inconsistent");
    }
    throw EngineError("summary count is not an integer");
  }
  const auto parsed = BigUnsigned::from_decimal(text)->to_uint64();
  if (!parsed) throw EngineError("summary count is not an integer");
  return *parsed;
}

}  // namespace

std::string_view operation_name(ReadOperation operation) noexcept {
  switch (operation) {
    case ReadOperation::eq:
      return "eq";
    case ReadOperation::ge:
      return "ge";
    case ReadOperation::lt:
      return "lt";
  }
  return "eq";
}

Value query_value(const ReadColumn& column, const Value& value) {
  if (is_null(value)) return Null{};
  const std::string& kind = column.type;
  if (starts_with(kind, "decimal(")) return query_decimal(value);
  if (kind == "bool") {
    if (!std::holds_alternative<bool>(value)) {
      throw QueryRefused("a boolean query value must be a bool");
    }
    return value;
  }
  if (kind == "int32" || kind == "int64") {
    const auto* integer = std::get_if<std::int64_t>(&value);
    if (integer == nullptr) throw QueryRefused("an integer query value must be an integer");
    if (kind == "int32" && (*integer < std::numeric_limits<std::int32_t>::min() ||
                            *integer > std::numeric_limits<std::int32_t>::max())) {
      throw QueryRefused("integer query value is outside int32");
    }
    return value;
  }
  if (kind == "float32" || kind == "float64") {
    double number = 0;
    if (const auto* real = std::get_if<double>(&value)) {
      number = *real;
    } else if (const auto* integer = std::get_if<std::int64_t>(&value)) {
      number = static_cast<double>(*integer);
    } else {
      throw QueryRefused("a float query value must be a finite number");
    }
    if (!std::isfinite(number)) throw QueryRefused("float query bounds must be finite");
    return number;
  }
  if (kind == "string") {
    const auto* text = std::get_if<std::string>(&value);
    if (text == nullptr) throw QueryRefused("a string query value must be text");
    if (!is_scalar_text(*text)) {
      throw QueryRefused("query strings must contain Unicode scalar values");
    }
    return value;
  }
  if (kind == "uuid") {
    if (std::holds_alternative<Uuid>(value)) return value;
    if (const auto* text = std::get_if<std::string>(&value)) {
      if (auto parsed = Uuid::parse(*text)) return *parsed;
    }
    throw QueryRefused("a UUID query value must be UUID or canonical UUID text");
  }
  if (kind == "date") {
    if (std::holds_alternative<Date>(value)) return value;
    if (const auto* text = std::get_if<std::string>(&value)) {
      if (auto parsed = Date::parse(*text)) return *parsed;
    }
    throw QueryRefused("a date query value must be a date or YYYY-MM-DD text");
  }
  if (kind == "timestamp" || kind == "timestamptz") return query_timestamp(column, value);
  if (kind == "bytes" && std::holds_alternative<Bytes>(value)) return value;
  throw QueryRefused("query predicates are not supported for " + kind);
}

ReadPlan plan_read(const std::vector<ReadColumn>& columns, const std::vector<std::string>& key,
                   const ReadOptions& options) {
  const auto field = [&columns](const std::string& name) -> const ReadColumn& {
    for (const ReadColumn& column : columns) {
      if (column.name == name) return column;
    }
    throw QueryRefused("query refers to a field this entity does not declare");
  };

  const std::int64_t limit = options.limit.rows();
  if (limit < 1 || limit > MAX_PAGE_ROWS) {
    throw QueryRefused("page limit must be an integer between 1 and " +
                       std::to_string(MAX_PAGE_ROWS));
  }
  if (options.paginate && key.empty()) {
    throw QueryRefused("a paginated read requires an entity key");
  }
  std::vector<std::string> order_names;
  if (options.order_by) order_names.push_back(*options.order_by);
  for (const std::string& name : key) {
    if (!options.order_by || name != *options.order_by) order_names.push_back(name);
  }
  if (options.paginate && order_names.size() > MAX_ORDER_FIELDS) {
    throw QueryRefused("a read may order by at most " + std::to_string(MAX_ORDER_FIELDS) +
                       " fields");
  }
  ReadPlan plan;
  plan.columns = columns;
  plan.descending = options.descending;
  plan.limit = limit;
  if (options.paginate) {
    for (const std::string& name : order_names) plan.order.push_back(field(name));
  }
  for (const ReadColumn& column : plan.order) {
    if (column.type == "json" || column.type == "float32" || column.type == "float64") {
      throw QueryRefused("JSON and floating-point ordering are not supported by this read API");
    }
  }
  for (const ReadColumn& column : plan.order) {
    if (starts_with(column.type, "decimal(") && decimal_type(column.type).first > kDecimalDigits) {
      throw QueryRefused("decimal ordering supports at most 76 digits");
    }
  }
  if (options.where) {
    // A `Row` is ordered by name: the reference sorts the mapping's names the same way.
    for (const auto& [name, value] : *options.where) {
      const ReadColumn& column = field(name);
      if (column.type == "json") {
        throw QueryRefused("JSON predicates are not supported by this read API");
      }
      plan.filters.push_back(ReadFilter{column, ReadOperation::eq, query_value(column, value)});
    }
  }
  if (options.bounds) {
    const ReadColumn& column = field(options.bounds->field);
    static constexpr std::string_view kRanged[] = {"int32", "int64",    "float32", "float64",
                                                   "decimal(", "date", "timestamp"};
    if (std::none_of(std::begin(kRanged), std::end(kRanged),
                     [&](std::string_view prefix) { return starts_with(column.type, prefix); })) {
      throw QueryRefused("this field has no range-read shape");
    }
    if (is_null(options.bounds->low) && is_null(options.bounds->high)) {
      throw QueryRefused("a range needs at least one bound");
    }
    if (!is_null(options.bounds->low)) {
      plan.filters.push_back(
          ReadFilter{column, ReadOperation::ge, query_value(column, options.bounds->low)});
    }
    if (!is_null(options.bounds->high)) {
      plan.filters.push_back(
          ReadFilter{column, ReadOperation::lt, query_value(column, options.bounds->high)});
    }
  }
  if (options.after) {
    if (!options.paginate) throw QueryRefused("an aggregate has no pagination position");
    const std::set<std::string> wanted(order_names.begin(), order_names.end());
    std::set<std::string> given;
    for (const auto& [name, unused] : *options.after) given.insert(name);
    if (given != wanted) {
      throw QueryRefused("after must contain exactly the complete ordering key");
    }
    std::vector<Value> position;
    for (const ReadColumn& column : plan.order) {
      position.push_back(query_value(column, options.after->find(column.name)->second));
    }
    plan.after = std::move(position);
  }
  return plan;
}

int summary_scale(const ReadColumn& column, int mean_scale) {
  if (mean_scale < 0 || mean_scale > 38) {
    throw QueryRefused("mean_scale must be an integer between 0 and 38");
  }
  if (column.type == "int32" || column.type == "int64") return 0;
  if (starts_with(column.type, "decimal(")) {
    const auto [precision, scale] = decimal_type(column.type);
    if (precision <= 56) return scale;
  }
  throw QueryRefused("summaries require int32, int64 or decimal precision up to 56");
}

NumericSummary numeric_summary(const SummaryRecord& record, const ReadColumn& column,
                               int mean_scale) {
  const int scale = summary_scale(column, mean_scale);
  NumericSummary out;
  out.count = summary_count(record.count);
  out.non_null_count = summary_count(record.present);
  if (out.non_null_count > out.count) throw EngineError("summary counts are inconsistent");
  if (out.non_null_count == 0) return out;

  const auto read = [](const std::optional<std::string>& text) {
    return query_decimal(text ? Value(*text) : Value(Null{}));
  };
  const Decimal minimum = read(record.minimum);
  const Decimal maximum = read(record.maximum);
  const Decimal total = read(record.total);

  // The mean as an exact rational, rounded half to even at `mean_scale`, with no decimal context:
  // |total| * 10^mean_scale over count * 10^scale.
  const auto [negative, unscaled] = decimal_integer(total, scale);
  const BigUnsigned numerator = unscaled * BigUnsigned::pow10(static_cast<unsigned>(mean_scale));
  const BigUnsigned denominator =
      BigUnsigned(out.non_null_count) * BigUnsigned::pow10(static_cast<unsigned>(scale));
  auto [rounded, remainder] = divmod(numerator, denominator);
  const BigUnsigned twice = remainder + remainder;
  if (twice > denominator || (twice == denominator && rounded.is_odd())) rounded += BigUnsigned(1);
  // The sign is the total's even when the mean rounds to zero: the reference writes `-0`.
  out.mean = scaled_decimal(negative, rounded, mean_scale);

  const auto at_scale = [scale](const Decimal& value) {
    const auto [sign, magnitude] = decimal_integer(value, scale);
    return scaled_decimal(sign, magnitude, scale);
  };
  out.minimum = at_scale(minimum);
  out.maximum = at_scale(maximum);
  out.total = at_scale(total);
  return out;
}

Row read_row(const std::vector<ReadColumn>& columns, Row row) {
  for (const ReadColumn& column : columns) {
    const auto found = row.find(column.name);
    if (found == row.end()) {
      throw EngineError("the engine returned a row without the column " +
                        detail::python_repr(column.name));
    }
    if (is_null(found->second) || !starts_with(column.type, "decimal(")) continue;
    const int scale = decimal_type(column.type).second;
    const auto [negative, magnitude] = decimal_integer(query_decimal(found->second), scale);
    found->second = scaled_decimal(negative, magnitude, scale);
  }
  return row;
}

// --- the read's SQL -----------------------------------------------------------------------------

namespace {

bool is_float(const ReadColumn& column) {
  return column.type == "float32" || column.type == "float64";
}

std::string joined(const std::vector<std::string>& parts, std::string_view separator) {
  std::string out;
  for (std::size_t i = 0; i < parts.size(); ++i) {
    if (i > 0) out += separator;
    out += parts[i];
  }
  return out;
}

/// The column as a predicate and an ordering read it: text in the reads' collation, a ClickHouse
/// uuid as its text (whose order is the one a page follows), a float as a double.
std::string expression_of(const ReadColumn& column, std::string_view dialect) {
  const std::string expression = quote_identifier(dialect, column.name);
  if (column.type == "string" && dialect == "postgres") return "(" + expression + " COLLATE \"C\")";
  if (column.type == "uuid" && dialect == "clickhouse") return "toString(" + expression + ")";
  if (is_float(column)) {
    return dialect == "postgres" ? "CAST(" + expression + " AS double precision)"
                                 : "toFloat64(" + expression + ")";
  }
  return expression;
}

std::string comparison(const ReadColumn& column, std::string_view op, const Value& value,
                       std::string_view dialect, const Parameter& parameter) {
  std::string expression = expression_of(column, dialect);
  if (is_null(value)) return expression + " IS NULL";
  if (column.type == "uuid" && dialect == "clickhouse" && op == "=") {
    // Equality compares the native value: a condition on `toString` cannot prune the primary key.
    return quote_identifier(dialect, column.name) + " = toUUID(" +
           parameter(Value(std::get<Uuid>(value).to_string())) + ")";
  }
  std::string bound;
  if (starts_with(column.type, "decimal(")) {
    const auto [precision, declared] = decimal_type(column.type);
    const Decimal& number = std::get<Decimal>(value);
    const int scale = std::max(declared, std::max(0, number.scale()));
    if (precision - declared + scale > kDecimalDigits) {
      throw QueryRefused("decimal comparison requires more than 76 digits");
    }
    const std::string target = dialect == "postgres"
                                   ? "numeric(76," + std::to_string(scale) + ")"
                                   : "Nullable(Decimal(76," + std::to_string(scale) + "))";
    expression = "CAST(" + expression + " AS " + target + ")";
    bound = "CAST(" + parameter(Value(number.to_string())) + " AS " + target + ")";
  } else if (column.type == "uuid" && dialect == "clickhouse") {
    bound = parameter(Value(std::get<Uuid>(value).to_string()));
  } else {
    bound = parameter(value);
  }
  std::string out = expression + " " + std::string(op) + " " + bound;
  if (is_float(column) && op != "=") {
    const std::string not_nan = dialect == "postgres"
                                    ? expression + " <> 'NaN'::double precision"
                                    : "NOT isNaN(" + expression + ")";
    out = "(" + not_nan + " AND " + out + ")";
  }
  return out;
}

std::string_view operator_of(ReadOperation operation) noexcept {
  switch (operation) {
    case ReadOperation::eq:
      return "=";
    case ReadOperation::ge:
      return ">=";
    case ReadOperation::lt:
      return "<";
  }
  return "=";
}

std::vector<std::string> filter_clauses(const ReadPlan& plan, std::string_view dialect,
                                        const Parameter& parameter) {
  std::vector<std::string> clauses;
  for (const ReadFilter& filter : plan.filters) {
    clauses.push_back(
        comparison(filter.column, operator_of(filter.operation), filter.value, dialect, parameter));
  }
  return clauses;
}

}  // namespace

std::string read_sql(std::string_view table, const ReadPlan& plan, std::string_view dialect,
                     const Parameter& parameter, bool count) {
  std::vector<std::string> clauses = filter_clauses(plan, dialect, parameter);
  if (!count && plan.after) {
    std::vector<std::string> alternatives;
    const std::string_view op = plan.descending ? "<" : ">";
    for (std::size_t index = 0; index < plan.order.size(); ++index) {
      const Value& value = (*plan.after)[index];
      if (is_null(value)) continue;
      // Each occurrence is bound in SQL order. Reusing a prefix would reuse a named ClickHouse
      // parameter, but leave PostgreSQL's positional parameters misaligned.
      std::vector<std::string> parts;
      for (std::size_t position = 0; position < index; ++position) {
        parts.push_back(
            comparison(plan.order[position], "=", (*plan.after)[position], dialect, parameter));
      }
      const std::string expression = expression_of(plan.order[index], dialect);
      const std::string later = comparison(plan.order[index], op, value, dialect, parameter);
      parts.push_back("(" + expression + " IS NULL OR " + later + ")");
      alternatives.push_back("(" + joined(parts, " AND ") + ")");
    }
    clauses.push_back(alternatives.empty() ? "FALSE" : "(" + joined(alternatives, " OR ") + ")");
  }
  const std::string where = clauses.empty() ? "" : " WHERE " + joined(clauses, " AND ");
  const std::string final = dialect == "clickhouse" ? " FINAL" : "";
  const std::string quoted = quote_identifier(dialect, table);
  if (count) {
    const std::string expression =
        dialect == "postgres" ? "CAST(count(*) AS text)" : "toString(count())";
    return "SELECT " + expression + " AS sde_count FROM " + quoted + final + where;
  }
  std::vector<std::string> projection;
  for (const ReadColumn& column : plan.columns) {
    const std::string name = quote_identifier(dialect, column.name);
    if (dialect == "clickhouse" && (column.type == "timestamp" || column.type == "timestamptz")) {
      projection.push_back("toTimeZone(" + name + ", 'UTC') AS " + name);
    } else if (starts_with(column.type, "decimal(")) {
      projection.push_back(dialect == "postgres" ? "CAST(" + name + " AS text) AS " + name
                                                 : "toString(" + name + ") AS " + name);
    } else {
      projection.push_back(name);
    }
  }
  std::vector<std::string> order;
  const std::string direction = plan.descending ? "DESC" : "ASC";
  for (const ReadColumn& column : plan.order) {
    order.push_back(expression_of(column, dialect) + " " + direction + " NULLS LAST");
  }
  return "SELECT " + joined(projection, ", ") + " FROM " + quoted + final + where + " ORDER BY " +
         joined(order, ", ") + " LIMIT " + parameter(Value(plan.limit + 1));
}

std::string summary_sql(std::string_view table, const ReadPlan& plan, const ReadColumn& column,
                        std::string_view dialect, const Parameter& parameter) {
  const int scale = summary_scale(column, 0);
  const std::string quoted = quote_identifier(dialect, column.name);
  const std::string target = dialect == "postgres"
                                 ? "numeric(76," + std::to_string(scale) + ")"
                                 : "Nullable(Decimal(76," + std::to_string(scale) + "))";
  const auto text = [&](const std::string& expression) {
    return dialect == "postgres" ? "CAST(" + expression + " AS text)"
                                 : "toString(" + expression + ")";
  };
  const std::vector<std::string> fields = {
      text("count(*)") + " AS sde_count",
      text("count(" + quoted + ")") + " AS sde_present",
      text("min(" + quoted + ")") + " AS sde_min",
      text("max(" + quoted + ")") + " AS sde_max",
      text("sum(CAST(" + quoted + " AS " + target + "))") + " AS sde_total",
  };
  const std::vector<std::string> clauses = filter_clauses(plan, dialect, parameter);
  const std::string where = clauses.empty() ? "" : " WHERE " + joined(clauses, " AND ");
  const std::string final = dialect == "clickhouse" ? " FINAL" : "";
  return "SELECT " + joined(fields, ", ") + " FROM " + quote_identifier(dialect, table) + final +
         where;
}

}  // namespace sde
