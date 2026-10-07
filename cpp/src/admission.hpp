#pragma once

/// What a row may give a field of each neutral type, and the form the value reaches an engine in.
///
/// Format contract section 8b, point 4. Until 7 October 2026 a row's values went to the engine as
/// the application gave them, and in the other two libraries each driver and each engine converted
/// them in its own way. Measured through their `Session.save` on PostgreSQL 15 and ClickHouse 24.8:
/// a decimal with more fractional digits than its column was rounded by one engine and truncated by
/// the other, an integer outside int32 wrapped to the opposite sign, a date that does not exist
/// moved two days. The same model meant something different per engine.
///
/// A value is admitted in the forms a filter of the same type takes (`query_value`), so a row and a
/// read agree about what a value is, and it is passed on in the form a filter gets, so every adapter
/// receives one representation per type. A refusal says what the type does not hold and never the
/// value: a refusal is logged, and the value is the client's.

#include <functional>
#include <stdexcept>
#include <string>

#include "sde/value.hpp"

namespace sde::detail {

/// What a field's type does not hold about a value, in words that never include it.
class Misfit : public std::runtime_error {
 public:
  using std::runtime_error::runtime_error;
};

/// Returns the value as engines receive it, or throws `Misfit`.
using Check = std::function<Value(const Value&)>;

/// The check for a field of this neutral type; empty where every value is admitted (`json`).
[[nodiscard]] Check check_for(const std::string& kind);

}  // namespace sde::detail
