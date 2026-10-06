#include "sde/migration.hpp"

#include <algorithm>
#include <string>

#include "python_compat.hpp"
#include "sde/errors.hpp"

namespace sde {

namespace {

using detail::python_repr;

}  // namespace

std::vector<std::string> key_columns(const std::vector<std::string>& order,
                                     const std::string& table) {
  if (order.empty()) {
    throw EngineError("a keyset scan of " + table +
                      " needs at least one ordering column. With none, every page is the first "
                      "page and a backfill would copy the same chunk until it was stopped.");
  }
  return order;
}

void same_width(const std::vector<Value>& bound, const std::vector<std::string>& columns,
                std::string_view name) {
  if (bound.size() != columns.size()) {
    throw EngineError(std::string(name) + " has " + std::to_string(bound.size()) +
                      " values and the order has " + std::to_string(columns.size()) +
                      " columns " + python_repr(columns) +
                      ". A row-value comparison of different widths is not a narrower "
                      "comparison, it is a different one.");
  }
}

std::optional<int> dialect_precision(std::string_view neutral, std::string_view dialect) noexcept {
  // PostgreSQL keeps microseconds in both timestamp types, and so does ClickHouse's DateTime64(6),
  // which is the layout's choice for both (format contract section 3.1).
  if ((neutral == "timestamp" || neutral == "timestamptz") &&
      (dialect == "postgres" || dialect == "clickhouse")) {
    return 6;
  }
  return std::nullopt;
}

std::optional<std::string> precision_refusal(std::string_view group, std::string_view entity,
                                             const std::map<std::string, std::string>& columns,
                                             std::string_view source_dialect,
                                             std::string_view target_dialect) {
  for (const auto& [column, neutral] : columns) {
    if (neutral.rfind("decimal(", 0) == 0 ||
        std::find(std::begin(PRECISION_INDEPENDENT), std::end(PRECISION_INDEPENDENT), neutral) !=
            std::end(PRECISION_INDEPENDENT)) {
      continue;
    }
    const std::optional<int> here = dialect_precision(neutral, source_dialect);
    const std::optional<int> there = dialect_precision(neutral, target_dialect);
    const std::string where =
        std::string(group) + "." + std::string(entity) + "." + column;
    if (!here || !there) {
      return where + " has neutral type " + python_repr(neutral) +
             ", and this library does not know whether " + std::string(source_dialect) + " and " +
             std::string(target_dialect) +
             " store it to the same precision. Refused rather than attempted: a type nobody "
             "classified is a type nobody checked, and the failure mode of guessing here is a "
             "value that comes back changed with no error anywhere.";
    }
    if (*there < *here) {
      return where + " is " + python_repr(neutral) + ", which " + std::string(source_dialect) +
             " stores to " + std::to_string(*here) + " sub-second digits and " +
             std::string(target_dialect) + " to " + std::to_string(*there) +
             ". Copying it would truncate every value with more precision than that - silently, "
             "because the insert succeeds and the value comes back changed - and `verify` would "
             "then find every such row mismatched at the end of the copy rather than before it. "
             "Your rows may all happen to be aligned to " +
             std::to_string(*there) +
             " digits, in which case this refusal costs you a migration that would have worked; we "
             "cannot tell without reading your data, and a copy that is faithful only for the "
             "values that happen to be present is not something to build a gate on.";
    }
  }
  return std::nullopt;
}

}  // namespace sde
