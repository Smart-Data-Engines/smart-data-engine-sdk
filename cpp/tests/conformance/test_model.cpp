/// `model/`: a neutral declaration, and the exact IR bytes, version, groups and shapes it produces.
/// `model/001-single-entity` is hand-written from the contract rather than generated, so it is the
/// vector that says whether sections 1 and 4 were read correctly.

#include <gtest/gtest.h>

#include "sde/canonical.hpp"
#include "sde/model.hpp"
#include "support/vectors.hpp"

namespace {

using namespace sde::testing_support;

class ModelVector : public ::testing::TestWithParam<std::string> {};

TEST_P(ModelVector, ProducesTheIrVersionGroupsAndShapes) {
  const auto directory = vectors_root() / "model" / GetParam();
  const sde::Model model = sde::load_neutral_model(read_json(directory / "model.json"));

  // Bytes, not a document: parsing ir.json and comparing structures would pass two libraries that
  // agree on the structure and disagree on key order or normalisation.
  EXPECT_EQ(model.ir_bytes(), read_bytes(directory / "ir.json"));
  EXPECT_EQ(model.version(), read_text(directory / "version.txt"));

  const sde::Json groups = read_json(directory / "groups.json");
  ASSERT_EQ(groups.as_array().size(), model.groups().size());
  for (std::size_t i = 0; i < model.groups().size(); ++i) {
    const sde::Json& expected = groups.as_array()[i];
    EXPECT_EQ(model.groups()[i].name, expected.find("name")->as_string());
    std::vector<std::string> members;
    for (const sde::Json& member : expected.find("members")->as_array()) {
      members.push_back(member.as_string());
    }
    EXPECT_EQ(model.groups()[i].members, members);
  }

  // shapes.json is absent from model/001, which predates it; asserted whenever present.
  if (has_file(directory / "shapes.json")) {
    const sde::Json shapes = read_json(directory / "shapes.json");
    ASSERT_EQ(shapes.as_array().size(), model.shapes().size());
    for (std::size_t i = 0; i < model.shapes().size(); ++i) {
      const sde::Json& expected = shapes.as_array()[i];
      const sde::OperationShape& shape = model.shapes()[i];
      EXPECT_EQ(shape.id, expected.find("id")->as_string()) << i;
      sde::Json actual = shape.as_ir();
      actual.set("id", shape.id);
      EXPECT_EQ(actual, expected) << "shape " << i << " differs";
    }
  }
}

INSTANTIATE_TEST_SUITE_P(Model, ModelVector, ::testing::ValuesIn(cases("model")),
                         [](const auto& param_info) { return test_name(param_info.param); });

TEST(ModelFamily, EveryCaseRuns) {
  EXPECT_GT(count_cases("model"), 0U);
}

TEST(ModelFamily, TheHandWrittenVectorPinsTheEncoding) {
  // If this library ever disagrees with 001, this library is wrong until somebody argues otherwise
  // in writing (conformance/README.md).
  const auto directory = vectors_root() / "model" / "001-single-entity";
  const sde::Model model = sde::load_neutral_model(read_json(directory / "model.json"));
  EXPECT_EQ(sde::canonical_bytes(model.ir()), read_bytes(directory / "ir.json"));
}

}  // namespace
