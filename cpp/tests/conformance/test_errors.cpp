/// `errors/`: which error, raised at which stage. The stage is the point: a library raising the right
/// error at the wrong time has a different bug, which a class-only assertion cannot see.
///
/// - `model`: the loader *and* the builder run inside the assertion, because a refusal about the
///   shape of a declaration comes out of the loader.
/// - `map`: the model is built first, outside the assertion - a vector whose model broke by accident
///   would otherwise satisfy an assertion that looks only at the class - and the map is loaded
///   inside it, with the case's `load` arguments.
/// - `session` and `write` need engine adapters, which is Tier 2.
///
/// A stage this runner does not know is a failure, never a skip: a stage nobody runs is a rule
/// nobody checks.

#include <gtest/gtest.h>

#include "conformance/stages.hpp"
#include "sde/model.hpp"
#include "support/vectors.hpp"

namespace {

using namespace sde::testing_support;

class ErrorVector : public ::testing::TestWithParam<std::string> {};

TEST_P(ErrorVector, IsRefusedAtItsStage) {
  const auto directory = vectors_root() / "errors" / GetParam();
  const sde::Json expected = read_json(directory / "expected.json");
  const std::string stage = expected.find("stage")->as_string();
  const std::string error = expected.find("error")->as_string();
  const std::string match = expected.find("match")->as_string();

  if (stage == "model") {
    expect_refusal([&] { (void)sde::load_neutral_model(read_json(directory / "model.json")); },
                   error, match);
    return;
  }
  if (stage == "map") {
    run_map_stage(directory, expected, error, match);
    return;
  }
  if (stage == "session" || stage == "write") {
    run_engine_stage(directory, expected, stage, error, match);
    return;
  }
  FAIL() << "the runner does not know the stage '" << stage
         << "'; an unknown stage fails rather than skips";
}

INSTANTIATE_TEST_SUITE_P(Errors, ErrorVector, ::testing::ValuesIn(cases("errors")),
                         [](const auto& param_info) { return test_name(param_info.param); });

TEST(ErrorsFamily, EveryCaseRuns) { EXPECT_GT(count_cases("errors"), 0U); }

}  // namespace
