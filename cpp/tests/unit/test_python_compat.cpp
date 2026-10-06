/// The reference's spelling of values and its reading of JSON numbers. Each expectation here was
/// produced by Python 3.12 (`repr`, `float`, `json_numbers`); a differential run over a hundred
/// thousand generated values agreed with all of them, and these are the cases worth naming.

#include <cmath>
#include <limits>

#include <gtest/gtest.h>

#include "python_compat.hpp"
#include "sde/json.hpp"

namespace {

using sde::Json;
using sde::detail::integral_double_digits;
using sde::detail::integral_numbers;
using sde::detail::python_float;
using sde::detail::python_float_repr;
using sde::detail::python_repr;
using sde::detail::python_truthy;
using sde::detail::python_type_name;

TEST(PythonRepr, QuotesAsPythonChooses) {
  EXPECT_EQ(python_repr("Order"), "'Order'");
  EXPECT_EQ(python_repr("it's"), "\"it's\"");
  EXPECT_EQ(python_repr("say \"hi\""), "'say \"hi\"'");
  EXPECT_EQ(python_repr("both ' and \""), "'both \\' and \"'");
  EXPECT_EQ(python_repr(""), "''");
}

TEST(PythonRepr, EscapesWhatPythonDoesNotPrint) {
  EXPECT_EQ(python_repr("a\\b"), "'a\\\\b'");
  EXPECT_EQ(python_repr("\n\t\r"), "'\\n\\t\\r'");
  EXPECT_EQ(python_repr(std::string_view("\0\x1f\x7f", 3)), "'\\x00\\x1f\\x7f'");
  EXPECT_EQ(python_repr("\xc2\xa0"), "'\\xa0'");            // U+00A0, a separator
  EXPECT_EQ(python_repr("\xc2\xad"), "'\\xad'");            // U+00AD, a format character
  EXPECT_EQ(python_repr("\xe2\x80\x8b"), "'\\u200b'");      // U+200B
  EXPECT_EQ(python_repr("\xe2\x80\xa8"), "'\\u2028'");      // U+2028, a line separator
  EXPECT_EQ(python_repr("\xf3\xa0\x80\x81"), "'\\U000e0001'");  // U+E0001, a tag
  EXPECT_EQ(python_repr("\xed\xa0\x80"), "'\\ud800'");      // a lone surrogate the parser kept
}

TEST(PythonRepr, PrintsWhatPythonPrints) {
  EXPECT_EQ(python_repr("Zamówienie"), "'Zamówienie'");
  EXPECT_EQ(python_repr("e\xcc\x81"), "'e\xcc\x81'");  // a combining mark is printable
  EXPECT_EQ(python_repr("\xf0\x9f\x98\x80"), "'\xf0\x9f\x98\x80'");
}

TEST(PythonRepr, ValuesAsJsonLoadsMakesThem) {
  EXPECT_EQ(python_repr(Json(nullptr)), "None");
  EXPECT_EQ(python_repr(Json(true)), "True");
  EXPECT_EQ(python_repr(Json(false)), "False");
  EXPECT_EQ(python_repr(Json::from_lexeme("42")), "42");
  EXPECT_EQ(python_repr(Json::from_lexeme("-0")), "0");
  EXPECT_EQ(python_repr(Json::from_lexeme("4.50")), "4.5");
  EXPECT_EQ(python_repr(Json::from_lexeme("1e400")), "inf");
  EXPECT_EQ(python_repr(sde::parse_json(R"([1, "a", null])")), "[1, 'a', None]");
  EXPECT_EQ(python_repr(sde::parse_json(R"({"k": 2.5, "b": []})")), "{'k': 2.5, 'b': []}");
  EXPECT_EQ(python_repr(std::vector<std::string>{"a", "b"}), "['a', 'b']");
  EXPECT_EQ(python_repr(std::vector<std::string>{}), "[]");
}

TEST(PythonFloatRepr, FixedBetweenTenToTheMinusFourAndSixteen) {
  EXPECT_EQ(python_float_repr(1e16), "1e+16");
  EXPECT_EQ(python_float_repr(1e15), "1000000000000000.0");
  EXPECT_EQ(python_float_repr(0.0001), "0.0001");
  EXPECT_EQ(python_float_repr(0.00001), "1e-05");
  EXPECT_EQ(python_float_repr(1.5e-5), "1.5e-05");
  EXPECT_EQ(python_float_repr(1e100), "1e+100");
  EXPECT_EQ(python_float_repr(123456789012345678.0), "1.2345678901234568e+17");
  EXPECT_EQ(python_float_repr(0.5), "0.5");
  EXPECT_EQ(python_float_repr(100.0), "100.0");
  EXPECT_EQ(python_float_repr(0.0), "0.0");
  EXPECT_EQ(python_float_repr(-0.0), "-0.0");
  EXPECT_EQ(python_float_repr(0.1), "0.1");
  EXPECT_EQ(python_float_repr(std::numeric_limits<double>::infinity()), "inf");
  EXPECT_EQ(python_float_repr(-std::numeric_limits<double>::infinity()), "-inf");
}

TEST(PythonFloat, OutOfRangeIsInfiniteAboveAndZeroBelow) {
  EXPECT_EQ(python_float("1e400"), std::numeric_limits<double>::infinity());
  EXPECT_EQ(python_float("-1.8e308"), -std::numeric_limits<double>::infinity());
  EXPECT_EQ(python_float("1e-400"), 0.0);
  EXPECT_TRUE(std::signbit(python_float("-2e-324")));
  EXPECT_EQ(python_float("0.00000e999999"), 0.0);
  EXPECT_EQ(python_float("4.9e-324"), 4.9e-324);  // the smallest subnormal is in range
  EXPECT_EQ(python_float("2.5"), 2.5);
}

TEST(IntegralDoubleDigits, ExactAtAnySize) {
  EXPECT_EQ(integral_double_digits(2.0), "2");
  EXPECT_EQ(integral_double_digits(-0.0), "0");
  EXPECT_EQ(integral_double_digits(9007199254740993.0), "9007199254740992");
  EXPECT_EQ(integral_double_digits(9223372036854775808.0), "9223372036854775808");
  EXPECT_EQ(integral_double_digits(1e20), "100000000000000000000");
  EXPECT_EQ(integral_double_digits(-1e22), "-10000000000000000000000");
  EXPECT_EQ(integral_double_digits(1e23), "99999999999999991611392");
}

TEST(IntegralNumbers, IntegralSpellingsBecomeIntegers) {
  const Json document = sde::parse_json(
      R"({"a": 2.0, "b": [2e0, 20e-1, 1.5, -0.0, 7], "c": {"d": 1e400, "e": 4.5e1}})");
  const Json normal = integral_numbers(document);
  EXPECT_EQ(sde::dump_json(normal), R"({"a":2,"b":[2,2,1.5,0,7],"c":{"d":1e400,"e":45}})");
  EXPECT_TRUE(normal.find("a")->as_number().integer);
  EXPECT_FALSE(normal.find("b")->as_array()[2].as_number().integer);
  // Members keep their order and their keys; nothing but numbers changes.
  EXPECT_EQ(normal.as_object()[1].first, "b");
}

TEST(PythonTruthy, AsBoolDecides) {
  for (const char* falsy : {"null", "false", "0", "-0", "0.0", "0e9", "1e-400", "\"\"", "[]", "{}"}) {
    EXPECT_FALSE(python_truthy(sde::parse_json(falsy))) << falsy;
  }
  for (const char* truthy : {"true", "1", "-1", "0.5", "1e400", "\"0\"", "[0]", "{\"a\":0}"}) {
    EXPECT_TRUE(python_truthy(sde::parse_json(truthy))) << truthy;
  }
}

TEST(PythonTypeName, AsTypeNameSays) {
  EXPECT_EQ(python_type_name(Json(nullptr)), "NoneType");
  EXPECT_EQ(python_type_name(Json(true)), "bool");
  EXPECT_EQ(python_type_name(Json::from_lexeme("1")), "int");
  EXPECT_EQ(python_type_name(Json::from_lexeme("1.0")), "float");
  EXPECT_EQ(python_type_name(Json("x")), "str");
  EXPECT_EQ(python_type_name(Json::array()), "list");
  EXPECT_EQ(python_type_name(Json::object()), "dict");
}

}  // namespace
