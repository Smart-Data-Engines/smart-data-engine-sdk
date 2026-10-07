#include "bulk.hpp"

#include "sde/errors.hpp"

namespace sde::detail {

std::vector<std::string> names_of(const Row& row) {
  std::vector<std::string> names;
  for (const auto& [name, unused] : row) names.push_back(name);
  return names;
}

std::vector<std::string> batch_columns(const std::vector<Row>& rows, std::size_t extra_columns) {
  if (rows.size() > kMaxBatchRows) {
    throw BulkWriteRefused("a batch may contain at most " + std::to_string(kMaxBatchRows) + " rows");
  }
  std::vector<std::string> columns;
  for (std::size_t i = 0; i < rows.size(); ++i) {
    if (rows[i].empty()) {
      throw BulkWriteRefused("each batch row must be a nonempty mapping with string fields");
    }
    std::vector<std::string> here = names_of(rows[i]);
    if (i == 0) {
      columns = std::move(here);
      if (rows.size() * (columns.size() + extra_columns) > kMaxBatchValues) {
        throw BulkWriteRefused("a batch may contain at most " + std::to_string(kMaxBatchValues) +
                               " values, including generation");
      }
    } else if (here != columns) {
      throw BulkWriteRefused("all batch rows must have the same fields");
    }
  }
  return columns;
}

}  // namespace sde::detail
