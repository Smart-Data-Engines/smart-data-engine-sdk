#include "sde/provisioning.hpp"

#include <algorithm>
#include <string>
#include <vector>

#include "generation.hpp"
#include "sde/errors.hpp"
#include "sde/write_fence.hpp"

namespace sde {

namespace {

constexpr int kGenerationsSince = 4;

/// The reference's `refuse_findings`: the table, the aspect and both values of every difference.
void refuse_findings(const std::vector<PhysicalFinding>& findings) {
  if (findings.empty()) return;
  std::string details;
  for (const PhysicalFinding& finding : findings) {
    if (!details.empty()) details += "; ";
    details += finding.to_string();
  }
  throw EngineError(
      "existing tables differ from the physical design this map declares: " + details +
      ". `CREATE ... IF NOT EXISTS` keeps whatever table or index already has the name, so this "
      "came from an earlier map or from outside SDE. A new layout needs fresh tables (staging), "
      "not the old ones under the new declaration.");
}

}  // namespace

void prepare_schema(const Model& model, const PlacementMap& placement,
                    const std::map<std::string, Engine*>& engines,
                    const std::optional<std::string>& project_id) {
  if (model.version() != placement.model_version()) {
    throw MigrationRefused("schema preparation needs the model named by the placement map");
  }
  const std::optional<std::string> local_project = detail::check_map_project(placement, project_id);
  for (const auto& [name, spot] : placement.groups()) {
    for (const Materialization* material : spot.all()) {
      if (engines.count(material->engine) == 0) {
        throw MigrationRefused("schema preparation is missing an engine named by the map");
      }
    }
  }
  if (placement.contract() >= kGenerationsSince) {
    // Before any statement: a map that cannot work creates nothing.
    for (const auto& [name, spot] : placement.groups()) {
      if (!spot.write_epoch) continue;
      for (const Materialization* material : spot.all()) {
        if (engines.at(material->engine)->capabilities().fences == nullptr) {
          throw MigrationRefused(
              "schema preparation needs native write generations on every engine of a group that "
              "carries one");
        }
      }
    }
    for (const auto& [name, spot] : placement.groups()) {
      if (spot.write_epoch) continue;
      for (const Materialization* material : spot.all()) {
        detail::refuse_a_fencing_engine(name, material->engine, *engines.at(material->engine));
      }
    }
  }
  for (const Group& group : model.groups()) {
    const GroupPlacement& spot = placement.placement_of(group.name);
    Keys keys;
    for (const std::string& member : group.members) keys[member] = model.entity(member).key;
    for (const Materialization* material : spot.all()) {
      Engine& engine = *engines.at(material->engine);
      refuse_findings(engine.ensure_schema(material->layout, keys));
      if (!spot.write_epoch) continue;
      std::vector<std::string> tables;
      for (const auto& [entity, table] : material->layout.tables) tables.push_back(table);
      std::sort(tables.begin(), tables.end());
      for (const std::string& table : tables) {
        (void)engine.capabilities().fences->write_fence(table, *local_project)
            .prepare(*spot.write_epoch);
      }
    }
  }
  if (placement.is_signed()) {
    // Storage only: recording this map's version here would activate it before the generations
    // are ready for the runtime, and could lock out the map in force.
    for (const auto& [name, engine] : engines) {
      if (WatermarkStore* store = engine->capabilities().watermark) (void)store->map_watermark();
    }
  }
}

}  // namespace sde
