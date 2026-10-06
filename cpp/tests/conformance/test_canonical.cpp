/// `canonical/`: a value fed straight to the encoder, and the exact bytes - or the refusal.
/// Every expectation in this family was written by hand from the contract (section 1), which makes
/// it the first thing a new implementation should pass and the check on whether the document says
/// enough.

#include <gtest/gtest.h>

#include "sde/canonical.hpp"
#include "support/vectors.hpp"

namespace {

using namespace sde::testing_support;

class CanonicalVector : public ::testing::TestWithParam<std::string> {};

TEST_P(CanonicalVector, MatchesTheBytesOrTheRefusal) {
  const auto directory = vectors_root() / "canonical" / GetParam();
  const sde::Json value = read_json(directory / "value.json");
  if (has_file(directory / "expected.json")) {
    const sde::Json expected = read_json(directory / "expected.json");
    expect_refusal([&] { (void)sde::canonical_bytes(value); },
                   expected.find("error")->as_string(), expected.find("match")->as_string());
    return;
  }
  // bytes.json holds bytes, not a document: compared exactly, with no trailing newline.
  EXPECT_EQ(sde::canonical_bytes(value), read_bytes(directory / "bytes.json"));
}

INSTANTIATE_TEST_SUITE_P(Canonical, CanonicalVector, ::testing::ValuesIn(cases("canonical")),
                         [](const auto& param_info) { return test_name(param_info.param); });

TEST(CanonicalFamily, EveryCaseRuns) {
  // Zero vectors found is a failure, never a vacuous pass.
  EXPECT_GT(count_cases("canonical"), 0U);
  EXPECT_EQ(cases("canonical").size(), count_cases("canonical"));
}

}  // namespace
