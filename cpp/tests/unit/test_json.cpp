#include <gtest/gtest.h>

#include "sde/json.hpp"
#include "sde/unicode.hpp"

namespace {

using sde::Json;
using sde::JsonError;
using sde::parse_json;

TEST(JsonParser, KeepsANumbersLexemeAndKind) {
  const Json value = parse_json(R"({"a": 9007199254740991, "b": 2.0, "c": -0, "d": 1e3})");
  EXPECT_EQ(value.find("a")->as_number().lexeme, "9007199254740991");
  EXPECT_TRUE(value.find("a")->as_number().integer);
  EXPECT_EQ(*value.find("a")->to_int64(), 9007199254740991);
  EXPECT_FALSE(value.find("b")->as_number().integer);
  EXPECT_FALSE(value.find("b")->to_int64().has_value());
  EXPECT_EQ(*value.find("c")->to_int64(), 0);
  EXPECT_FALSE(value.find("d")->as_number().integer);
  EXPECT_EQ(*value.find("d")->to_double(), 1000.0);
}

TEST(JsonParser, AnIntegerOutsideSixtyFourBitsIsNotRounded) {
  const Json value = parse_json("[9223372036854775807, 9223372036854775808]");
  EXPECT_EQ(*value.as_array()[0].to_int64(), INT64_MAX);
  EXPECT_FALSE(value.as_array()[1].to_int64().has_value());
}

TEST(JsonParser, KeysThatDifferOnlyInCompositionAreTwoMembers) {
  // "é" composed and "e" + U+0301: the canonical encoder refuses the pair after normalising
  // (canonical/008); a parser that normalised would hide it.
  const Json value = parse_json("{\"\xc3\xa9\": 1, \"e\xcc\x81\": 2}");
  EXPECT_EQ(value.as_object().size(), 2U);
}

TEST(JsonParser, AByteIdenticalDuplicateKeepsTheLastValueAtTheFirstPosition) {
  const Json value = parse_json(R"({"a": 1, "b": 2, "a": 3})");
  ASSERT_EQ(value.as_object().size(), 2U);
  EXPECT_EQ(value.as_object()[0].first, "a");
  EXPECT_EQ(*value.as_object()[0].second.to_int64(), 3);
}

TEST(JsonParser, DecodesEscapesAndSurrogatePairs) {
  const Json value = parse_json(R"(["\"\\\/\b\f\n\r\t", "\u00e9", "\ud83d\ude00"])");
  EXPECT_EQ(value.as_array()[0].as_string(), "\"\\/\b\f\n\r\t");
  EXPECT_EQ(value.as_array()[1].as_string(), "\xc3\xa9");
  EXPECT_EQ(value.as_array()[2].as_string(), "\xf0\x9f\x98\x80");
}

TEST(JsonParser, ALoneSurrogateSurvivesAsTextThatIsNotScalar) {
  // Refused later, by the map loader, with the contract's own class (errors/050, errors/051).
  const Json value = parse_json(R"(["\ud800", "\udc00x", "\ud800\u0041"])");
  for (const Json& item : value.as_array()) {
    EXPECT_FALSE(sde::is_scalar_text(item.as_string()));
  }
  EXPECT_EQ(value.as_array()[2].as_string().back(), 'A');
}

TEST(JsonParser, RefusesWhatIsNotStrictJson) {
  for (const char* text :
       {"", "{", "[1,]", "{\"a\":1,}", "// c\n1", "NaN", "Infinity", "01", "1.", ".5", "+1",
        "\"\x01\"", "'a'", "[1] 2", "\xef\xbb\xbf{}", "\"\\x\"", "\"\\u12g4\"", "tru", "\"\xff\""}) {
    EXPECT_THROW((void)parse_json(text), JsonError) << text;
  }
}

TEST(JsonParser, RefusesHostileNesting) {
  EXPECT_THROW((void)parse_json(std::string(100000, '[')), JsonError);
}

TEST(JsonWriter, IsCompactAndEscapesMinimally) {
  const Json value = parse_json(R"({"b": [1, "x\u0001/\u2028"], "a": null, "c": true})");
  EXPECT_EQ(sde::dump_json(value),
            "{\"b\":[1,\"x\\u0001/\xe2\x80\xa8\"],\"a\":null,\"c\":true}");
}

TEST(JsonValue, EqualityIsByValue) {
  EXPECT_EQ(parse_json(R"({"a": 1, "b": [2.50]})"), parse_json(R"({"b": [2.5], "a": 1})"));
  EXPECT_NE(parse_json("[1, 2]"), parse_json("[2, 1]"));
  EXPECT_EQ(Json(0.5), parse_json("0.5"));
  EXPECT_FALSE(Json(2.0).as_number().integer);
}

}  // namespace
