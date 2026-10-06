/// `signature/`: accepting a **set** of public keys, which is what makes rotating the signing key
/// possible without breaking anybody. The one family whose expectations were produced by openssl
/// rather than by a library (conformance/tools/signature_vectors.py).

#include <cstdint>
#include <map>

#include <gtest/gtest.h>

#include "encoding.hpp"
#include "sde/model.hpp"
#include "sde/placement.hpp"
#include "support/vectors.hpp"

namespace {

using namespace sde::testing_support;

std::string decoded(const sde::Json& value) {
  const auto bytes = sde::detail::base64_decode(value.as_string());
  EXPECT_TRUE(bytes.has_value()) << "a key in keys.json is not base64";
  return bytes.value_or("");
}

class SignatureVector : public ::testing::TestWithParam<std::string> {};

TEST_P(SignatureVector, VerifiesOrRefuses) {
  const auto directory = vectors_root() / "signature" / GetParam();
  const sde::Model model = sde::load_neutral_model(read_json(directory / "model.json"));
  const sde::Json document = read_json(directory / "map.json");
  const sde::Json encoded = read_json(directory / "keys.json");
  const sde::Json expected = read_json(directory / "expected.json");

  sde::LoadOptions options;
  options.model = &model;
  options.require_signature = true;
  // One entry under the empty name is the bare-key form. It is not the same call as a one-entry
  // set, and the difference is what the library reports back afterwards.
  const auto& members = encoded.as_object();
  if (members.size() == 1 && members.front().first.empty()) {
    options.public_keys = sde::PublicKeys::bare(decoded(members.front().second));
  } else {
    std::map<std::string, std::string> keys;
    for (const auto& [name, value] : members) keys.emplace(name, decoded(value));
    options.public_keys = sde::PublicKeys::named(std::move(keys));
  }

  if (const sde::Json* error = expected.find("error")) {
    expect_refusal([&] { (void)sde::load_map(document, options); }, error->as_string(),
                   expected.find("match")->as_string());
    return;
  }
  const sde::PlacementMap map = sde::load_map(document, options);
  EXPECT_TRUE(map.is_signed());
  const sde::Json& verified = *expected.find("verified_with");
  if (verified.is_null()) {
    EXPECT_FALSE(map.verified_with().has_value()) << *map.verified_with();
  } else {
    EXPECT_EQ(map.verified_with().value_or("<none>"), verified.as_string());
  }
  if (const sde::Json* fingerprint = expected.find("map_fingerprint")) {
    EXPECT_EQ(map.fingerprint().value_or("<none>"), fingerprint->as_string());
    EXPECT_EQ(map.project_id().value_or("<none>"), expected.find("project_id")->as_string());
    // Only the groups that carry a generation; contract 6 lets a group carry none.
    std::map<std::string, std::int64_t> epochs;
    for (const auto& [name, group] : map.groups()) {
      if (group.write_epoch) epochs.emplace(name, *group.write_epoch);
    }
    std::map<std::string, std::int64_t> want;
    for (const auto& [name, value] : expected.find("write_epochs")->as_object()) {
      want.emplace(name, value.to_int64().value_or(-1));
    }
    EXPECT_EQ(epochs, want);
  }
}

INSTANTIATE_TEST_SUITE_P(Signature, SignatureVector, ::testing::ValuesIn(cases("signature")),
                         [](const auto& param_info) { return test_name(param_info.param); });

// A family of nothing but refusals proves a library can refuse, never that it can accept.
TEST(SignatureFamily, CoversBothOutcomes) {
  bool refused = false;
  bool accepted = false;
  for (const std::string& name : cases("signature")) {
    const sde::Json expected = read_json(vectors_root() / "signature" / name / "expected.json");
    (expected.contains("error") ? refused : accepted) = true;
  }
  EXPECT_TRUE(refused);
  EXPECT_TRUE(accepted);
}

}  // namespace
