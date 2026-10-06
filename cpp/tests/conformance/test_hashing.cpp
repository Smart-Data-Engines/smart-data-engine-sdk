/// `hashing/`: a salt, a model, and every digest section 2a derives from them. A library that offers
/// hashing must pass all of it, because a client running two languages against one model needs both
/// to derive the same digests or each refuses the other's map.

#include <gtest/gtest.h>

#include "encoding.hpp"
#include "sde/hashing.hpp"
#include "support/vectors.hpp"

namespace {

using namespace sde::testing_support;

std::string salt_of(const std::filesystem::path& directory) {
  const auto salt = sde::detail::hex_decode(read_text(directory / "salt.hex"));
  EXPECT_TRUE(salt.has_value());
  return salt.value_or("");
}

void expect_names(const sde::NameMap& names, const sde::Json& expected) {
  for (const auto& [entity, digest] : expected.find("entities")->as_object()) {
    EXPECT_EQ(names.entity(entity), digest.as_string()) << entity;
  }
  for (const auto& [entity, fields] : expected.find("fields")->as_object()) {
    for (const auto& [field, digest] : fields.as_object()) {
      EXPECT_EQ(names.field(entity, field), digest.as_string()) << entity << "." << field;
    }
  }
  if (const sde::Json* relations = expected.find("relations")) {
    for (const auto& [entity, named] : relations->as_object()) {
      for (const auto& [relation, digest] : named.as_object()) {
        EXPECT_EQ(names.relation(entity, relation), digest.as_string()) << entity << "." << relation;
      }
    }
  }
  // The map holds exactly what the vector names, no more: a reserved identifier the container
  // swallowed would show up as a missing entry here (hashing/003).
  EXPECT_EQ(names.entities.size(), expected.find("entities")->as_object().size());
}

class HashingVector : public ::testing::TestWithParam<std::string> {};

TEST_P(HashingVector, DerivesEveryDigest) {
  const auto directory = vectors_root() / "hashing" / GetParam();
  const std::string salt = salt_of(directory);
  const sde::Model model = sde::load_neutral_model(read_json(directory / "model.json"));
  const auto [hashed, names] = sde::hash_identifiers(model, salt);

  EXPECT_EQ(hashed.ir_bytes(), read_bytes(directory / "ir.json"));
  EXPECT_EQ(hashed.version(), read_text(directory / "version.txt"));
  expect_names(names, read_json(directory / "names.json"));

  const sde::Json groups = read_json(directory / "groups.json");
  ASSERT_EQ(groups.as_array().size(), hashed.groups().size());
  for (std::size_t i = 0; i < hashed.groups().size(); ++i) {
    EXPECT_EQ(hashed.groups()[i].name, groups.as_array()[i].find("name")->as_string());
  }

  // One identifier in two normal forms must hash to one name (002).
  if (has_file(directory / "model-decomposed.json")) {
    const sde::Model decomposed =
        sde::load_neutral_model(read_json(directory / "model-decomposed.json"));
    const auto [hashed_decomposed, unused] = sde::hash_identifiers(decomposed, salt);
    EXPECT_EQ(hashed_decomposed.version(), read_text(directory / "version-decomposed.txt"));
    EXPECT_EQ(hashed_decomposed.version(), hashed.version());
  }
}

INSTANTIATE_TEST_SUITE_P(Hashing, HashingVector, ::testing::ValuesIn(cases("hashing")),
                         [](const auto& param_info) { return test_name(param_info.param); });

TEST(HashingFamily, EveryCaseRuns) { EXPECT_GT(count_cases("hashing"), 0U); }

}  // namespace
