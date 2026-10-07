#pragma once

#include <map>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

#include "sde/json.hpp"
#include "sde/physical.hpp"

namespace sde::detail {

using Tables = std::map<std::string, std::string>;
using Columns = std::map<std::string, std::map<std::string, std::string>>;

[[nodiscard]] std::map<std::string, std::vector<std::string>> parse_key_order(
    const Json& raw, const std::string& where, const Tables& tables);
[[nodiscard]] std::map<std::string, Partition> parse_partition_by(const Json& raw,
                                                                  const std::string& where,
                                                                  const Tables& tables);
[[nodiscard]] std::vector<Index> parse_indexes(const Json* raw, const std::string& where,
                                               const Tables& tables, const Columns& columns,
                                               int contract);

/// Why a key order is not a permutation of the key, in the reference's words; empty when it is.
/// The loader refuses with `MapError` and the DDL renderer with `EngineError`, as the reference's
/// `effective_key(error=...)` does, so the message lives here once.
[[nodiscard]] std::optional<std::string> key_order_refusal(std::string_view where,
                                                           std::string_view entity,
                                                           const std::vector<std::string>& key,
                                                           const std::vector<std::string>& ordered);

/// Why a partition is not on a key column, in the reference's words; empty when it is.
[[nodiscard]] std::optional<std::string> partition_key_refusal(std::string_view where,
                                                               std::string_view entity,
                                                               const std::vector<std::string>& key,
                                                               const Partition& partition);

/// ClickHouse's partition expression for a granularity, as its catalogue reports it back:
/// `toDate`, `toYYYYMM`, `toYear`. Empty for a granularity outside the vocabulary.
[[nodiscard]] std::string_view partition_function(std::string_view granularity) noexcept;

/// The two rules that need the model: a key order is a permutation of the key, and a partition is
/// on a key column of a time type.
void check_against_model(const std::string& where, const std::string& entity,
                         const std::vector<std::string>& key,
                         const std::map<std::string, std::string>& field_types,
                         const std::vector<std::string>* key_order, const Partition* partition);

/// `parse_identifier_list`: `a, \`b c\`, d` as the names ClickHouse's catalogue writes, a quoted
/// one with `\\` escaping a backslash and the backtick, and the same list in one pair of
/// parentheses, as ClickHouse 26.5 and later write a single expression that was written in them.
/// Empty when the text is not such a list: an expression the parser does not understand is not
/// evidence that a table matches.
[[nodiscard]] std::optional<std::vector<std::string>> parse_identifier_list(std::string_view text);

/// A partition key as the catalogue writes it, `toYYYYMM(\`at\`)`: the function and the field, or
/// nothing for a table with no partition key.
struct PartitionKey {
  std::string function;
  std::string field;
};
/// Empty when the text is not one function of one name, alone or in one pair of parentheses.
[[nodiscard]] std::optional<std::optional<PartitionKey>> parse_partition_key(std::string_view text);

/// Python's `repr` of a tuple of texts: `()`, `('a',)`, `('a', 'b')`.
[[nodiscard]] std::string python_tuple_repr(const std::vector<std::string>& items);

}  // namespace sde::detail
