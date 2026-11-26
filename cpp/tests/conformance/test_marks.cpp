/// `marks/`: where each mark a library writes is taken (format contract section 7r) - as the copy
/// of a group in a project, and on a source. The marks were computed with `hashlib` from the
/// contract's forms and the answers written by hand (conformance/tools/marks_vectors.py).

#include <optional>
#include <string>

#include <gtest/gtest.h>

#include "sde/ownership.hpp"
#include "support/vectors.hpp"

namespace {

using namespace sde::testing_support;

class MarksVector : public ::testing::TestWithParam<std::string> {};

TEST_P(MarksVector, TakesEachMarkWhereItBelongs) {
  const sde::Json items = read_json(vectors_root() / "marks" / GetParam() / "cases.json");
  for (const sde::Json& item : items.as_array()) {
    const sde::Json& mark = *item.find("mark");
    const std::optional<std::string> found =
        mark.is_null() ? std::nullopt : std::optional<std::string>(mark.as_string());
    const std::string why = item.find("why")->as_string();
    EXPECT_EQ(sde::owned_copy_marker(item.find("project_id")->as_string(),
                                     item.find("group")->as_string(),
                                     item.find("table")->as_string(), found),
              item.find("copy")->as_bool())
        << why;
    EXPECT_EQ(!sde::copy_marks(found), item.find("source")->as_bool()) << why;
  }
}

INSTANTIATE_TEST_SUITE_P(Marks, MarksVector, ::testing::ValuesIn(cases("marks")),
                         [](const auto& param_info) { return test_name(param_info.param); });

}  // namespace
