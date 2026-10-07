#pragma once

/// The checks a batch gets before any engine sees it, the reference's `sde.bulk`: shared by the
/// session, which checks the batch an application gave it, and by an adapter, whose `insert_many`
/// a caller can reach without a session.

#include <cstddef>
#include <string>
#include <vector>

#include "sde/value.hpp"

namespace sde::detail {

inline constexpr std::size_t kMaxBatchRows = 1000;
inline constexpr std::size_t kMaxBatchValues = 60'000;

/// A row's field names, in code point order.
[[nodiscard]] std::vector<std::string> names_of(const Row& row);

/// The batch's field names, checked whole: at most `kMaxBatchRows` nonempty rows with the same
/// fields, and at most `kMaxBatchValues` values counting `extra_columns` in every row. Refuses
/// anything else (`BulkWriteRefused`), and no message carries a value.
[[nodiscard]] std::vector<std::string> batch_columns(const std::vector<Row>& rows,
                                                     std::size_t extra_columns = 0);

}  // namespace sde::detail
