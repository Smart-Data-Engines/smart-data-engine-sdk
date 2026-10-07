/// ClickHouse's catalogue read into names rather than compared as text we predict: the reference's
/// cases (`test_physical.py`), and the parentheses ClickHouse 26.5 and later keep around a single
/// expression written in them - `ORDER BY (id)` reads back `(id)` there and `id` on 26.4 and every
/// release before it, measured on 24.8, 25.3, 25.8, 25.12, 26.3, 26.4, 26.5, 26.6 and 26.9. Text the
/// parsers do not understand stays a refusal: an expression they cannot read is not evidence that a
/// table matches.

#include <optional>
#include <string>
#include <vector>

#include <gtest/gtest.h>

#include "physical_internal.hpp"

namespace {

using sde::detail::parse_identifier_list;
using sde::detail::parse_partition_key;
using Names = std::vector<std::string>;

TEST(PhysicalCatalogue, NamesAreParsedNotPredicted) {
  const std::vector<std::pair<std::string, Names>> cases = {
      {"station, at", {"station", "at"}},
      {"at", {"at"}},
      // ClickHouse 24.8 leaves reserved words bare when they match the bare pattern - measured.
      {"select, order", {"select", "order"}},
      // ... and quotes `null` and anything outside the pattern, with `\` escaping `\` and the tick.
      {"`null`, `z\xC4\x85" "b`", {"null", "z\xC4\x85" "b"}},
      {"`a b`, c", {"a b", "c"}},
      {"`tick\\`tock`", {"tick`tock"}},
      {"`back\\\\slash`", {"back\\slash"}},
      {"", {}},
  };
  for (const auto& [text, names] : cases) {
    SCOPED_TRACE(text);
    EXPECT_EQ(parse_identifier_list(text), std::optional<Names>(names));
  }
}

TEST(PhysicalCatalogue, ASingleExpressionInParenthesesIsTheSameKeyFromClickHouse265On) {
  const std::vector<std::pair<std::string, Names>> cases = {
      {"(id)", {"id"}},
      {"(`a b`)", {"a b"}},
      {"(`null`)", {"null"}},
      {"(`a)`)", {"a)"}},  // a parenthesis inside a quoted name is the name's
      {"(a, b)", {"a", "b"}},
  };
  for (const auto& [text, names] : cases) {
    SCOPED_TRACE(text);
    EXPECT_EQ(parse_identifier_list(text), std::optional<Names>(names));
  }
}

TEST(PhysicalCatalogue, TextTheParserDoesNotUnderstandIsStillNoList) {
  for (const char* text : {"`unterminated", "`dangling\\", "a,b", "a, ", "a, , b",
                           "toYYYYMM(at)", "1abc",
                           // one pair, around the whole, and a list inside it - nothing else
                           "()", "((id))", "(a) + (b)", "(id", "id)", "(a,b)", "(toYYYYMM(at))"}) {
    SCOPED_TRACE(text);
    EXPECT_EQ(parse_identifier_list(text), std::nullopt);
  }
}

TEST(PhysicalCatalogue, PartitionKeysAreOneFunctionOfOneNameInOrOutOfOnePairOfParentheses) {
  const auto none = parse_partition_key("");
  ASSERT_TRUE(none.has_value()) << "an empty key is a table with no partition, not an unreadable one";
  EXPECT_FALSE(none->has_value());
  const auto read = [](const char* text) -> std::optional<std::pair<std::string, std::string>> {
    const auto key = parse_partition_key(text);
    if (!key || !*key) return std::nullopt;
    return std::make_pair((*key)->function, (*key)->field);
  };
  EXPECT_EQ(read("toYYYYMM(at)"), std::make_pair(std::string("toYYYYMM"), std::string("at")));
  EXPECT_EQ(read("toDate(`posted day`)"), std::make_pair(std::string("toDate"), std::string("posted day")));
  EXPECT_EQ(read("(toYYYYMM(at))"), std::make_pair(std::string("toYYYYMM"), std::string("at")));
  for (const char* text : {"at", "(at)", "toYYYYMM(toDate(at))", "toYYYYMM(at, 'UTC')", "toYYYYMM(a, b)",
                           "((toYYYYMM(at)))", "(a)(b)", "toYYYYMM((at))"}) {
    SCOPED_TRACE(text);
    EXPECT_FALSE(parse_partition_key(text).has_value());
  }
}

}  // namespace
