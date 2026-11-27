#include "table_marks_internal.hpp"

#include <algorithm>
#include <utility>
#include <vector>

#include "ownership_internal.hpp"
#include "python_compat.hpp"
#include "sde/errors.hpp"

namespace sde::detail {

namespace {

struct Named {
  std::string engine, group, materialization, entity, table;
  bool copy = false;
};

}  // namespace

void check_table_marks(const PlacementMap& placement, const std::map<std::string, Engine*>& engines,
                       const std::string& project_id, bool copies_present) {
  if (placement.contract() < GENERATIONS_SINCE) return;
  std::vector<Named> ordered;
  std::map<std::string, std::vector<std::string>> by_engine;
  std::map<std::string, bool> engine_has_copy;
  for (const auto& [group, spot] : placement.groups()) {  // in name order
    if (!spot.write_epoch) continue;
    for (const Materialization* material : spot.all()) {
      const bool copy = material != &spot.source;
      for (const auto& [entity, table] : material->layout.tables) {  // entities in name order
        ordered.push_back({material->engine, group, material->id, entity, table, copy});
        by_engine[material->engine].push_back(table);
        if (copy) engine_has_copy[material->engine] = true;
      }
    }
  }
  std::map<std::string, std::map<std::string, TableFacts>> facts;
  for (const auto& [engine, tables] : by_engine) {
    ReadsTableFacts* reader = engines.at(engine)->capabilities().facts;
    if (reader != nullptr) {
      facts.emplace(engine, reader->table_facts(tables));
    } else if (engine_has_copy[engine]) {
      throw MigrationRefused("engine " + engine + " holds a copy, and its adapter cannot read the "
                             "mark a copy's table carries (table_facts, format contract §7r)");
    }
  }
  // A name the adapter gave no answer for reaches no table.
  const TableFacts none{};
  const auto facts_of = [&](const Named& named) -> const TableFacts* {
    const auto engine_facts = facts.find(named.engine);
    if (engine_facts == facts.end()) return nullptr;
    const auto found = engine_facts->second.find(named.table);
    return found == engine_facts->second.end() ? &none : &found->second;
  };
  std::map<std::string, std::vector<const Named*>> reach;
  for (const Named& named : ordered) {
    // Before prepare_schema creates the copies, a copy's name may reach a table of a later schema of
    // the search path, where the copy is not created: its own check, and this one after the last
    // statement, read the copy where it is.
    if (named.copy && !copies_present) continue;
    const TableFacts* answer = facts_of(named);
    if (answer == nullptr) continue;
    const TableFacts& fact = *answer;
    if (named.copy) {
      if (!fact.keys.empty() && !owned_copy_marker(project_id, named.group, named.table, fact.mark)) {
        throw MigrationRefused(copy_refusal(named.table, fact.mark));
      }
      if (fact.keys.empty() && copies_present) {
        throw MigrationRefused("the copy's table " + python_repr(named.table) + " of " +
                               named.group + " is not there: provision the map (prepare_schema) "
                               "before a session writes to it");
      }
    } else if (copy_marks(fact.mark)) {
      throw MigrationRefused(source_refusal(named.table, named.group, fact.mark));
    }
    std::vector<std::string> keys = fact.keys;
    std::sort(keys.begin(), keys.end());
    for (const std::string& key : keys) reach[key].push_back(&named);
  }
  for (const Named& named : ordered) {
    if (named.copy && !copies_present) continue;
    const TableFacts* answer = facts_of(named);
    if (answer == nullptr) continue;
    std::vector<std::string> keys = answer->keys;
    std::sort(keys.begin(), keys.end());
    for (const std::string& key : keys) {
      for (const Named* other : reach[key]) {
        if (other != &named) {
          const std::string whose = named.copy ? "copy's" : "source's";
          const std::string alone = named.copy ? "that copy" : "its group";
          throw MigrationRefused("the " + whose + " table " + python_repr(named.table) + " of " +
                                 named.group + " for " + named.entity + " is also " +
                                 other->group + "'s table " + python_repr(other->table) + " for " +
                                 other->entity + " in " + other->engine + ": a " + whose +
                                 " table holds one entity of " + alone +
                                 " alone, and two names of the map reach this one");
        }
      }
    }
  }
}

}  // namespace sde::detail
