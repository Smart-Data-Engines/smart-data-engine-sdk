#pragma once

#include <map>
#include <string>
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

/// The two rules that need the model: a key order is a permutation of the key, and a partition is
/// on a key column of a time type.
void check_against_model(const std::string& where, const std::string& entity,
                         const std::vector<std::string>& key,
                         const std::map<std::string, std::string>& field_types,
                         const std::vector<std::string>* key_order, const Partition* partition);

}  // namespace sde::detail
