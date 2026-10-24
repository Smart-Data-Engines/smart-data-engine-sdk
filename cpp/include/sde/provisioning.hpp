#pragma once

/// Schema preparation: what the person provisioning a map runs before any application opens it.
///
/// From contract 4 a session creates nothing. Its connections need not hold the right to issue DDL,
/// and it checks the tables and the write generations instead of making them. This makes them -
/// the tables and indexes each layout describes, and the initial generation of each group carrying
/// one - through connections the client owns and configures for provisioning, before the runtime is
/// opened on other ones. Credentials stay in those adapters, as they do everywhere else.

#include <map>
#include <optional>
#include <string>

#include "sde/engine.hpp"
#include "sde/model.hpp"
#include "sde/placement.hpp"

namespace sde {

/// Applies the schema a map describes and the initial generation of each group that carries one;
/// never advances an existing generation.
///
/// Refuses (`MigrationRefused`) before any statement: a map for another model, a project that is
/// not the locally configured one, an engine the map names and `engines` lacks, an engine without
/// write generations under a group carrying one, and - contract 6 - an engine with them under a
/// group carrying none. Refuses (`EngineError`) a table that already exists with another physical
/// design, naming the table, the aspect and both values: a session only reports that, and this is
/// where a person can act on it. For a signed map it prepares each engine's bookkeeping table
/// without recording this map's version, which would activate the map before its generations are
/// ready and could lock out the one in force.
void prepare_schema(const Model& model, const PlacementMap& placement,
                    const std::map<std::string, Engine*>& engines,
                    const std::optional<std::string>& project_id = std::nullopt);

}  // namespace sde
