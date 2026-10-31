/// A value as the Python object it is in the reference: its `repr`, `str`, type name and `int()`,
/// each captured from CPython 3.12 for the same value. The orderbook adapter writes a refused value
/// with `repr` and reads a key the way the reference does, with `str()` and `int()`; a `json`
/// field's document is the object `json.loads` makes of it.

#include <cstdint>
#include <limits>
#include <string>
#include <vector>

#include <gtest/gtest.h>

#include "python_compat.hpp"
#include "sde/errors.hpp"
#include "sde/json.hpp"
#include "sde/value.hpp"

namespace {

sde::Value document(const char* json) { return sde::Value(sde::JsonDocument{sde::parse_json(json)}); }

struct Case {
  sde::Value value;
  const char* repr;
  const char* str;
  const char* type;
  bool integer;         ///< whether int() answers
  const char* int_out;  ///< its answer, or the message Python raises
};

const std::vector<Case>& cases() {
  static const std::vector<Case> all = {
    {sde::Value(), "None", "None", "NoneType", false, "int() argument must be a string, a bytes-like object or a real number, not 'NoneType'"},
    {sde::Value(true), "True", "True", "bool", true, "1"},
    {sde::Value(false), "False", "False", "bool", true, "0"},
    {sde::Value(std::int64_t{0}), "0", "0", "int", true, "0"},
    {sde::Value(std::int64_t{-9223372036854775807 - 1}), "-9223372036854775808", "-9223372036854775808", "int", true, "-9223372036854775808"},
    {sde::Value(std::int64_t{1000}), "1000", "1000", "int", true, "1000"},
    {sde::Value(2.5), "2.5", "2.5", "float", true, "2"},
    {sde::Value(-0.0), "-0.0", "-0.0", "float", true, "0"},
    {sde::Value(1e16), "1e+16", "1e+16", "float", true, "10000000000000000"},
    {sde::Value(1e300), "1e+300", "1e+300", "float", true, "1000000000000000052504760255204420248704468581108159154915854115511802457988908195786371375080447864043704443832883878176942523235360430575644792184786706982848387200926575803737830233794788090059368953234970799945081119038967640880074652742780142494579258788820056842838115669472196386865459400540160"},
    {sde::Value(0.1), "0.1", "0.1", "float", true, "0"},
    {sde::Value(-1e-5), "-1e-05", "-1e-05", "float", true, "0"},
    {sde::Value(123456789.75), "123456789.75", "123456789.75", "float", true, "123456789"},
    {sde::Value(std::numeric_limits<double>::infinity()), "inf", "inf", "float", false, "cannot convert float infinity to integer"},
    {sde::Value(std::numeric_limits<double>::quiet_NaN()), "nan", "nan", "float", false, "cannot convert float NaN to integer"},
    {sde::Value(sde::Decimal("1.50")), "Decimal('1.50')", "1.50", "Decimal", true, "1"},
    {sde::Value(sde::Decimal("-0.5")), "Decimal('-0.5')", "-0.5", "Decimal", true, "0"},
    {sde::Value(sde::Decimal("0.0000001")), "Decimal('1E-7')", "1E-7", "Decimal", true, "0"},
    {sde::Value(sde::Decimal("0.000001")), "Decimal('0.000001')", "0.000001", "Decimal", true, "0"},
    {sde::Value(sde::Decimal("-0.00000012")), "Decimal('-1.2E-7')", "-1.2E-7", "Decimal", true, "0"},
    {sde::Value(sde::Decimal("1e3")), "Decimal('1000')", "1000", "Decimal", true, "1000"},
    {sde::Value(sde::Decimal("0.00")), "Decimal('0.00')", "0.00", "Decimal", true, "0"},
    {sde::Value(sde::Decimal("-0")), "Decimal('-0')", "-0", "Decimal", true, "0"},
    {sde::Value(sde::Decimal("123456.789")), "Decimal('123456.789')", "123456.789", "Decimal", true, "123456"},
    {sde::Value(sde::Decimal("99.99")), "Decimal('99.99')", "99.99", "Decimal", true, "99"},
    {sde::Value(std::string("bid")), "'bid'", "bid", "str", false, "invalid literal for int() with base 10: 'bid'"},
    {sde::Value(std::string("it's")), "\"it's\"", "it's", "str", false, "invalid literal for int() with base 10: \"it's\""},
    {sde::Value(std::string("12")), "'12'", "12", "str", true, "12"},
    {sde::Value(std::string(" -12_3 ")), "' -12_3 '", " -12_3 ", "str", true, "-123"},
    {sde::Value(std::string("x")), "'x'", "x", "str", false, "invalid literal for int() with base 10: 'x'"},
    {sde::Value(std::string("")), "''", "", "str", false, "invalid literal for int() with base 10: ''"},
    {sde::Value(std::string("\xd9\xa3")), "'\xd9\xa3'", "\xd9\xa3", "str", true, "3"},
    {sde::Value(std::string(201, 'y')), "'yyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy'", "yyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy", "str", false, "invalid literal for int() with base 10: 'yyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy"},
    {sde::Value(sde::Bytes{{0x00, 0x27, 0x41, 0x7f, 0xff}}), "b\"\\x00'A\\x7f\\xff\"", "b\"\\x00'A\\x7f\\xff\"", "bytes", false, "invalid literal for int() with base 10: b\"\\x00'A\\x7f\\xff\""},
    {sde::Value(sde::Bytes{{0x31, 0x32}}), "b'12'", "b'12'", "bytes", true, "12"},
    {sde::Value(sde::Bytes{{0x20, 0x31, 0x0a}}), "b' 1\\n'", "b' 1\\n'", "bytes", true, "1"},
    {sde::Value(sde::Bytes{{0xd9, 0xa3}}), "b'\\xd9\\xa3'", "b'\\xd9\\xa3'", "bytes", false, "invalid literal for int() with base 10: b'\\xd9\\xa3'"},
    {sde::Value(sde::Bytes{{0x27, 0x22}}), "b'\\'\"'", "b'\\'\"'", "bytes", false, "invalid literal for int() with base 10: b'\\'\"'"},
    {sde::Value(sde::Uuid("12345678-1234-5678-1234-567812345678")), "UUID('12345678-1234-5678-1234-567812345678')", "12345678-1234-5678-1234-567812345678", "UUID", true, "24197857161011715162171839636988778104"},
    {sde::Value(sde::Uuid("00000000-0000-0000-0000-000000000000")), "UUID('00000000-0000-0000-0000-000000000000')", "00000000-0000-0000-0000-000000000000", "UUID", true, "0"},
    {sde::Value(sde::Uuid("FFFFFFFF-FFFF-FFFF-FFFF-FFFFFFFFFFFF")), "UUID('ffffffff-ffff-ffff-ffff-ffffffffffff')", "ffffffff-ffff-ffff-ffff-ffffffffffff", "UUID", true, "340282366920938463463374607431768211455"},
    {sde::Value(sde::Date("2026-10-07")), "datetime.date(2026, 10, 7)", "2026-10-07", "date", false, "int() argument must be a string, a bytes-like object or a real number, not 'datetime.date'"},
    {sde::Value(sde::Date("0001-01-01")), "datetime.date(1, 1, 1)", "0001-01-01", "date", false, "int() argument must be a string, a bytes-like object or a real number, not 'datetime.date'"},
    {sde::Value(sde::Timestamp("2026-10-07T12:00:00")), "datetime.datetime(2026, 10, 7, 12, 0)", "2026-10-07 12:00:00", "datetime", false, "int() argument must be a string, a bytes-like object or a real number, not 'datetime.datetime'"},
    {sde::Value(sde::Timestamp("2026-10-07T12:00:05")), "datetime.datetime(2026, 10, 7, 12, 0, 5)", "2026-10-07 12:00:05", "datetime", false, "int() argument must be a string, a bytes-like object or a real number, not 'datetime.datetime'"},
    {sde::Value(sde::Timestamp("2026-10-07T00:00:00.000001")), "datetime.datetime(2026, 10, 7, 0, 0, 0, 1)", "2026-10-07 00:00:00.000001", "datetime", false, "int() argument must be a string, a bytes-like object or a real number, not 'datetime.datetime'"},
    {sde::Value(sde::TimestampTz("2026-10-07T12:34:56.5Z")), "datetime.datetime(2026, 10, 7, 12, 34, 56, 500000, tzinfo=datetime.timezone.utc)", "2026-10-07 12:34:56.500000+00:00", "datetime", false, "int() argument must be a string, a bytes-like object or a real number, not 'datetime.datetime'"},
    {sde::Value(sde::TimestampTz("0001-01-01T00:00:00Z")), "datetime.datetime(1, 1, 1, 0, 0, tzinfo=datetime.timezone.utc)", "0001-01-01 00:00:00+00:00", "datetime", false, "int() argument must be a string, a bytes-like object or a real number, not 'datetime.datetime'"},
    {document("\"ask\""), "'ask'", "ask", "str", false, "invalid literal for int() with base 10: 'ask'"},
    {document("5"), "5", "5", "int", true, "5"},
    {document("-0"), "0", "0", "int", true, "0"},
    {document("2.75"), "2.75", "2.75", "float", true, "2"},
    {document("1e3"), "1000.0", "1000.0", "float", true, "1000"},
    {document("null"), "None", "None", "NoneType", false, "int() argument must be a string, a bytes-like object or a real number, not 'NoneType'"},
    {document("true"), "True", "True", "bool", true, "1"},
    {document("[1, \"a\"]"), "[1, 'a']", "[1, 'a']", "list", false, "int() argument must be a string, a bytes-like object or a real number, not 'list'"},
    {document("{\"k\": 2.5}"), "{'k': 2.5}", "{'k': 2.5}", "dict", false, "int() argument must be a string, a bytes-like object or a real number, not 'dict'"},
    {document("123456789012345678901234567890"), "123456789012345678901234567890", "123456789012345678901234567890", "int", true, "123456789012345678901234567890"},
    {document("\"9_9\""), "'9_9'", "9_9", "str", true, "99"},
  };
  return all;
}

TEST(PythonValue, ReprStrTypeAndIntAreCPythons) {
  for (const Case& expected : cases()) {
    SCOPED_TRACE(expected.repr);
    EXPECT_EQ(sde::detail::python_value_repr(expected.value), expected.repr);
    EXPECT_EQ(sde::detail::python_value_str(expected.value), expected.str);
    EXPECT_EQ(sde::detail::python_value_type_name(expected.value), expected.type);
    try {
      const auto got = sde::detail::python_value_int(expected.value);
      EXPECT_TRUE(expected.integer) << "int() answered " << got.digits;
      EXPECT_EQ((got.negative ? "-" : "") + got.digits, expected.int_out);
    } catch (const sde::EngineError& error) {
      EXPECT_FALSE(expected.integer);
      EXPECT_EQ(std::string(error.what()), expected.int_out);
    }
  }
}

TEST(PythonValue, AnIntegerFitsAnInt64OnlyWithinItsRange) {
  using sde::detail::to_int64;
  EXPECT_EQ(to_int64({false, "9223372036854775807"}), INT64_MAX);
  EXPECT_EQ(to_int64({true, "9223372036854775808"}), INT64_MIN);
  EXPECT_EQ(to_int64({false, "9223372036854775808"}), std::nullopt);
  EXPECT_EQ(to_int64({true, "9223372036854775809"}), std::nullopt);
  EXPECT_EQ(to_int64({false, "18446744073709551616"}), std::nullopt);
  EXPECT_EQ(to_int64({false, "0"}), 0);
}

}  // namespace
