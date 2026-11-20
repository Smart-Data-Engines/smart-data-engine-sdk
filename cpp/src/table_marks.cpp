#include "table_marks_internal.hpp"

#include <algorithm>
#include <regex>
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
  static const std::regex copy_mark(R"(sde\.copy1\.[0-9a-f]{32}\.[0-9a-f]{16}\.[0-9a-f]{16})");
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
  std::map<std::string, std::vector<const Named*>> reach;
  for (const Named& named : ordered) {
    const auto engine_facts = facts.find(named.engine);
    if (engine_facts == facts.end()) continue;
    const auto found = engine_facts->second.find(named.table);
    if (found == engine_facts->second.end()) continue;
    const TableFacts& fact = found->second;
    if (named.copy) {
      if (!fact.keys.empty() && !owned_copy_marker(project_id, named.group, named.table, fact.mark)) {
        throw MigrationRefused(copy_refusal(named.table, fact.mark));
      }
      if (fact.keys.empty() && copies_present) {
        throw MigrationRefused("the copy's table " + python_repr(named.table) + " of " +
                               named.group + " is not there: provision the map (prepare_schema) "
                               "before a session writes to it");
      }
    } else if (fact.mark && std::regex_match(*fact.mark, copy_mark)) {
      throw MigrationRefused(source_refusal(named.table, named.group));
    }
    std::vector<std::string> keys = fact.keys;
    std::sort(keys.begin(), keys.end());
    for (const std::string& key : keys) reach[key].push_back(&named);
  }
  for (const Named& named : ordered) {
    if (!named.copy) continue;
    const auto engine_facts = facts.find(named.engine);
    if (engine_facts == facts.end()) continue;
    const auto found = engine_facts->second.find(named.table);
    if (found == engine_facts->second.end()) continue;
    std::vector<std::string> keys = found->second.keys;
    std::sort(keys.begin(), keys.end());
    for (const std::string& key : keys) {
      for (const Named* other : reach[key]) {
        const bool same = other->group == named.group &&
                          other->materialization == named.materialization &&
                          other->table == named.table;
        if (!same) {
          throw MigrationRefused("the copy's table " + python_repr(named.table) + " of " +
                                 named.group + " is also " + other->group + "'s table " +
                                 python_repr(other->table) + " for " + other->entity + " in " +
                                 other->engine + ": a copy's table holds that copy alone, and two "
                                 "names of the map reach this one");
        }
      }
    }
  }
}

}  // namespace sde::detail
