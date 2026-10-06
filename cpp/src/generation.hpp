#pragma once

/// Write generations in a session (format contract section 7d): what a write carries, what a read
/// gives back, and what a session checks before it routes anything.

#include <map>
#include <optional>
#include <string>
#include <vector>

#include "sde/engine.hpp"
#include "sde/model.hpp"
#include "sde/placement.hpp"
#include "sde/value.hpp"

namespace sde::detail {

/// A project id given to a session: 32 lowercase hexadecimal digits, or none.
void check_project_id(const std::optional<std::string>& project_id);

/// The locally configured project of a generation-bearing map (contract 4 and later), refused
/// unless it is the map's own - an identity is never learned from the document - and unless the
/// map has its immutable fingerprint. Empty below contract 4.
std::optional<std::string> check_map_project(const PlacementMap& placement,
                                             const std::optional<std::string>& project_id);

/// Contract 6: a group without a generation must be on an engine that cannot fence writes.
void refuse_a_fencing_engine(const std::string& group, const std::string& engine_name,
                             Engine& engine);

/// Checks every group's generation against the fences its engines hold, and returns how the tables
/// differ from the declared physical design (reported, never refused). Nothing below contract 4.
std::vector<PhysicalFinding> validate_generations(const Model& model,
                                                  const PlacementMap& placement,
                                                  const std::map<std::string, Engine*>& engines,
                                                  const std::optional<std::string>& project_id);

/// The values a write sends: the group's write epoch added under the reserved column, which the
/// caller may not set themselves.
Row stamp_values(const PlacementMap& placement, const std::string& group, const Row& values);

/// A row as the application sees it: without the reserved column, from contract 4.
Row logical_row(const PlacementMap& placement, Row row);

}  // namespace sde::detail
