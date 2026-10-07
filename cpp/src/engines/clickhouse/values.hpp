#pragma once

/// Values on their way into and out of ClickHouse. Two forms. A literal in a statement, written as
/// the reference's driver binds one: `clickhouse-connect`'s client-side binding, with the
/// reference's own rule for a moment in time. And rows in `RowBinaryWithNamesAndTypes`, both ways:
/// a value in its column's binary form, as the reference's driver reads and writes them in its
/// Native format, so a date of year 1 or 9999 is the date it was - text in and out would have the
/// server clamp it to the range it prints, silently.
///
/// Neither `bytes` nor `json` has a ClickHouse layout - the reference refuses both, for reasons it
/// measured - so neither is a literal here.

#include <string>
#include <string_view>
#include <vector>

#include "sde/value.hpp"

namespace sde::detail::clickhouse {

/// The value as a literal in a statement: `NULL`, `True`, a Python `repr` of a number, a quoted
/// and escaped text, `toDateTime64('...', 6, 'UTC')` for a moment in time. Refuses (`EngineError`)
/// a value of a type ClickHouse holds no column of.
[[nodiscard]] std::string literal(const Value& value);

/// An answer: its columns, their types as the server wrote them, and every row's values.
struct Answer {
  std::vector<std::string> names;
  std::vector<std::string> types;
  std::vector<std::vector<Value>> rows;
};

/// Reads `RowBinaryWithNamesAndTypes`. Refuses (`EngineError`) a type this library does not read,
/// an integer past 64 bits where one was promised, and an answer that ends early.
[[nodiscard]] Answer decode_answer(std::string_view body);

/// Writes rows as `RowBinaryWithNamesAndTypes` for columns of these types, each value as its
/// column's type holds it. Refuses (`EngineError`) a value the column cannot hold without changing
/// it - out of range, a decimal with more fractional digits than the column keeps, another kind.
[[nodiscard]] std::string encode_rows(const std::vector<std::string>& names,
                                      const std::vector<std::string>& types,
                                      const std::vector<std::vector<Value>>& rows);

}  // namespace sde::detail::clickhouse
