#pragma once

/// Copying a group into its second engine, and proving the copy is complete (Tier 2).
///
/// This module produces numbers and the control plane decides: copying a row means reading a
/// client's row, and comparing two copies means holding both engines open, neither of which the
/// control plane may ever do.

#include <map>
#include <optional>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

#include "sde/value.hpp"

namespace sde {

/// The ordering columns of a keyset scan, refusing an empty order (`EngineError`): with none, every
/// page is the first page and a backfill would copy the same chunk until it was stopped.
[[nodiscard]] std::vector<std::string> key_columns(const std::vector<std::string>& order,
                                                   const std::string& table);

/// A bound has one value per ordering column (`EngineError` otherwise): a row-value comparison of
/// another width is a different comparison, not a narrower one.
void same_width(const std::vector<Value>& bound, const std::vector<std::string>& columns,
                std::string_view name);

/// Sub-second digits a dialect keeps, for the neutral types where dialects differ. A type in
/// neither this table nor `PRECISION_INDEPENDENT` refuses a copy, so adding one to the vocabulary
/// forces a decision rather than inheriting one nobody made.
[[nodiscard]] std::optional<int> dialect_precision(std::string_view neutral,
                                                   std::string_view dialect) noexcept;

/// Neutral types a copy between dialects does not silently change (`decimal(p,s)` is one too).
inline constexpr std::string_view PRECISION_INDEPENDENT[] = {
    "bool", "int32", "int64", "float32", "float64", "string", "bytes", "uuid", "date", "json"};

/// Why copying these columns from one dialect to another would change values, or nothing. Asked
/// by a backfill before it copies and by a session before its first fan-out write: a truncation in
/// the fan-out happens on every write, with no error anywhere.
[[nodiscard]] std::optional<std::string> precision_refusal(
    std::string_view group, std::string_view entity,
    const std::map<std::string, std::string>& columns, std::string_view source_dialect,
    std::string_view target_dialect);

}  // namespace sde
