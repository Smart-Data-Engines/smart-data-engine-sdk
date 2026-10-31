/// `int(text)` of a `str`, as CPython 3.12 reads it - how the engine's own client reads every
/// number in an orderbook server's answer, and so what the adapter reads them with. Every expected
/// outcome was captured from CPython: Unicode digits and spaces, C's whitespace against Python's
/// (U+001C to U+001F are neither stripped nor digits), underscores only between digits, and the
/// 4300-digit limit, which is checked only once the text is otherwise a number.

#include <string>
#include <vector>

#include <gtest/gtest.h>

#include "python_url.hpp"

namespace {

using sde::detail::python_url::int_of;
using sde::detail::python_url::PythonInt;
using sde::detail::python_url::PythonIntLimit;
using sde::detail::python_url::PythonValueError;

enum class Kind { value, invalid, limit };

struct Case {
  std::string text;
  Kind kind;
  bool negative;
  std::string expected;  ///< the digits, or the digit count of a text past the limit
};

std::vector<Case> cases() {
  std::vector<Case> out = {
    {std::string("0", 1), Kind::value, false, "0"},
    {std::string("-0", 2), Kind::value, false, "0"},
    {std::string("+0", 2), Kind::value, false, "0"},
    {std::string("007", 3), Kind::value, false, "7"},
    {std::string("-007", 4), Kind::value, true, "7"},
    {std::string("12345678901234567890123", 23), Kind::value, false, "12345678901234567890123"},
    {std::string("1_000", 5), Kind::value, false, "1000"},
    {std::string("1__0", 4), Kind::invalid, false, ""},
    {std::string("_1", 2), Kind::invalid, false, ""},
    {std::string("1_", 2), Kind::invalid, false, ""},
    {std::string("+_1", 3), Kind::invalid, false, ""},
    {std::string("-1_0", 4), Kind::value, true, "10"},
    {std::string(" 12 ", 4), Kind::value, false, "12"},
    {std::string("\x09""12\x0a", 4), Kind::value, false, "12"},
    {std::string("\x0b""12\x0c", 4), Kind::value, false, "12"},
    {std::string("\x0d""12", 3), Kind::value, false, "12"},
    {std::string("\x1c""12", 3), Kind::invalid, false, ""},
    {std::string("12\x1f", 3), Kind::invalid, false, ""},
    {std::string("\xe3\x80\x80""12\xe3\x80\x80", 8), Kind::value, false, "12"},
    {std::string("\xc2\x85""12", 4), Kind::value, false, "12"},
    {std::string("12\xc2\x85", 4), Kind::value, false, "12"},
    {std::string("\xc2\xa0-1", 4), Kind::value, true, "1"},
    {std::string("\xd9\xa3", 2), Kind::value, false, "3"},
    {std::string("\xd9\xa1\xd9\xa2", 4), Kind::value, false, "12"},
    {std::string("1\xd9\xa2", 3), Kind::value, false, "12"},
    {std::string("\xf0\x9d\x9f\x8f", 4), Kind::value, false, "1"},
    {std::string("\xc2\xb2", 2), Kind::invalid, false, ""},
    {std::string("1.0", 3), Kind::invalid, false, ""},
    {std::string("1e3", 3), Kind::invalid, false, ""},
    {std::string("0x10", 4), Kind::invalid, false, ""},
    {std::string("", 0), Kind::invalid, false, ""},
    {std::string(" ", 1), Kind::invalid, false, ""},
    {std::string("+", 1), Kind::invalid, false, ""},
    {std::string("-", 1), Kind::invalid, false, ""},
    {std::string("--1", 3), Kind::invalid, false, ""},
    {std::string("+-1", 3), Kind::invalid, false, ""},
    {std::string("1 2", 3), Kind::invalid, false, ""},
    {std::string("1\x00", 2), Kind::invalid, false, ""},
    {std::string("\x00""2", 2), Kind::invalid, false, ""},
    {std::string("\xd9\xa1_\xd9\xa2", 5), Kind::value, false, "12"},
    {std::string("1_\xd9\xa2", 4), Kind::value, false, "12"},
    {std::string("inf", 3), Kind::invalid, false, ""},
    {std::string("nan", 3), Kind::invalid, false, ""},
    {std::string("\xef\xbc\x91\xef\xbc\x92", 6), Kind::value, false, "12"},
    {std::string("\xe2\x80\xa8""7", 4), Kind::value, false, "7"},
    {std::string("7\xe2\x80\xa9", 4), Kind::value, false, "7"},
    {std::string("\xe1\xa0\x8e""7", 4), Kind::invalid, false, ""},
    {std::string("\xef\xbb\xbf""7", 4), Kind::invalid, false, ""},
    {std::string("\xe2\x80\x8b""7", 4), Kind::invalid, false, ""},
    {std::string("7\xe2\x81\x9f", 4), Kind::value, false, "7"},
    {std::string("\x7f""7", 2), Kind::invalid, false, ""},
    {std::string("7\x7f", 2), Kind::invalid, false, ""},
  };
  // The long ones, built rather than written out.
  out.push_back({std::string(4300, '9'), Kind::value, false, std::string(4300, '9')});
  out.push_back({std::string(4301, '9'), Kind::limit, false, "4301"});
  out.push_back({std::string(4301, '0'), Kind::limit, false, "4301"});
  out.push_back({"-" + std::string(4301, '1'), Kind::limit, false, "4301"});
  std::string underscored;
  for (int i = 0; i < 4300; ++i) underscored += "1_";
  out.push_back({underscored + "1", Kind::limit, false, "4301"});
  out.push_back({std::string(4300, '1') + "x", Kind::invalid, false, ""});
  out.push_back({" " + std::string(4301, '5') + "x", Kind::invalid, false, ""});
  return out;
}

TEST(PythonInt, EveryTextIsReadAsCPythonReadsIt) {
  for (const Case& expected : cases()) {
    SCOPED_TRACE(expected.text.size() < 80 ? expected.text : expected.text.substr(0, 80) + "...");
    try {
      const PythonInt got = int_of(expected.text);
      EXPECT_EQ(expected.kind, Kind::value) << "accepted as " << got.digits;
      EXPECT_EQ(got.negative, expected.negative);
      EXPECT_EQ(got.digits, expected.expected);
    } catch (const PythonValueError&) {
      EXPECT_EQ(expected.kind, Kind::invalid);
    } catch (const PythonIntLimit& limit) {
      EXPECT_EQ(expected.kind, Kind::limit);
      EXPECT_EQ(std::to_string(limit.digits), expected.expected);
    }
  }
}

}  // namespace
