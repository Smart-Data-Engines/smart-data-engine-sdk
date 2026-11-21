/// A session reads the marks of its map's tables when it opens (format contract section 7r), on
/// live engines: the cases of the reference's `test_table_marks_live.py`.
///
/// Two independent reviews of copy ownership wrote over another group's rows through a session that
/// read no mark. A session of a map with write generations now refuses, before it writes: a copy's
/// table without its own mark, a source's table with a copy's mark, and a copy's or a source's
/// table that another name of the map reaches. `prepare_schema` refuses the same. The control: a
/// map provisioned as `prepare_schema` provisions it opens.

#include <functional>
#include <map>
#include <memory>
#include <string>
#include <utility>
#include <vector>

#include <gtest/gtest.h>

#include "live/live.hpp"
#include "live/sides.hpp"
#include "sde/errors.hpp"
#include "sde/layout.hpp"
#include "sde/model.hpp"
#include "sde/placement.hpp"
#include "sde/provisioning.hpp"
#include "sde/session.hpp"

namespace {

using sde::live::Side;
using sde::live::side_of;

const std::string kProject(32, '3');

struct Placed {
  std::pair<std::string, std::string> source;                   // engine, table
  std::vector<std::pair<std::string, std::string>> copies;      // engine, table
};

sde::Model two_groups() {
  return sde::load_neutral_model(R"({"entities": [
      {"name": "Account", "fields": [{"name": "id", "type": "int64"}, {"name": "value", "type": "int32"}],
       "key": ["id"]},
      {"name": "Event", "fields": [{"name": "id", "type": "int64"}, {"name": "value", "type": "int32"}],
       "key": ["id"]}]})");
}

sde::PlacementMap place(const sde::Model& model, const Side& side,
                        const std::map<std::string, Placed>& groups, int version = 1) {
  const std::string columns = R"({"id": ")" + sde::column_type("int64", side.dialect()) +
                              R"(", "value": ")" + sde::column_type("int32", side.dialect()) + R"("})";
  const auto material = [&](const std::string& engine, const std::string& id,
                            const std::string& entity, const std::string& table) {
    return R"({"engine": ")" + engine + R"(", "id": ")" + id + R"(", "layout": {"tables": {")" +
           entity + R"(": ")" + table + R"("}, "columns": {")" + entity + R"(": )" + columns + "}}}";
  };
  std::string body = R"({"contract": 4, "project_id": ")" + kProject + R"(", "model_version": ")" +
                     model.version() + R"(", "map_version": )" + std::to_string(version) +
                     R"(, "groups": {)";
  bool first = true;
  for (const auto& [group, placed] : groups) {
    body += std::string(first ? "" : ", ") + "\"" + group + R"(": {"write_epoch": 1, "source": )" +
            material(placed.source.first, group + "@source", group, placed.source.second);
    first = false;
    if (!placed.copies.empty()) {
      std::string derived;
      std::string also;
      for (std::size_t n = 0; n < placed.copies.size(); ++n) {
        std::string copy = material(placed.copies[n].first, group + "@copy" + std::to_string(n),
                                    group, placed.copies[n].second);
        copy.pop_back();
        derived += std::string(n == 0 ? "" : ", ") + copy + R"(, "lag_budget_ms": 60000})";
        also += std::string(n == 0 ? "" : ", ") + "\"" + group + "@copy" + std::to_string(n) + "\"";
      }
      body += R"(, "derived": [)" + derived + R"(], "also_write": [)" + also + "]";
    }
    body += "}";
  }
  body += "}}";
  sde::LoadOptions options;
  options.model = &model;
  return sde::load_map(body, options);
}

sde::SessionOptions with_project() {
  sde::SessionOptions options;
  options.project_id = kProject;
  return options;
}

void unmark(Side& side, const std::string& table) {
  if (side.dialect() == "postgres") {
    side.run("COMMENT ON TABLE \"" + table + "\" IS NULL");
  } else {
    side.run("ALTER TABLE `" + table + "` MODIFY COMMENT ''");
  }
}

std::string refusal_of(const std::function<void()>& body) {
  try {
    body();
  } catch (const sde::MigrationRefused& refused) {
    return refused.what();
  }
  return "(no refusal)";
}

class TableMarks : public ::testing::TestWithParam<std::string> {};

TEST_P(TableMarks, AMapProvisionedAsPrepareSchemaProvisionsItOpens) {
  const std::unique_ptr<Side> side = side_of(GetParam());
  SDE_REQUIRE_SIDE(side, GetParam());
  const sde::Model model = two_groups();
  const std::map<std::string, sde::Engine*> engines{{"e", &side->engine()}};
  const sde::PlacementMap map = place(model, *side, {{"Account", {{"e", "acct"}, {{"e", "acct_copy"}}}},
                                                     {"Event", {{"e", "ev"}, {}}}});
  sde::prepare_schema(model, map, engines, kProject);
  const sde::Session session(model, map, engines, with_project());
}

TEST_P(TableMarks, ASessionRefusesACopyWithoutItsMark) {
  const std::unique_ptr<Side> side = side_of(GetParam());
  SDE_REQUIRE_SIDE(side, GetParam());
  const sde::Model model = two_groups();
  const std::map<std::string, sde::Engine*> engines{{"e", &side->engine()}};
  const sde::PlacementMap map = place(model, *side, {{"Account", {{"e", "acct"}, {{"e", "acct_copy"}}}},
                                                     {"Event", {{"e", "ev"}, {}}}});
  sde::prepare_schema(model, map, engines, kProject);
  unmark(*side, "acct_copy");
  EXPECT_NE(refusal_of([&] { const sde::Session session(model, map, engines, with_project()); })
                .find("is not one this library created for it"),
            std::string::npos);
}

TEST_P(TableMarks, AMarkedCopyNamedAsAnotherGroupsSourceIsRefused) {
  const std::unique_ptr<Side> side = side_of(GetParam());
  SDE_REQUIRE_SIDE(side, GetParam());
  const sde::Model model = two_groups();
  const std::map<std::string, sde::Engine*> engines{{"e", &side->engine()}};
  const sde::PlacementMap first = place(model, *side, {{"Account", {{"e", "acct"}, {{"e", "shared"}}}},
                                                       {"Event", {{"e", "ev"}, {}}}});
  sde::prepare_schema(model, first, engines, kProject);
  const sde::PlacementMap second = place(
      model, *side, {{"Account", {{"e", "acct"}, {}}}, {"Event", {{"e", "shared"}, {}}}}, 2);
  EXPECT_NE(refusal_of([&] { sde::prepare_schema(model, second, engines, kProject); })
                .find("carries a copy's mark"),
            std::string::npos);
  EXPECT_NE(refusal_of([&] { const sde::Session session(model, second, engines, with_project()); })
                .find("carries a copy's mark"),
            std::string::npos);
}

TEST_P(TableMarks, TwoEngineNamesBoundToOneDatabaseAreRefused) {
  const std::unique_ptr<Side> side = side_of(GetParam());
  SDE_REQUIRE_SIDE(side, GetParam());
  const sde::Model model = two_groups();
  const std::map<std::string, sde::Engine*> engines{{"a", &side->engine()}, {"b", &side->engine()}};
  const sde::PlacementMap map = place(model, *side, {{"Account", {{"a", "acct"}, {{"b", "shared"}}}},
                                                     {"Event", {{"a", "shared"}, {}}}});
  EXPECT_NE(refusal_of([&] { sde::prepare_schema(model, map, engines, kProject); })
                .find("carries a copy's mark"),
            std::string::npos);
  EXPECT_NE(refusal_of([&] { const sde::Session session(model, map, engines, with_project()); })
                .find("carries a copy's mark"),
            std::string::npos);
}

TEST_P(TableMarks, ACopyAnotherEngineNameReachesIsRefused) {
  const std::unique_ptr<Side> side = side_of(GetParam());
  SDE_REQUIRE_SIDE(side, GetParam());
  const sde::Model model = two_groups();
  const std::map<std::string, sde::Engine*> engines{{"a", &side->engine()}, {"b", &side->engine()}};
  const sde::PlacementMap map = place(
      model, *side,
      {{"Account", {{"a", "acct"}, {}}}, {"Event", {{"a", "ev"}, {{"a", "ev_copy"}, {"b", "ev_copy"}}}}});
  EXPECT_NE(refusal_of([&] { sde::prepare_schema(model, map, engines, kProject); })
                .find("two names of the map reach this one"),
            std::string::npos);
  EXPECT_NE(refusal_of([&] { const sde::Session session(model, map, engines, with_project()); })
                .find("two names of the map reach this one"),
            std::string::npos);
}

TEST_P(TableMarks, TwoGroupsSourcesReachingOneTableAreRefused) {
  // A session would write both groups' rows into the table, and an erasure of one group's subject
  // would delete the other's: refused after prepare_schema's last statement, and before a session's
  // first write - by two engine names bound to one database, and by one name.
  const std::unique_ptr<Side> side = side_of(GetParam());
  SDE_REQUIRE_SIDE(side, GetParam());
  const sde::Model model = two_groups();
  const std::map<std::string, sde::Engine*> engines{{"a", &side->engine()}, {"b", &side->engine()}};
  const std::string refused =
      "a source's table holds its group alone, and two names of the map reach this one";
  for (const std::string other : {"b", "a"}) {
    const sde::PlacementMap map =
        place(model, *side, {{"Account", {{"a", "shared"}, {}}}, {"Event", {{other, "shared"}, {}}}});
    EXPECT_NE(refusal_of([&] { sde::prepare_schema(model, map, engines, kProject); }).find(refused),
              std::string::npos)
        << other;
    EXPECT_NE(refusal_of([&] { const sde::Session session(model, map, engines, with_project()); })
                  .find(refused),
              std::string::npos)
        << other;
  }
}

INSTANTIATE_TEST_SUITE_P(Engines, TableMarks, ::testing::Values("postgres", "clickhouse"),
                         [](const ::testing::TestParamInfo<std::string>& info) { return info.param; });

}  // namespace
