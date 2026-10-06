#include "generation.hpp"

#include <algorithm>
#include <string>

#include "sde/errors.hpp"
#include "sde/write_fence.hpp"

namespace sde::detail {

namespace {

constexpr int kGenerationsSince = 4;

Keys keys_of(const Model& model, const PhysicalLayout& layout) {
  Keys keys;
  for (const auto& [entity, unused] : layout.tables) keys[entity] = model.entity(entity).key;
  return keys;
}

bool lower_hex(const std::string& text, std::size_t width) {
  return text.size() == width && std::all_of(text.begin(), text.end(), [](char c) {
           return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f');
         });
}

}  // namespace

void check_project_id(const std::optional<std::string>& project_id) {
  if (project_id && !lower_hex(*project_id, 32)) {
    throw MigrationRefused("verification project_id must be 32 lowercase hexadecimal digits");
  }
}

std::optional<std::string> check_map_project(const PlacementMap& placement,
                                             const std::optional<std::string>& project_id) {
  if (placement.contract() < kGenerationsSince) return std::nullopt;
  if (!project_id || project_id != placement.project_id()) {
    throw MigrationRefused(
        "this generation-bearing map needs its locally configured project_id; do not learn that "
        "identity from the supplied map");
  }
  if (!placement.fingerprint()) {
    throw MigrationRefused("generation-bearing sessions need an immutable loaded placement map");
  }
  return project_id;
}

void refuse_a_fencing_engine(const std::string& group, const std::string& engine_name,
                             Engine& engine) {
  if (engine.capabilities().fences != nullptr) {
    throw MigrationRefused("group " + group + " carries no write generation on " + engine_name +
                           ", which fences writes; a map we issue gives every group on such an "
                           "engine a generation, so this one was built for another engine");
  }
}

std::vector<PhysicalFinding> validate_generations(const Model& model,
                                                  const PlacementMap& placement,
                                                  const std::map<std::string, Engine*>& engines,
                                                  const std::optional<std::string>& project_id) {
  std::vector<PhysicalFinding> findings;
  if (placement.contract() < kGenerationsSince) return findings;
  const std::string local_project = *check_map_project(placement, project_id);
  if (model.version() != placement.model_version()) {
    throw MigrationRefused("the generation-bearing map names another session model");
  }
  for (const auto& [name, spot] : placement.groups()) {
    if (!spot.write_epoch) {
      // Contract 6: a group with no write generation, on an engine that cannot fence. The map
      // carries no dialect, so this is the first place either half can be checked.
      for (const Materialization* material : spot.all()) {
        Engine& engine = *engines.at(material->engine);
        refuse_a_fencing_engine(name, material->engine, engine);
        if (SchemaValidator* validator = engine.capabilities().schema) {
          const std::vector<PhysicalFinding> found =
              validator->validate_schema(material->layout, keys_of(model, material->layout));
          findings.insert(findings.end(), found.begin(), found.end());
        }
      }
      continue;
    }
    for (const Materialization* material : spot.all()) {
      const Capabilities offered = engines.at(material->engine)->capabilities();
      Fencable* fences = offered.fences;
      if (fences == nullptr || offered.schema == nullptr) {
        throw MigrationRefused("engine " + material->engine +
                               " does not implement write generations");
      }
      const std::vector<PhysicalFinding> found =
          offered.schema->validate_schema(material->layout, keys_of(model, material->layout));
      findings.insert(findings.end(), found.begin(), found.end());
      std::vector<std::string> tables;
      for (const auto& [entity, table] : material->layout.tables) tables.push_back(table);
      std::sort(tables.begin(), tables.end());
      for (const std::string& table : tables) {
        const FenceState state = fences->write_fence(table, local_project).state();
        if (!state.complete() || state.epoch() != spot.write_epoch) {
          throw MigrationRefused("the write generation for " + name + " is not active in " +
                                 material->engine +
                                 "; provision the signed map or load the current map before "
                                 "opening a session");
        }
      }
    }
  }
  return findings;
}

Row stamp_values(const PlacementMap& placement, const std::string& group, const Row& values) {
  // Reserved in every group of a generation-bearing map, including one without a generation of
  // its own (contract 6): a read strips the column from every row such a map returns.
  if (placement.contract() >= kGenerationsSince && values.count(EPOCH_COLUMN) != 0) {
    throw MigrationRefused("the write-epoch column is reserved for the SDK");
  }
  const std::optional<std::int64_t>& epoch = placement.placement_of(group).write_epoch;
  if (!epoch) return values;
  Row stamped = values;
  stamped[std::string(EPOCH_COLUMN)] = *epoch;
  return stamped;
}

Row logical_row(const PlacementMap& placement, Row row) {
  if (placement.contract() >= kGenerationsSince) row.erase(std::string(EPOCH_COLUMN));
  return row;
}

}  // namespace sde::detail
