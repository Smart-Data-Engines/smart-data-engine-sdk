/// The read planner and the exact summary against the reference's own answers: each table below was
/// produced by running `sde.query` of the reference on the same inputs (refusals start with `!`).

#include <array>
#include <cstdint>
#include <limits>
#include <string>
#include <utility>
#include <vector>

#include <gtest/gtest.h>

#include "sde/errors.hpp"
#include "sde/query.hpp"

namespace {

// The normalised value as the `query/` vectors write it.
std::string written(const sde::Value& value) {
  if (sde::is_null(value)) return "null";
  if (const auto* decimal = std::get_if<sde::Decimal>(&value)) return decimal->to_string();
  if (const auto* naive = std::get_if<sde::Timestamp>(&value)) return naive->to_string() + "Z";
  if (const auto* instant = std::get_if<sde::TimestampTz>(&value)) return instant->to_string();
  if (const auto* uuid = std::get_if<sde::Uuid>(&value)) return uuid->to_string();
  if (const auto* date = std::get_if<sde::Date>(&value)) return date->to_string();
  if (const auto* text = std::get_if<std::string>(&value)) return *text;
  if (const auto* integer = std::get_if<std::int64_t>(&value)) return std::to_string(*integer);
  return "<" + std::string(sde::value_kind(value)) + ">";
}

std::string outcome(const sde::ReadColumn& column, const sde::Value& value) {
  try {
    return written(sde::query_value(column, value));
  } catch (const sde::QueryRefused& refusal) {
    return std::string("!") + refusal.what();
  }
}

// decimal(12,2): sde.query.query_value of the reference, text in, normalised text or "!" + refusal out
const std::vector<std::pair<std::string, std::string>> kDecimalQueries = {
    {"0", "0"},
    {"-0", "-0"},
    {"+5", "5"},
    {".5", "0.5"},
    {"5.", "5"},
    {"-.5", "-0.5"},
    {"1.50", "1.50"},
    {"00012.50", "12.50"},
    {"0.000", "0.000"},
    {"-0.000", "-0.000"},
    {"1e3", "1000"},
    {"1E+3", "1000"},
    {"1e-3", "0.001"},
    {"0e5", "0"},
    {"0.000e5", "0"},
    {"5.0e1", "50"},
    {"100e-2", "1.00"},
    {"  7.25\t", "7.25"},
    {"\u00a01.5\u3000", "1.5"},
    {"\u20051\u2028", "1"},
    {"1_000", "!invalid decimal query value"},
    {"\u0661\u0662", "!invalid decimal query value"},
    {"NaN", "!invalid decimal query value"},
    {"Infinity", "!invalid decimal query value"},
    {"", "!invalid decimal query value"},
    {"+", "!invalid decimal query value"},
    {".", "!invalid decimal query value"},
    {"1e", "!invalid decimal query value"},
    {"1e+", "!invalid decimal query value"},
    {"1.2.3", "!invalid decimal query value"},
    {"--1", "!invalid decimal query value"},
    {"0x10", "!invalid decimal query value"},
    {"1 0", "!invalid decimal query value"},
    {"1111111111111111111111111111111111111111111111111111111111111111111111111111", "1111111111111111111111111111111111111111111111111111111111111111111111111111"},
    {"11111111111111111111111111111111111111111111111111111111111111111111111111111", "!decimal query values may use at most 76 digits"},
    {"0.1111111111111111111111111111111111111111111111111111111111111111111111111111", "0.1111111111111111111111111111111111111111111111111111111111111111111111111111"},
    {"0.11111111111111111111111111111111111111111111111111111111111111111111111111111", "!decimal query values may use at most 76 digits"},
    {"1e75", "1000000000000000000000000000000000000000000000000000000000000000000000000000"},
    {"1e76", "!decimal query values may use at most 76 digits"},
    {"0e-76", "0.0000000000000000000000000000000000000000000000000000000000000000000000000000"},
    {"0e-77", "!decimal query values may use at most 76 digits"},
    {"1e-76", "0.0000000000000000000000000000000000000000000000000000000000000000000000000001"},
    {"1e-77", "!decimal query values may use at most 76 digits"},
    {"1e999999999999999999", "!decimal query values may use at most 76 digits"},
    {"1e1000000000000000000", "!invalid decimal query value"},
    {"0e999999999999999999", "!decimal query values may use at most 76 digits"},
    {"0e1000000000000000000", "!invalid decimal query value"},
    {"0.0e1000000000000000000", "!decimal query values may use at most 76 digits"},
    {"1e-1999999999999999997", "!decimal query values may use at most 76 digits"},
    {"1e-1999999999999999998", "!invalid decimal query value"},
    {"1e9223372036854775807", "!invalid decimal query value"},
    {"1e00000000000000000000000001", "10"},
    {"-12345678901234567890.123456789", "-12345678901234567890.123456789"},
    {"123e999999999999999998", "!invalid decimal query value"},
    {"0.5e1000000000000000000", "!decimal query values may use at most 76 digits"},
};
// timestamptz: sde.query.query_value of the reference, text in, normalised text or "!" + refusal out
const std::vector<std::pair<std::string, std::string>> kTimestampTzQueries = {
    {"2026-09-14T12:00:00.123456+02:00", "2026-09-14T10:00:00.123456Z"},
    {"2026-09-14T10:00:00.123457Z", "2026-09-14T10:00:00.123457Z"},
    {"2026-09-14 10:00:00", "2026-09-14T10:00:00.000000Z"},
    {"2026-09-14T10:00:00.5", "2026-09-14T10:00:00.500000Z"},
    {"2026-09-14T10:00:00.1234567", "!timestamp query text needs at most six fractional digits"},
    {"2026-09-14T10:00", "!timestamp query text needs at most six fractional digits"},
    {"2026-09-14T24:00:00", "!invalid timestamp query value"},
    {"2026-09-14T23:59:60", "!invalid timestamp query value"},
    {"2026-02-29T00:00:00", "!invalid timestamp query value"},
    {"2024-02-29T00:00:00", "2024-02-29T00:00:00.000000Z"},
    {"2026-13-01T00:00:00", "!invalid timestamp query value"},
    {"2026-09-14T10:00:00+24:00", "!invalid timestamp query value"},
    {"2026-09-14T10:00:00+23:59:59", "2026-09-13T10:00:01.000000Z"},
    {"2026-09-14T10:00:00-00:00", "2026-09-14T10:00:00.000000Z"},
    {"2026-09-14T10:00:00+05:30:15", "2026-09-14T04:29:45.000000Z"},
    {"2026-09-14T10:00:00+05:30:15.5", "!timestamp query text needs at most six fractional digits"},
    {"2026-09-14T10:00:00z", "!timestamp query text needs at most six fractional digits"},
    {"2026-09-14t10:00:00", "!timestamp query text needs at most six fractional digits"},
    {"0001-01-01T00:00:00+00:01", "!timestamp query value is outside UTC years 0001 through 9999"},
    {"0001-01-01T00:00:00-00:01", "0001-01-01T00:01:00.000000Z"},
    {"9999-12-31T23:59:59.999999Z", "9999-12-31T23:59:59.999999Z"},
    {"9999-12-31T23:59:59-00:01", "!timestamp query value is outside UTC years 0001 through 9999"},
    {"1965-09-14T12:00:00.123456+02:00", "1965-09-14T10:00:00.123456Z"},
    {"\u0662\u0660\u0662\u0666-09-14T10:00:00", "!invalid timestamp query value"},
    {"2026-09-14T10:00:00 ", "!timestamp query text needs at most six fractional digits"},
    {" 2026-09-14T10:00:00", "!timestamp query text needs at most six fractional digits"},
    {"2026-09-14T10:00:00.", "!timestamp query text needs at most six fractional digits"},
    {"0000-01-01T00:00:00", "!invalid timestamp query value"},
    {"2026-09-14T10:00:00+0200", "!timestamp query text needs at most six fractional digits"},
};
// timestamp: sde.query.query_value of the reference, text in, normalised text or "!" + refusal out
const std::vector<std::pair<std::string, std::string>> kTimestampQueries = {
    {"2026-09-14T12:00:00.123456+02:00", "2026-09-14T10:00:00.123456Z"},
    {"2026-09-14T10:00:00.123457Z", "2026-09-14T10:00:00.123457Z"},
    {"2026-09-14 10:00:00", "2026-09-14T10:00:00.000000Z"},
    {"2026-09-14T10:00:00.5", "2026-09-14T10:00:00.500000Z"},
    {"2026-09-14T10:00:00.1234567", "!timestamp query text needs at most six fractional digits"},
    {"2026-09-14T10:00", "!timestamp query text needs at most six fractional digits"},
    {"2026-09-14T24:00:00", "!invalid timestamp query value"},
    {"2026-09-14T23:59:60", "!invalid timestamp query value"},
    {"2026-02-29T00:00:00", "!invalid timestamp query value"},
    {"2024-02-29T00:00:00", "2024-02-29T00:00:00.000000Z"},
    {"2026-13-01T00:00:00", "!invalid timestamp query value"},
    {"2026-09-14T10:00:00+24:00", "!invalid timestamp query value"},
    {"2026-09-14T10:00:00+23:59:59", "2026-09-13T10:00:01.000000Z"},
    {"2026-09-14T10:00:00-00:00", "2026-09-14T10:00:00.000000Z"},
    {"2026-09-14T10:00:00+05:30:15", "2026-09-14T04:29:45.000000Z"},
    {"2026-09-14T10:00:00+05:30:15.5", "!timestamp query text needs at most six fractional digits"},
    {"2026-09-14T10:00:00z", "!timestamp query text needs at most six fractional digits"},
    {"2026-09-14t10:00:00", "!timestamp query text needs at most six fractional digits"},
    {"0001-01-01T00:00:00+00:01", "!timestamp query value is outside UTC years 0001 through 9999"},
    {"0001-01-01T00:00:00-00:01", "0001-01-01T00:01:00.000000Z"},
    {"9999-12-31T23:59:59.999999Z", "9999-12-31T23:59:59.999999Z"},
    {"9999-12-31T23:59:59-00:01", "!timestamp query value is outside UTC years 0001 through 9999"},
    {"1965-09-14T12:00:00.123456+02:00", "1965-09-14T10:00:00.123456Z"},
    {"\u0662\u0660\u0662\u0666-09-14T10:00:00", "!invalid timestamp query value"},
    {"2026-09-14T10:00:00 ", "!timestamp query text needs at most six fractional digits"},
    {" 2026-09-14T10:00:00", "!timestamp query text needs at most six fractional digits"},
    {"2026-09-14T10:00:00.", "!timestamp query text needs at most six fractional digits"},
    {"0000-01-01T00:00:00", "!invalid timestamp query value"},
    {"2026-09-14T10:00:00+0200", "!timestamp query text needs at most six fractional digits"},
};
// date: sde.query.query_value of the reference, text in, normalised text or "!" + refusal out
const std::vector<std::pair<std::string, std::string>> kDateQueries = {
    {"2026-09-14", "2026-09-14"},
    {"2024-02-29", "2024-02-29"},
    {"2026-02-29", "!a date query value must be a date or YYYY-MM-DD text"},
    {"0001-01-01", "0001-01-01"},
    {"9999-12-31", "9999-12-31"},
    {"0000-12-31", "!a date query value must be a date or YYYY-MM-DD text"},
    {"2026-9-14", "!a date query value must be a date or YYYY-MM-DD text"},
    {"\u0662\u0660\u0662\u0666-09-14", "!a date query value must be a date or YYYY-MM-DD text"},
    {"2026-09-14T00:00:00", "!a date query value must be a date or YYYY-MM-DD text"},
    {" 2026-09-14", "!a date query value must be a date or YYYY-MM-DD text"},
};
// uuid: sde.query.query_value of the reference, text in, normalised text or "!" + refusal out
const std::vector<std::pair<std::string, std::string>> kUuidQueries = {
    {"FFFFFFFF-FFFF-FFFF-0000-000000000000", "ffffffff-ffff-ffff-0000-000000000000"},
    {"00112233-4455-6677-8899-aabbccddeeff", "00112233-4455-6677-8899-aabbccddeeff"},
    {"{00112233-4455-6677-8899-aabbccddeeff}", "!a UUID query value must be UUID or canonical UUID text"},
    {"00112233445566778899aabbccddeeff", "!a UUID query value must be UUID or canonical UUID text"},
    {"00112233-4455-6677-8899-aabbccddeef", "!a UUID query value must be UUID or canonical UUID text"},
    {"00112233-4455-6677-8899-aabbccddeefg", "!a UUID query value must be UUID or canonical UUID text"},
    {"urn:uuid:00112233-4455-6677-8899-aabbccddeeff", "!a UUID query value must be UUID or canonical UUID text"},
};
void check(const char* type, const std::vector<std::pair<std::string, std::string>>& table) {
  const sde::ReadColumn column{"f", type};
  for (const auto& [text, want] : table) {
    EXPECT_EQ(outcome(column, sde::Value(text)), want) << type << " <- \"" << text << "\"";
  }
}

TEST(QueryValue, DecimalTextAsTheReferenceReadsIt) { check("decimal(12,2)", kDecimalQueries); }
TEST(QueryValue, InstantTextAsTheReferenceReadsIt) { check("timestamptz", kTimestampTzQueries); }
TEST(QueryValue, WallClockTextAsTheReferenceReadsIt) { check("timestamp", kTimestampQueries); }
TEST(QueryValue, DateTextAsTheReferenceReadsIt) { check("date", kDateQueries); }
TEST(QueryValue, UuidTextAsTheReferenceReadsIt) { check("uuid", kUuidQueries); }

TEST(QueryValue, EachTypeRefusesTheWrongKindByName) {
  const auto refused = [](const char* type, const sde::Value& value) {
    return outcome(sde::ReadColumn{"f", type}, value);
  };
  EXPECT_EQ(refused("decimal(12,2)", true), "!decimal query values require Decimal, integer or decimal text");
  EXPECT_EQ(refused("decimal(12,2)", 1.5), "!decimal query values require Decimal, integer or decimal text");
  EXPECT_EQ(refused("bool", std::int64_t{1}), "!a boolean query value must be a bool");
  EXPECT_EQ(refused("int64", true), "!an integer query value must be an integer");
  EXPECT_EQ(refused("int64", 1.0), "!an integer query value must be an integer");
  EXPECT_EQ(refused("int32", std::int64_t{2147483648}), "!integer query value is outside int32");
  EXPECT_EQ(refused("int32", std::int64_t{-2147483649}), "!integer query value is outside int32");
  EXPECT_EQ(refused("float64", true), "!a float query value must be a finite number");
  EXPECT_EQ(refused("float64", std::numeric_limits<double>::infinity()), "!float query bounds must be finite");
  EXPECT_EQ(refused("float64", std::numeric_limits<double>::quiet_NaN()), "!float query bounds must be finite");
  EXPECT_EQ(refused("string", std::int64_t{1}), "!a string query value must be text");
  EXPECT_EQ(refused("string", std::string("\xed\xa0\x80")), "!query strings must contain Unicode scalar values");
  EXPECT_EQ(refused("string", std::string("\xff")), "!query strings must contain Unicode scalar values");
  EXPECT_EQ(refused("uuid", std::int64_t{1}), "!a UUID query value must be UUID or canonical UUID text");
  EXPECT_EQ(refused("date", sde::Timestamp{}), "!a date query value must be a date or YYYY-MM-DD text");
  EXPECT_EQ(refused("timestamptz", sde::Date{}), "!a timestamp query value must be datetime or ISO text");
  EXPECT_EQ(refused("bytes", std::string("00")), "!query predicates are not supported for bytes");
  EXPECT_EQ(refused("json", std::string("{}")), "!query predicates are not supported for json");
}

TEST(QueryValue, AcceptsEachTypesHostValues) {
  const auto accepted = [](const char* type, const sde::Value& value) {
    return outcome(sde::ReadColumn{"f", type}, value);
  };
  EXPECT_EQ(accepted("decimal(12,2)", std::int64_t{-7}), "-7");
  EXPECT_EQ(accepted("decimal(12,2)", sde::Decimal("1.50")), "1.50");
  EXPECT_EQ(accepted("decimal(12,2)", sde::Decimal("1e75")), std::string("1") + std::string(75, '0'));
  EXPECT_EQ(accepted("decimal(12,2)", sde::Decimal("1e76")), "!decimal query values may use at most 76 digits");
  EXPECT_EQ(accepted("int32", std::int64_t{-2147483648}), "-2147483648");
  EXPECT_EQ(accepted("int32", std::int64_t{2147483647}), "2147483647");
  EXPECT_EQ(accepted("uuid", sde::Uuid("00112233-4455-6677-8899-AABBCCDDEEFF")),
            "00112233-4455-6677-8899-aabbccddeeff");
  EXPECT_EQ(accepted("date", sde::Date(2026, 10, 6)), "2026-10-06");
  // A wall-clock reading given for an instant is read as UTC, and an instant given for a wall-clock
  // column is its UTC reading.
  EXPECT_EQ(accepted("timestamptz", sde::Timestamp("2026-09-14T10:00:00")), "2026-09-14T10:00:00.000000Z");
  EXPECT_EQ(accepted("timestamp", sde::TimestampTz("2026-09-14T12:00:00+02:00")), "2026-09-14T10:00:00.000000Z");
  EXPECT_TRUE(std::holds_alternative<sde::Timestamp>(
      sde::query_value(sde::ReadColumn{"f", "timestamp"}, sde::TimestampTz{})));
  EXPECT_TRUE(std::holds_alternative<sde::TimestampTz>(
      sde::query_value(sde::ReadColumn{"f", "timestamptz"}, sde::Timestamp{})));
  // An integer bound of a float column becomes the nearest double.
  EXPECT_EQ(std::get<double>(sde::query_value(sde::ReadColumn{"f", "float64"},
                                              std::int64_t{9007199254740993})),
            9007199254740992.0);
  EXPECT_TRUE(sde::is_null(sde::query_value(sde::ReadColumn{"f", "json"}, sde::Null{})));
}

const std::vector<sde::ReadColumn> kColumns = {
    {"id", "int64"}, {"label", "string"}, {"at", "timestamptz"}, {"amount", "decimal(12,2)"},
    {"doc", "json"}, {"ratio", "float64"}, {"wide", "decimal(77,2)"}};

std::string refusal(const sde::ReadOptions& options, std::vector<std::string> key = {"id"}) {
  try {
    (void)sde::plan_read(kColumns, key, options);
  } catch (const sde::QueryRefused& refused) {
    return refused.what();
  }
  return "";
}

TEST(PlanRead, RefusesInTheReferencesOrder) {
  sde::ReadOptions options;
  options.limit = 0;
  EXPECT_EQ(refusal(options), "page limit must be an integer between 1 and 1000");
  options.limit = 1001;
  EXPECT_EQ(refusal(options), "page limit must be an integer between 1 and 1000");
  options.limit = std::uint64_t{18446744073709551615ULL};  // wraps to -1, never into range
  EXPECT_EQ(refusal(options), "page limit must be an integer between 1 and 1000");
  options.limit = 1000;
  EXPECT_EQ(refusal(options, {}), "a paginated read requires an entity key");
  std::vector<std::string> long_key;
  for (int i = 0; i < 33; ++i) long_key.push_back("id");
  EXPECT_EQ(refusal(options, long_key), "a read may order by at most 32 fields");
  options.order_by = "missing";
  EXPECT_EQ(refusal(options), "query refers to a field this entity does not declare");
  options.order_by = "doc";
  EXPECT_EQ(refusal(options), "JSON and floating-point ordering are not supported by this read API");
  options.order_by = "ratio";
  EXPECT_EQ(refusal(options), "JSON and floating-point ordering are not supported by this read API");
  options.order_by = "wide";
  EXPECT_EQ(refusal(options), "decimal ordering supports at most 76 digits");
  options.order_by.reset();
  options.where = sde::Row{{"doc", "x"}};
  EXPECT_EQ(refusal(options), "JSON predicates are not supported by this read API");
  options.where = sde::Row{{"nope", 1}};
  EXPECT_EQ(refusal(options), "query refers to a field this entity does not declare");
  options.where.reset();
  options.bounds = sde::Range{"label", "a", "b"};
  EXPECT_EQ(refusal(options), "this field has no range-read shape");
  options.bounds = sde::Range{"id", sde::Null{}, sde::Null{}};
  EXPECT_EQ(refusal(options), "a range needs at least one bound");
  options.bounds.reset();
  options.after = sde::Row{{"label", "x"}};
  EXPECT_EQ(refusal(options), "after must contain exactly the complete ordering key");
  options.paginate = false;
  EXPECT_EQ(refusal(options), "an aggregate has no pagination position");
}

TEST(PlanRead, AnAggregateIgnoresOrderAndKey) {
  sde::ReadOptions options;
  options.paginate = false;
  options.order_by = "doc";  // unread: an aggregate has no order
  const sde::ReadPlan plan = sde::plan_read(kColumns, {}, options);
  EXPECT_TRUE(plan.order.empty());
  EXPECT_FALSE(plan.after.has_value());
}

TEST(PlanRead, FiltersInNameOrderThenTheRange) {
  sde::ReadOptions options;
  options.where = sde::Row{{"label", "x"}, {"id", 5}, {"amount", sde::Null{}}};
  options.bounds = sde::Range{"at", sde::Null{}, "2026-09-14T10:00:00Z"};
  options.order_by = "label";
  options.descending = true;
  options.after = sde::Row{{"id", 7}, {"label", sde::Null{}}};
  options.limit = 25;
  const sde::ReadPlan plan = sde::plan_read(kColumns, {"id"}, options);
  ASSERT_EQ(plan.filters.size(), 4U);
  EXPECT_EQ(plan.filters[0].column.name, "amount");
  EXPECT_TRUE(sde::is_null(plan.filters[0].value));
  EXPECT_EQ(plan.filters[1].column.name, "id");
  EXPECT_EQ(plan.filters[2].column.name, "label");
  EXPECT_EQ(plan.filters[3].column.name, "at");
  EXPECT_EQ(sde::operation_name(plan.filters[3].operation), "lt");
  ASSERT_EQ(plan.order.size(), 2U);
  EXPECT_EQ(plan.order[0].name, "label");
  EXPECT_EQ(plan.order[1].name, "id");
  ASSERT_TRUE(plan.after.has_value());
  EXPECT_TRUE(sde::is_null((*plan.after)[0]));
  EXPECT_EQ(std::get<std::int64_t>((*plan.after)[1]), 7);
  EXPECT_TRUE(plan.descending);
  EXPECT_EQ(plan.limit, 25);
}

struct SummaryCase {
  std::string type;
  int mean_scale;
  std::array<std::string, 5> record;  // count, present, minimum, maximum, total
  std::string error;                  // "Class:message", empty for a result
  std::vector<std::string> expected;  // count, present, minimum, maximum, total, mean
};

// From the reference's `numeric_summary`. Its `ValueError` - an answer no engine could have given -
// is this library's `EngineError`, with the same message.
const std::vector<SummaryCase> kSummaries = {
    {"int64", 6, {"10", "7", "-5", "9", "13"}, "", {"10", "7", "-5", "9", "13", "1.857143"}},
    {"int64", 0, {"4", "4", "1", "1", "2"}, "", {"4", "4", "1", "1", "2", "0"}},
    {"int64", 0, {"4", "4", "1", "3", "6"}, "", {"4", "4", "1", "3", "6", "2"}},
    {"int64", 0, {"4", "4", "-3", "-1", "-6"}, "", {"4", "4", "-3", "-1", "-6", "-2"}},
    {"int64", 0, {"4", "4", "-1", "-1", "-2"}, "", {"4", "4", "-1", "-1", "-2", "-0"}},
    {"int64", 38, {"3", "3", "0", "1", "1"}, "", {"3", "3", "0", "1", "1", "0.33333333333333333333333333333333333333"}},
    {"int32", 6, {"3", "2", "-2147483648", "2147483647", "-1"}, "", {"3", "2", "-2147483648", "2147483647", "-1", "-0.500000"}},
    {"decimal(12,2)", 3, {"2", "2", "3.25", "5.25", "8.5"}, "", {"2", "2", "3.25", "5.25", "8.50", "4.250"}},
    {"decimal(12,2)", 0, {"2", "2", "-0.00", "0.00", "-0.00"}, "", {"2", "2", "0.00", "0.00", "0.00", "0"}},
    {"decimal(56,20)", 38, {"3", "3", "1.00000000000000000001", "99999999999999999999999999999999999.99999999999999999999", "111111111111111111111111111111111111.11111111111111111111"}, "", {"3", "3", "1.00000000000000000001", "99999999999999999999999999999999999.99999999999999999999", "111111111111111111111111111111111111.11111111111111111111", "37037037037037037037037037037037037.03703703703703703703666666666666666667"}},
    {"decimal(12,2)", 6, {"5", "5", "1.005", "2", "3"}, "ValueError:summary result has fractional digits outside the stored scale", {}},
    {"decimal(12,2)", 6, {"5", "5", "1", "2", "11111111111111111111111111111111111111111111111111111111111111111111111111111"}, "QueryRefused:decimal query values may use at most 76 digits", {}},
    {"int64", 6, {"2", "3", "1", "1", "1"}, "ValueError:summary counts are inconsistent", {}},
    {"int64", 6, {"-1", "0", "0", "0", "0"}, "ValueError:summary counts are inconsistent", {}},
    {"int64", 39, {"1", "1", "1", "1", "1"}, "QueryRefused:mean_scale must be an integer between 0 and 38", {}},
    {"float64", 6, {"1", "1", "1", "1", "1"}, "QueryRefused:summaries require int32, int64 or decimal precision up to 56", {}},
    {"decimal(57,2)", 6, {"1", "1", "1", "1", "1"}, "QueryRefused:summaries require int32, int64 or decimal precision up to 56", {}},
    {"int64", 6, {"5", "5", "1.5", "2", "3"}, "ValueError:summary result has fractional digits outside the stored scale", {}},
};

TEST(NumericSummary, AsTheReferenceComputesIt) {
  for (const SummaryCase& c : kSummaries) {
    SCOPED_TRACE(c.type + " at mean scale " + std::to_string(c.mean_scale) + ", total " + c.record[4]);
    const sde::SummaryRecord record{c.record[0], c.record[1], c.record[2], c.record[3], c.record[4]};
    const sde::ReadColumn column{"value", c.type};
    if (!c.error.empty()) {
      const std::string kind = c.error.substr(0, c.error.find(':'));
      const std::string message = c.error.substr(c.error.find(':') + 1);
      try {
        (void)sde::numeric_summary(record, column, c.mean_scale);
        ADD_FAILURE() << "no refusal; wanted " << c.error;
      } catch (const sde::QueryRefused& refused) {
        EXPECT_EQ(kind, "QueryRefused");
        EXPECT_EQ(refused.what(), message);
      } catch (const sde::EngineError& failed) {
        EXPECT_EQ(kind, "ValueError");
        EXPECT_EQ(failed.what(), message);
      }
      continue;
    }
    const sde::NumericSummary summary = sde::numeric_summary(record, column, c.mean_scale);
    EXPECT_EQ(std::to_string(summary.count), c.expected[0]);
    EXPECT_EQ(std::to_string(summary.non_null_count), c.expected[1]);
    EXPECT_EQ(summary.minimum->to_string(), c.expected[2]);
    EXPECT_EQ(summary.maximum->to_string(), c.expected[3]);
    EXPECT_EQ(summary.total->to_string(), c.expected[4]);
    EXPECT_EQ(summary.mean->to_string(), c.expected[5]);
  }
}

TEST(NumericSummary, NoValuesIsNoStatistics) {
  const sde::NumericSummary summary =
      sde::numeric_summary(sde::SummaryRecord{"3", "0", std::nullopt, std::nullopt, std::nullopt},
                           sde::ReadColumn{"value", "int64"}, 6);
  EXPECT_EQ(summary.count, 3U);
  EXPECT_EQ(summary.non_null_count, 0U);
  EXPECT_FALSE(summary.minimum || summary.maximum || summary.total || summary.mean);
}

TEST(NumericSummary, AnAnswerNoEngineCouldGiveIsTheEnginesError) {
  const sde::ReadColumn column{"value", "int64"};
  EXPECT_THROW((void)sde::numeric_summary(sde::SummaryRecord{"x", "1", "1", "1", "1"}, column, 6),
               sde::EngineError);
  EXPECT_THROW((void)sde::numeric_summary(sde::SummaryRecord{"18446744073709551616", "1", "1", "1", "1"},
                                          column, 6),
               sde::EngineError);
  // A missing aggregate where values were counted is refused like the reference's `_decimal(None)`.
  EXPECT_THROW((void)sde::numeric_summary(sde::SummaryRecord{"1", "1", std::nullopt, "1", "1"}, column, 6),
               sde::QueryRefused);
}

TEST(ReadRow, PutsDecimalsBackAtTheirScale) {
  const std::vector<sde::ReadColumn> columns = {{"amount", "decimal(12,2)"}, {"id", "int64"}};
  sde::Row row{{"amount", "8.5"}, {"id", 3}};
  row = sde::read_row(columns, row);
  EXPECT_EQ(std::get<sde::Decimal>(row.at("amount")).to_string(), "8.50");
  EXPECT_EQ(std::get<std::int64_t>(row.at("id")), 3);
  sde::Row negative_zero{{"amount", sde::Decimal("-0")}, {"id", 1}};
  EXPECT_EQ(std::get<sde::Decimal>(sde::read_row(columns, negative_zero).at("amount")).to_string(), "0.00");
  sde::Row null{{"amount", sde::Null{}}, {"id", 1}};
  EXPECT_TRUE(sde::is_null(sde::read_row(columns, null).at("amount")));
  sde::Row finer{{"amount", "1.005"}, {"id", 1}};
  EXPECT_THROW((void)sde::read_row(columns, finer), sde::EngineError);
  sde::Row missing{{"id", 1}};
  EXPECT_THROW((void)sde::read_row(columns, missing), sde::EngineError);
}

}  // namespace
