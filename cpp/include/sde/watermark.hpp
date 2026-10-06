#pragma once

/// Refusing a placement map that goes backwards (format contract section 7, "Forward only").
///
/// A signed map for version 3 verifies forever - that is what a signature is - so replacing the
/// client's map file with an older signed one would load cleanly and route writes to the previous
/// placement. Refusing it needs memory, and the memory lives in the client's own engines, in a table
/// this library owns: append-only, and the watermark is the highest version any of them recorded.
/// Only signed maps are checked; an unsigned map is the client's own document, and in that mode this
/// does nothing at all - no table, no query, no cost.

#include <cstdint>
#include <map>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

#include "sde/engine.hpp"
#include "sde/json.hpp"
#include "sde/log.hpp"
#include "sde/placement.hpp"

namespace sde {

/// What the check did, in a form a client can assert on. A protection whose state cannot be read is
/// a protection taken on trust.
struct WatermarkCheck {
  /// `enforced`, `unavailable` (no engine of the map can keep the bookkeeping) or `not_applicable`
  /// (the map is unsigned).
  std::string protection;
  std::int64_t map_version = 0;
  std::optional<std::int64_t> highest_seen;
  std::vector<std::string> participating;  ///< sorted
  std::vector<std::string> unable;         ///< sorted
  std::string why;

  [[nodiscard]] Json as_record() const;
};

/// Refuses (`MapRolledBack`) a signed map older than the newest these engines have seen; equal is
/// allowed, because a restart against the same map is the ordinary case. Records the version in
/// every engine that can keep it, only when it moves.
WatermarkCheck enforce_forward_only(const PlacementMap& placement,
                                    const std::map<std::string, Engine*>& engines,
                                    const LogSink& log = {});

}  // namespace sde
