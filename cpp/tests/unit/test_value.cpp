/// The host values of Tier 2: exact decimals, UUIDs, dates and timestamps, read and written the way
/// the reference's types read and write them.

#include <chrono>
#include <cstdint>
#include <limits>
#include <stdexcept>
#include <string>
#include <tuple>
#include <type_traits>
#include <variant>
#include <vector>

#include <gtest/gtest.h>

#include "sde/value.hpp"
#include "value_internal.hpp"

namespace {

// A string literal is a string value, never a JSON document or a bool: the reason a JSON value is
// wrapped rather than held as `Json` directly.
static_assert(std::is_same_v<std::variant_alternative_t<5, sde::Value>, std::string>);

TEST(Value, LiteralsChooseTheObviousAlternative) {
  EXPECT_TRUE(std::holds_alternative<std::string>(sde::Value("text")));
  EXPECT_TRUE(std::holds_alternative<std::int64_t>(sde::Value(5)));
  EXPECT_TRUE(std::holds_alternative<bool>(sde::Value(true)));
  EXPECT_TRUE(std::holds_alternative<double>(sde::Value(2.5)));
  EXPECT_TRUE(sde::is_null(sde::Value{}));
  EXPECT_EQ(sde::value_kind(sde::Value(sde::Decimal("1.5"))), "Decimal");
  EXPECT_EQ(sde::value_kind(sde::Value(sde::JsonDocument{sde::Json("x")})), "JsonDocument");
}

TEST(Value, ARowIsOrderedByCodePoint) {
  sde::Row row{{"\xc3\xa9", 1}, {"z", 2}, {"a", 3}, {"\xf0\x9f\x98\x80", 4}};
  std::vector<std::string> names;
  for (const auto& [name, unused] : row) names.push_back(name);
  EXPECT_EQ(names, (std::vector<std::string>{"a", "z", "\xc3\xa9", "\xf0\x9f\x98\x80"}));
}

// Decimal text in, `format(Decimal(text), "f")` out, from CPython.
TEST(Decimal, ReadsAndWritesAsPythonFormatsIt) {
  const std::vector<std::pair<std::string, std::string>> cases = {
      {"0", "0"},           {"-0", "-0"},         {"+5", "5"},         {".5", "0.5"},
      {"5.", "5"},          {"-.5", "-0.5"},      {"1.50", "1.50"},    {"00012.50", "12.50"},
      {"0.000", "0.000"},   {"-0.000", "-0.000"}, {"1e3", "1000"},     {"1E+3", "1000"},
      {"1e-3", "0.001"},    {"0e5", "0"},         {"0.000e5", "0"},    {"5.0e1", "50"},
      {"100e-2", "1.00"},   {"-0E+1", "-0"},      {"0.05e1", "0.5"},   {"12.345e-10", "0.0000000012345"},
      {"9223372036854775808", "9223372036854775808"}};
  for (const auto& [text, fixed] : cases) {
    EXPECT_EQ(sde::Decimal(text).to_string(), fixed) << text;
  }
}

TEST(Decimal, RefusesWhatIsNotADecimal) {
  // Wider than Python's constructor on purpose: no whitespace, no NaN or Infinity, no underscores
  // and no digits outside ASCII. The read planner strips and checks text the reference's way before
  // it gets here.
  for (const std::string text : {"", "+", "-", ".", "1e", "1e+", "1.2.3", "--1", "0x10", "1 0", " 1",
                                 "1 ", "NaN", "Infinity", "1_000", "\xd9\xa1\xd9\xa2", "1e1001"}) {
    EXPECT_FALSE(sde::Decimal::parse(text).has_value()) << text;
    EXPECT_THROW(sde::Decimal{text}, std::invalid_argument) << text;
  }
  // The limit is digits, not characters: a thousand digits fit and a thousand and one do not.
  EXPECT_TRUE(sde::Decimal::parse("1e999").has_value());
  EXPECT_TRUE(sde::Decimal::parse("0." + std::string(999, '1')).has_value());
  EXPECT_FALSE(sde::Decimal::parse("0." + std::string(1001, '1')).has_value());
}

TEST(Decimal, ComparesByValue) {
  EXPECT_EQ(sde::Decimal("1.10"), sde::Decimal("1.1"));
  EXPECT_EQ(sde::Decimal("-0"), sde::Decimal("0.000"));
  EXPECT_LT(sde::Decimal("-2"), sde::Decimal("-1.5"));
  EXPECT_LT(sde::Decimal("-0.001"), sde::Decimal("0"));
  EXPECT_LT(sde::Decimal("9.99"), sde::Decimal("10"));
  EXPECT_GT(sde::Decimal("100"), sde::Decimal("99.999"));
  EXPECT_NE(sde::Decimal("0.1"), sde::Decimal("0.01"));
  // The text keeps the scale it was given: equal numbers, different columns' worth of digits.
  EXPECT_EQ(sde::Decimal("1.10").to_string(), "1.10");
}

TEST(Decimal, FromIntegersIncludingTheMostNegative) {
  EXPECT_EQ(sde::Decimal(std::int64_t{0}).to_string(), "0");
  EXPECT_EQ(sde::Decimal(std::int64_t{-42}).to_string(), "-42");
  EXPECT_EQ(sde::Decimal(std::numeric_limits<std::int64_t>::min()).to_string(),
            "-9223372036854775808");
  EXPECT_EQ(sde::Decimal(std::int64_t{1}).scale(), 0);
}

TEST(Decimal, CountsItsDigitsAsPythonDoes) {
  EXPECT_EQ(sde::Decimal("0.5").integer_digits(), 0);
  EXPECT_EQ(sde::Decimal("12.50").integer_digits(), 2);
  EXPECT_EQ(sde::Decimal("12.50").scale(), 2);
  EXPECT_EQ(sde::Decimal("1e3").integer_digits(), 4);
  EXPECT_EQ(sde::Decimal("0").integer_digits(), 1);
  EXPECT_EQ(sde::Decimal("0.000").integer_digits(), 0);
}

TEST(DecimalCounts, MatchCPythonsExponents) {
  // as_tuple().exponent and adjusted() from CPython for the same text.
  const std::vector<std::tuple<std::string, std::int64_t, std::int64_t>> cases = {
      {"0012.50", -2, 1}, {"0.05", -2, -2}, {"0.000", -3, -3}, {"0", 0, 0},
      {"1.5e3", 2, 3},    {"0e5", 5, 5},    {"0.0e1000000000000000000", 999999999999999999,
                                             999999999999999999}};
  for (const auto& [text, exponent, adjusted] : cases) {
    const auto parts = sde::detail::decimal_parts(text);
    ASSERT_TRUE(parts.has_value()) << text;
    const auto counts = sde::detail::decimal_counts(*parts);
    EXPECT_EQ(counts.exponent, exponent) << text;
    EXPECT_EQ(counts.adjusted, adjusted) << text;
  }
}

TEST(DecimalCounts, CPythonsLimitsOnConstruction) {
  // Measured on CPython 3.12: the first of each pair builds, the second raises InvalidOperation.
  const auto invalid = [](std::string_view text) {
    return sde::detail::python_decimal_invalid(
        sde::detail::decimal_counts(*sde::detail::decimal_parts(text)));
  };
  EXPECT_FALSE(invalid("1e999999999999999999"));
  EXPECT_TRUE(invalid("1e1000000000000000000"));
  EXPECT_FALSE(invalid("0.5e1000000000000000000"));
  EXPECT_TRUE(invalid("5e1000000000000000000"));
  EXPECT_FALSE(invalid("1e-1999999999999999997"));
  EXPECT_TRUE(invalid("1e-1999999999999999998"));
  EXPECT_TRUE(invalid("12e-1999999999999999998"));
  EXPECT_TRUE(invalid("1e9223372036854775807"));
  EXPECT_TRUE(invalid("1e-99999999999999999999999999"));
  EXPECT_FALSE(invalid("1e00000000000000000000000001"));
}

TEST(Uuid, CanonicalTextEitherCaseAndLowerOut) {
  const sde::Uuid id("FFFFFFFF-FFFF-FFFF-0000-00000000000A");
  EXPECT_EQ(id.to_string(), "ffffffff-ffff-ffff-0000-00000000000a");
  EXPECT_EQ(id.bytes()[15], 0x0A);
  EXPECT_EQ(sde::Uuid("00112233-4455-6677-8899-aabbccddeeff"),
            sde::Uuid("00112233-4455-6677-8899-AABBCCDDEEFF"));
  EXPECT_LT(sde::Uuid("00000000-0000-0000-0000-000000000001"),
            sde::Uuid("10000000-0000-0000-0000-000000000000"));
  for (const std::string text :
       {"{00112233-4455-6677-8899-aabbccddeeff}", "00112233445566778899aabbccddeeff",
        "00112233-4455-6677-8899-aabbccddeef", "00112233-4455-6677-8899-aabbccddeefg",
        "urn:uuid:00112233-4455-6677-8899-aabbccddeeff", "0011223-34455-6677-8899-aabbccddeeff"}) {
    EXPECT_FALSE(sde::Uuid::parse(text).has_value()) << text;
  }
}

TEST(Bytes, HexBothWays) {
  const sde::Bytes bytes = *sde::Bytes::from_hex("0001fF80");
  EXPECT_EQ(bytes.data, (std::vector<std::uint8_t>{0x00, 0x01, 0xFF, 0x80}));
  EXPECT_EQ(bytes.to_hex(), "0001ff80");
  EXPECT_FALSE(sde::Bytes::from_hex("abc").has_value());
  EXPECT_FALSE(sde::Bytes::from_hex("zz").has_value());
  EXPECT_TRUE(sde::Bytes::from_hex("")->data.empty());
  // Unsigned order, as a byte string sorts.
  EXPECT_LT(*sde::Bytes::from_hex("7f"), *sde::Bytes::from_hex("80"));
}

// Days from 1970-01-01, from CPython's `date.toordinal()`.
TEST(Date, DaysAgreeWithPythonsCalendar) {
  const std::vector<std::tuple<int, unsigned, unsigned, std::int64_t>> anchors = {
      {1, 1, 1, -719162},       {1, 12, 31, -718798},    {4, 2, 29, -718008},
      {100, 3, 1, -682944},     {400, 2, 29, -573372},   {1582, 10, 15, -141427},
      {1899, 12, 31, -25568},   {1969, 12, 31, -1},      {1970, 1, 1, 0},
      {2000, 2, 29, 11016},     {2026, 10, 6, 20732},    {9999, 12, 31, 2932896}};
  for (const auto& [year, month, day, days] : anchors) {
    EXPECT_EQ(sde::Date(year, month, day).days(), days) << year << "-" << month << "-" << day;
  }
}

TEST(Date, EveryDayOfTheRangeRoundTripsThroughItsText) {
  for (std::int64_t days = sde::detail::kMinDays; days <= sde::detail::kMaxDays; ++days) {
    const auto date = sde::Date::from_days(days);
    ASSERT_TRUE(date.has_value()) << days;
    const auto again = sde::Date::parse(date->to_string());
    ASSERT_TRUE(again.has_value()) << date->to_string();
    ASSERT_EQ(again->days(), days) << date->to_string();
  }
  EXPECT_FALSE(sde::Date::from_days(sde::detail::kMinDays - 1).has_value());
  EXPECT_FALSE(sde::Date::from_days(sde::detail::kMaxDays + 1).has_value());
}

TEST(Date, RefusesDatesThatDoNotExist) {
  EXPECT_TRUE(sde::Date::parse("2024-02-29").has_value());
  for (const std::string text : {"2026-02-29", "1900-02-29", "0000-12-31", "2026-13-01", "2026-04-31",
                                 "2026-00-10", "2026-9-14", "2026-09-14T00:00:00", " 2026-09-14",
                                 "\xd9\xa2\xd9\xa0\xd9\xa2\xd9\xa6-09-14", "+2026-09-14"}) {
    EXPECT_FALSE(sde::Date::parse(text).has_value()) << text;
  }
  EXPECT_THROW(sde::Date(10000, 1, 1), std::invalid_argument);
  EXPECT_THROW(sde::Date(2026, 2, 29), std::invalid_argument);
}

TEST(Timestamp, ReadsWallClockTextWithoutAZone) {
  EXPECT_EQ(sde::Timestamp("2026-09-14T10:00:00.5").to_string(), "2026-09-14T10:00:00.500000");
  EXPECT_EQ(sde::Timestamp("2026-09-14 10:00:00").to_string(), "2026-09-14T10:00:00.000000");
  EXPECT_EQ(sde::Timestamp("1965-09-14T12:00:00.000001").to_string(), "1965-09-14T12:00:00.000001");
  EXPECT_EQ(sde::Timestamp("1969-12-31T23:59:59.999999").micros(), -1);
  // An offset makes it an instant, which is the other type.
  EXPECT_FALSE(sde::Timestamp::parse("2026-09-14T10:00:00Z").has_value());
  EXPECT_FALSE(sde::Timestamp::parse("2026-09-14T10:00:00+02:00").has_value());
}

TEST(TimestampTz, ReadsTheInstantAsPythonDoes) {
  EXPECT_EQ(sde::TimestampTz("2026-09-14T12:00:00.123456+02:00").to_string(),
            "2026-09-14T10:00:00.123456Z");
  EXPECT_EQ(sde::TimestampTz("2026-09-14T10:00:00+23:59:59").to_string(),
            "2026-09-13T10:00:01.000000Z");
  EXPECT_EQ(sde::TimestampTz("2026-09-14T10:00:00+05:30:15").to_string(),
            "2026-09-14T04:29:45.000000Z");
  EXPECT_EQ(sde::TimestampTz("2026-09-14T10:00:00-00:00").to_string(),
            "2026-09-14T10:00:00.000000Z");
  // No zone is read as UTC, as the reference reads a naive datetime given for an instant.
  EXPECT_EQ(sde::TimestampTz("2026-09-14T10:00:00").to_string(), "2026-09-14T10:00:00.000000Z");
  EXPECT_EQ(sde::TimestampTz("0001-01-01T00:00:00-00:01").to_string(), "0001-01-01T00:01:00.000000Z");
  EXPECT_EQ(sde::TimestampTz("9999-12-31T23:59:59.999999Z").micros(), sde::detail::kMaxMicros);
  EXPECT_EQ(sde::TimestampTz("0001-01-01T00:00:00Z").micros(), sde::detail::kMinMicros);
  for (const std::string text :
       {"2026-09-14T24:00:00", "2026-09-14T23:59:60", "2026-09-14T10:00:00+24:00",
        "2026-09-14T10:00:00.1234567", "2026-09-14T10:00", "2026-09-14T10:00:00z",
        "2026-09-14t10:00:00", "2026-09-14T10:00:00 ", "2026-09-14T10:00:00.",
        "2026-09-14T10:00:00+0200", "2026-09-14T10:00:00+05:30:15.5", "0000-01-01T00:00:00",
        "0001-01-01T00:00:00+00:01", "9999-12-31T23:59:59-00:01", "2026-02-29T00:00:00"}) {
    EXPECT_FALSE(sde::TimestampTz::parse(text).has_value()) << text;
  }
}

TEST(TimestampTz, FromTheSystemClock) {
  using std::chrono::microseconds;
  const auto point = std::chrono::sys_days{std::chrono::year{2026} / 10 / 6} + microseconds{1};
  const sde::TimestampTz instant{std::chrono::time_point_cast<microseconds>(point)};
  EXPECT_EQ(instant.to_string(), "2026-10-06T00:00:00.000001Z");
  EXPECT_EQ(instant.time(), point);
  EXPECT_THROW(sde::TimestampTz{std::chrono::sys_time<microseconds>{microseconds{sde::detail::kMaxMicros + 1}}},
               std::invalid_argument);
}

}  // namespace
