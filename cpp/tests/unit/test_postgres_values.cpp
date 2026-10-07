/// The PostgreSQL text codec without a server: what each value is sent as, and what each cell the
/// server can send is read as. The live tests send and read them through a real server.

#include <cmath>
#include <limits>
#include <string>
#include <vector>

#include <gtest/gtest.h>

#include "engines/postgres/values.hpp"
#include "sde/errors.hpp"
#include "sde/json.hpp"

namespace {

using namespace sde::detail::postgres;

TEST(PostgresValues, EachValueIsSentAsTextTheServerReads) {
  EXPECT_EQ(parameter_text(sde::Null{}), std::nullopt);
  EXPECT_EQ(parameter_text(true), "true");
  EXPECT_EQ(parameter_text(false), "false");
  EXPECT_EQ(parameter_text(std::int64_t{-9223372036854775807 - 1}), "-9223372036854775808");
  EXPECT_EQ(parameter_text(0.1), "0.1");
  EXPECT_EQ(parameter_text(1e300), "1e+300");
  EXPECT_EQ(parameter_text(std::numeric_limits<double>::quiet_NaN()), "NaN");
  EXPECT_EQ(parameter_text(-std::numeric_limits<double>::infinity()), "-Infinity");
  EXPECT_EQ(parameter_text(sde::Decimal("-12.50")), "-12.50");
  EXPECT_EQ(parameter_text(sde::Decimal("1e3")), "1000");
  EXPECT_EQ(parameter_text(std::string("Zürich")), "Zürich");
  EXPECT_EQ(parameter_text(sde::Bytes{{0x00, 0xff, 0x10}}), "\\x00ff10");
  EXPECT_EQ(parameter_text(*sde::Uuid::parse("6BA7B810-9DAD-11D1-80B4-00C04FD430C8")),
            "6ba7b810-9dad-11d1-80b4-00c04fd430c8");
  EXPECT_EQ(parameter_text(*sde::Date::parse("0001-01-01")), "0001-01-01");
  EXPECT_EQ(parameter_text(*sde::Timestamp::parse("2026-10-07T12:00:00.5")),
            "2026-10-07T12:00:00.500000");
  EXPECT_EQ(parameter_text(*sde::TimestampTz::parse("2026-10-07T12:00:00+02:00")),
            "2026-10-07T10:00:00.000000Z");
  EXPECT_EQ(parameter_text(sde::JsonDocument{sde::parse_json(R"({"a": [1, "x"]})")}),
            R"({"a":[1,"x"]})");
}

TEST(PostgresValues, TextWithANulIsRefusedRatherThanCut) {
  // libpq reads a parameter as a C string, so the server would store the text up to the NUL.
  EXPECT_THROW((void)parameter_text(std::string("a\0b", 3)), sde::EngineError);
}

TEST(PostgresValues, EachCellIsReadAsTheTypeTheServerSent) {
  EXPECT_EQ(cell_value("t", oid::kBool), sde::Value(true));
  EXPECT_EQ(cell_value("-32768", oid::kInt2), sde::Value(std::int64_t{-32768}));
  EXPECT_EQ(cell_value("9223372036854775807", oid::kInt8),
            sde::Value(std::int64_t{9223372036854775807}));
  EXPECT_EQ(cell_value("1.1", oid::kFloat4), sde::Value(1.1));
  EXPECT_EQ(cell_value("-Infinity", oid::kFloat8),
            sde::Value(-std::numeric_limits<double>::infinity()));
  EXPECT_TRUE(std::isnan(std::get<double>(cell_value("NaN", oid::kFloat8))));
  EXPECT_EQ(cell_value("12.50", oid::kNumeric), sde::Value(sde::Decimal("12.50")));
  EXPECT_EQ(cell_value("\\x00ff", oid::kBytea), sde::Value(sde::Bytes{{0x00, 0xff}}));
  EXPECT_EQ(cell_value("6ba7b810-9dad-11d1-80b4-00c04fd430c8", oid::kUuid),
            sde::Value(*sde::Uuid::parse("6ba7b810-9dad-11d1-80b4-00c04fd430c8")));
  EXPECT_EQ(cell_value("2024-02-29", oid::kDate), sde::Value(*sde::Date::parse("2024-02-29")));
  EXPECT_EQ(cell_value("2026-10-07 12:00:00.123456", oid::kTimestamp),
            sde::Value(*sde::Timestamp::parse("2026-10-07T12:00:00.123456")));
  EXPECT_EQ(cell_value(R"({"a": 1})", oid::kJsonb),
            sde::Value(sde::JsonDocument{sde::parse_json(R"({"a":1})")}));
  EXPECT_EQ(cell_value("plain text", 25), sde::Value(std::string("plain text")));
}

TEST(PostgresValues, AnInstantIsReadWhateverOffsetTheSessionWritesItWith) {
  // The ISO style writes a whole-hour offset as `+HH`, others with minutes, and a historic zone's
  // local mean time with seconds.
  const sde::Value noon = *sde::TimestampTz::parse("2026-10-07T12:00:00Z");
  EXPECT_EQ(cell_value("2026-10-07 14:00:00+02", oid::kTimestamptz), noon);
  EXPECT_EQ(cell_value("2026-10-07 06:30:00-05:30", oid::kTimestamptz), noon);
  EXPECT_EQ(cell_value("2026-10-07 12:00:00+00", oid::kTimestamptz), noon);
  EXPECT_EQ(cell_value("1900-01-01 01:24:00+01:24", oid::kTimestamptz),
            sde::Value(*sde::TimestampTz::parse("1900-01-01T00:00:00Z")));
  EXPECT_EQ(cell_value("2026-10-07 12:00:00.000001+00", oid::kTimestamptz),
            sde::Value(*sde::TimestampTz::parse("2026-10-07T12:00:00.000001Z")));
}

TEST(PostgresValues, AValueThisLibraryDoesNotRepresentIsRefused) {
  for (const auto& [text, type] : std::vector<std::pair<std::string, unsigned>>{
           {"infinity", oid::kDate},
           {"-infinity", oid::kTimestamptz},
           {"0044-03-15 BC", oid::kDate},
           {"10000-01-01", oid::kDate},
           {"NaN", oid::kNumeric},
           {"Infinity", oid::kNumeric},
           {"\\000", oid::kBytea},
           {"maybe", oid::kBool},
           {"1.5", oid::kInt8}}) {
    EXPECT_THROW((void)cell_value(text, type), sde::EngineError) << text;
  }
}

TEST(PostgresValues, AnArrayLiteralQuotesEveryElement) {
  EXPECT_EQ(text_array({}), "{}");
  EXPECT_EQ(text_array({"plain", "with space", "q\"uote", "back\\slash", "", "NULL"}),
            R"({"plain","with space","q\"uote","back\\slash","","NULL"})");
}

}  // namespace
