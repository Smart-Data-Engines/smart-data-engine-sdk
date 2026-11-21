#pragma once

/// The marks of a map's tables, read where a library writes to them (format contract section 7r),
/// as the reference's `sde.table_marks` reads them.

#include <map>
#include <string>

#include "sde/engine.hpp"
#include "sde/placement.hpp"

namespace sde::detail {

/// For the groups with a write generation: each copy's table carries this copy's mark, each source's
/// table carries no copy's mark, and no copy's or source's table is reached by another name of the
/// map - one `table_facts` a binding. `copies_present` is false before `prepare_schema` creates the
/// copies. A copy on an engine that cannot read marks is refused; a source there is taken.
void check_table_marks(const PlacementMap& placement, const std::map<std::string, Engine*>& engines,
                       const std::string& project_id, bool copies_present = true);

}  // namespace sde::detail
