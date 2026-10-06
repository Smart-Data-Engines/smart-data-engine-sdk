/// `routing/`: where each operation goes - the three conditions and the lookup of section 8 - and,
/// for a map mid-migration, where writes fan out to. A fan-out target read differently in two
/// languages is a row written to one copy and not the other, and nothing raises when it happens.

#include <gtest/gtest.h>

#include "sde/model.hpp"
#include "sde/placement.hpp"
#include "sde/routing.hpp"
#include "support/vectors.hpp"

namespace {

using namespace sde::testing_support;

bool flag(const sde::Json& expectation, std::string_view key) {
  const sde::Json* value = expectation.find(key);
  return value != nullptr && value->is_bool() && value->as_bool();
}

class RoutingVector : public ::testing::TestWithParam<std::string> {};

TEST_P(RoutingVector, ResolvesEveryCase) {
  const auto directory = vectors_root() / "routing" / GetParam();
  const sde::Model model = sde::load_neutral_model(read_json(directory / "model.json"));
  sde::LoadOptions options;
  options.model = &model;
  const sde::PlacementMap map = sde::load_map(read_json(directory / "map.json"), options);

  const sde::Json cases_json = read_json(directory / "cases.json");
  ASSERT_FALSE(cases_json.as_array().empty());
  for (const sde::Json& expectation : cases_json.as_array()) {
    const std::string& id = expectation.find("shape")->as_string();
    const sde::OperationShape* shape = model.find_shape(id);
    ASSERT_NE(shape, nullptr) << "the vector refers to shape " << id
                              << ", which this library does not enumerate";
    const sde::RouteContext context{flag(expectation, "in_write_transaction"),
                                    flag(expectation, "fresh")};
    EXPECT_EQ(sde::resolve(map, *shape, context).id, expectation.find("expect")->as_string())
        << shape->entity << "." << shape->kind;
  }

  if (has_file(directory / "also_write.json")) {
    // Held in a variable: a range-for over a member of a temporary dangles before C++23.
    const sde::Json fan_out = read_json(directory / "also_write.json");
    for (const auto& [group, ids] : fan_out.as_object()) {
      std::vector<std::string> want;
      for (const sde::Json& id : ids.as_array()) want.push_back(id.as_string());
      std::vector<std::string> got;
      for (const sde::Materialization* copy : map.placement_of(group).also_write_targets()) {
        got.push_back(copy->id);
      }
      EXPECT_EQ(got, want) << "group " << group;
    }
  }
}

INSTANTIATE_TEST_SUITE_P(Routing, RoutingVector, ::testing::ValuesIn(cases("routing")),
                         [](const auto& param_info) { return test_name(param_info.param); });

}  // namespace
