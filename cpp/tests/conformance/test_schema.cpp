/// `schema/` (Tier 2, first half): the DDL a layout renders to, byte for byte.
///
/// The layout reaches the renderer through the map loader production uses - a whole `map.json`,
/// never a bare layout document - so a case can only pin DDL for a layout this library would accept
/// in the first place. Statements are compared exactly, because they are bytes a server receives;
/// refusals by substring, because they are diagnostics, as in `errors/`.

#include <algorithm>
#include <map>
#include <set>
#include <string>
#include <vector>

#include <gtest/gtest.h>

#include "sde/layout.hpp"
#include "sde/model.hpp"
#include "sde/placement.hpp"
#include "sde/schema.hpp"
#include "support/vectors.hpp"

namespace {

using namespace sde::testing_support;

std::vector<std::string> strings(const sde::Json& array) {
  std::vector<std::string> out;
  for (const sde::Json& item : array.as_array()) out.push_back(item.as_string());
  return out;
}

/// The materialisation with this id, and its group. Searched across every group: a map's ids are
/// unique in the whole document (`errors/013`), so a case naming the group too would carry a fact
/// the map already carries.
std::pair<std::string, const sde::Materialization*> find_materialization(
    const sde::PlacementMap& map, const std::string& id) {
  for (const auto& [group, placement] : map.groups()) {
    for (const sde::Materialization* found : placement.all()) {
      if (found->id == id) return {group, found};
    }
  }
  ADD_FAILURE() << "the vector names materialisation " << id << ", which this map does not have";
  return {"", nullptr};
}

class SchemaVector : public ::testing::TestWithParam<std::string> {};

TEST_P(SchemaVector, RendersEveryCase) {
  const auto directory = vectors_root() / "schema" / GetParam();
  const sde::Model model = sde::load_neutral_model(read_json(directory / "model.json"));
  sde::LoadOptions options;
  options.model = &model;
  const sde::PlacementMap map = sde::load_map(read_json(directory / "map.json"), options);

  const sde::Json document = read_json(directory / "cases.json");
  ASSERT_FALSE(document.as_array().empty());
  for (const sde::Json& expectation : document.as_array()) {
    const std::string& id = expectation.find("materialization")->as_string();
    const std::string& dialect = expectation.find("dialect")->as_string();
    SCOPED_TRACE(id + " as " + dialect);
    const auto [group_name, found] = find_materialization(map, id);
    ASSERT_NE(found, nullptr);

    // The one place a case edits its own input. `columns` is optional in the document, so a map
    // naming tables alone loads cleanly - and once it has loaded there is no other way to say "the
    // map said nothing about columns".
    sde::PhysicalLayout layout = found->layout;
    if (const sde::Json* columns = expectation.find("layout_columns")) {
      layout.columns.clear();
      for (const auto& [entity, types] : columns->as_object()) {
        for (const auto& [column, type] : types.as_object()) {
          layout.columns[entity][column] = type.as_string();
        }
      }
    }
    std::map<std::string, std::vector<std::string>> keys;
    if (const sde::Json* given = expectation.find("keys")) {
      for (const auto& [entity, key] : given->as_object()) keys[entity] = strings(key);
    } else {
      const auto group = std::find_if(model.groups().begin(), model.groups().end(),
                                      [&](const sde::Group& g) { return g.name == group_name; });
      ASSERT_NE(group, model.groups().end());
      for (const std::string& member : group->members) keys[member] = model.entity(member).key;
    }

    if (const sde::Json* fixed = expectation.find("fixed")) {
      EXPECT_EQ(sde::schema_is_fixed(dialect), fixed->as_bool()) << "schema_is_fixed(" << dialect << ")";
    } else {
      const sde::Json& want = *expectation.find("fixed_error");
      expect_refusal([&] { (void)sde::schema_is_fixed(dialect); },
                     want.find("error")->as_string(), want.find("match")->as_string());
    }

    if (const sde::Json* error = expectation.find("error")) {
      expect_refusal([&] { (void)sde::schema_statements(layout, keys, dialect); },
                     error->as_string(), expectation.find("match")->as_string());
      continue;
    }
    EXPECT_EQ(sde::schema_statements(layout, keys, dialect),
              strings(*expectation.find("statements")))
        << "the DDL differs from the vector. Two libraries that agree on a map and disagree here "
           "place one entity in tables with different columns, and each created a table "
           "successfully.";

    if (const sde::Json* views = expectation.find("views")) {
      std::map<std::string, std::string> was;
      for (const auto& [entity, table] : views->find("was")->as_object()) {
        was[entity] = table.as_string();
      }
      const sde::CompatibilityViews rendered = sde::compatibility_views(layout, was, dialect);
      EXPECT_EQ(rendered.create, strings(*views->find("create")));
      EXPECT_EQ(rendered.drop, strings(*views->find("drop")));
      EXPECT_EQ(rendered.complete(), views->find("complete")->as_bool());
      const sde::Json::Array& impossible = views->find("not_possible")->as_array();
      ASSERT_EQ(rendered.not_possible.size(), impossible.size());
      for (std::size_t i = 0; i < impossible.size(); ++i) {
        const auto& [entity, why] = rendered.not_possible[i];
        EXPECT_EQ(entity, impossible[i].find("entity")->as_string());
        for (const std::string& fragment : strings(*impossible[i].find("match"))) {
          EXPECT_NE(why.find(fragment), std::string::npos)
              << "the reason " << entity << " cannot have a view has to contain " << fragment
              << ". A reason may say more than the vector, and may not say less: " << why;
        }
      }
    }
  }
}

INSTANTIATE_TEST_SUITE_P(Schema, SchemaVector, ::testing::ValuesIn(cases("schema")),
                         [](const auto& param_info) { return test_name(param_info.param); });

TEST(SchemaFamily, CoversEveryDialectThisLibraryRenders) {
  // Totality, in the direction that rots: a dialect with no vector is one where two libraries can
  // disagree and nothing shared would notice.
  std::set<std::string> covered;
  for (const std::string& name : cases("schema")) {
    const sde::Json document = read_json(vectors_root() / "schema" / name / "cases.json");
    for (const sde::Json& expectation : document.as_array()) {
      covered.insert(expectation.find("dialect")->as_string());
    }
  }
  for (const std::string_view dialect : sde::DIALECTS) {
    EXPECT_EQ(covered.count(std::string(dialect)), 1U) << dialect << " has no schema/ vector";
  }
}

}  // namespace
