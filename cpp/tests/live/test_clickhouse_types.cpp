/// Every ClickHouse type this adapter reads and writes beyond the ones its own layouts create: a map
/// written by hand can name a table that holds them. Each expected value is what the reference's
/// adapter returned for the same row on the same server, and each write is read back by the
/// administrator as text, as the reference's own write of the same row reads back. Where the
/// reference changes a value - a fraction of a second cut, an enum name it does not know stored as
/// 0 - this library refuses, and the test says which.
///
///     SDE_CLICKHOUSE_DSN=clickhouse://default:sde@127.0.0.1:58123/sde ctest -L live

#include <cstdint>
#include <map>
#include <memory>
#include <string>
#include <utility>
#include <vector>

#include <gtest/gtest.h>

#include "live/capture.hpp"
#include "live/clickhouse.hpp"
#include "live/live.hpp"
#include "sde/clickhouse.hpp"
#include "sde/errors.hpp"
#include "sde/value.hpp"

namespace {

using sde::live::ClickHouseAdmin;
using sde::live::ClickHouseScope;
using sde::live::refusal;

struct Column {
  std::string name;
  std::string type;
  std::string literal;  ///< as the administrator writes it
};

/// The reference's table (`reference_types.py`): one column of each type, each at an edge.
const std::vector<Column> kColumns = {
    {"id", "Int64", "1"},
    {"i8", "Int8", "-128"},
    {"i16", "Int16", "-32768"},
    {"i32", "Int32", "-2147483648"},
    {"u8", "UInt8", "255"},
    {"u16", "UInt16", "65535"},
    {"u32", "UInt32", "4294967295"},
    {"u64", "UInt64", "9223372036854775807"},
    {"i128", "Int128", "-170141183460469231731687303715884105728"},
    {"u128", "UInt128", "340282366920938463463374607431768211455"},
    {"i256", "Int256",
     "-57896044618658097711785492504343953926634992332820282019728792003956564819968"},
    {"u256", "UInt256",
     "115792089237316195423570985008687907853269984665640564039457584007913129639935"},
    {"f32", "Float32", "1.1"},
    {"f64", "Float64", "-0.1"},
    {"b", "Bool", "true"},
    {"d32", "Decimal32(2)", "-9999999.99"},
    {"d64", "Decimal64(4)", "12345678901234.5678"},
    {"d128", "Decimal128(10)", "-1234567890123456789012345678.0123456789"},
    {"d256", "Decimal256(20)",
     "12345678901234567890123456789012345678901234567890.12345678901234567890"},
    {"dps", "Decimal(18, 0)", "-999999999999999999"},
    {"s", "String", "'naïve ✓'"},
    {"fs", "FixedString(4)", "'ab'"},
    {"lc", "LowCardinality(String)", "'low'"},
    {"n", "Nullable(Int32)", "NULL"},
    {"nn", "Nullable(String)", "'there'"},
    {"ln", "LowCardinality(Nullable(String))", "NULL"},
    {"u", "UUID", "'6ba7b810-9dad-11d1-80b4-00c04fd430c8'"},
    {"d", "Date", "'2149-06-06'"},
    {"d32x", "Date32", "'1900-01-01'"},
    {"dt", "DateTime('UTC')", "'2106-02-07 06:28:15'"},
    {"dtn", "DateTime", "'1970-01-01 00:00:01'"},
    {"dt64", "DateTime64(3, 'UTC')", "'2026-10-07 12:34:56.789'"},
    {"dt64n", "DateTime64(9)", "'2026-10-07 12:34:56.123456000'"},
    {"e8", "Enum8('a' = 1, 'b' = -2)", "'b'"},
    {"e16", "Enum16('x' = 1000, 'y\\'z' = -1000)", "'y\\'z'"},
};

/// What the reference read, as this library holds it: an integer past 64 bits is an exact decimal
/// here and a Python int there, and FixedString is bytes in both.
std::map<std::string, sde::Value> read_by_the_reference() {
  return {
      {"id", std::int64_t{1}},
      {"i8", std::int64_t{-128}},
      {"i16", std::int64_t{-32768}},
      {"i32", std::int64_t{-2147483648LL}},
      {"u8", std::int64_t{255}},
      {"u16", std::int64_t{65535}},
      {"u32", std::int64_t{4294967295LL}},
      {"u64", std::int64_t{9223372036854775807LL}},
      {"i128", sde::Decimal("-170141183460469231731687303715884105728")},
      {"u128", sde::Decimal("340282366920938463463374607431768211455")},
      {"i256",
       sde::Decimal("-57896044618658097711785492504343953926634992332820282019728792003956564819968")},
      {"u256",
       sde::Decimal("115792089237316195423570985008687907853269984665640564039457584007913129639935")},
      {"f32", 1.100000023841858},
      {"f64", -0.1},
      {"b", true},
      {"d32", sde::Decimal("-9999999.99")},
      {"d64", sde::Decimal("12345678901234.5678")},
      {"d128", sde::Decimal("-1234567890123456789012345678.0123456789")},
      {"d256", sde::Decimal("12345678901234567890123456789012345678901234567890.12345678901234567890")},
      {"dps", sde::Decimal("-999999999999999999")},
      {"s", std::string("naïve ✓")},
      {"fs", sde::Bytes{{'a', 'b', 0, 0}}},
      {"lc", std::string("low")},
      {"n", sde::Null{}},
      {"nn", std::string("there")},
      {"ln", sde::Null{}},
      {"u", *sde::Uuid::parse("6ba7b810-9dad-11d1-80b4-00c04fd430c8")},
      {"d", *sde::Date::parse("2149-06-06")},
      {"d32x", *sde::Date::parse("1900-01-01")},
      {"dt", *sde::TimestampTz::parse("2106-02-07T06:28:15Z")},
      {"dtn", *sde::Timestamp::parse("1970-01-01T00:00:01")},
      {"dt64", *sde::TimestampTz::parse("2026-10-07T12:34:56.789Z")},
      {"dt64n", *sde::Timestamp::parse("2026-10-07T12:34:56.123456")},
      {"e8", std::string("b")},
      {"e16", std::string("y'z")},
  };
}

/// What `toString` of each column says after the reference wrote the row it had read.
std::map<std::string, std::string> written_by_the_reference() {
  return {
      {"i8", "-128"},
      {"i16", "-32768"},
      {"i32", "-2147483648"},
      {"u8", "255"},
      {"u16", "65535"},
      {"u32", "4294967295"},
      {"u64", "9223372036854775807"},
      {"i128", "-170141183460469231731687303715884105728"},
      {"u128", "340282366920938463463374607431768211455"},
      {"i256", "-57896044618658097711785492504343953926634992332820282019728792003956564819968"},
      {"u256", "115792089237316195423570985008687907853269984665640564039457584007913129639935"},
      {"f32", "1.1"},
      {"f64", "-0.1"},
      {"b", "true"},
      {"d32", "-9999999.99"},
      {"d64", "12345678901234.5678"},
      {"d128", "-1234567890123456789012345678.0123456789"},
      {"d256", "12345678901234567890123456789012345678901234567890.1234567890123456789"},
      {"dps", "-999999999999999999"},
      {"s", "naïve ✓"},
      {"lc", "low"},
      {"nn", "there"},
      {"u", "6ba7b810-9dad-11d1-80b4-00c04fd430c8"},
      {"d", "2149-06-06"},
      {"d32x", "1900-01-01"},
      {"dt", "2106-02-07 06:28:15"},
      {"dtn", "1970-01-01 00:00:01"},
      {"dt64", "2026-10-07 12:34:56.789"},
      {"dt64n", "2026-10-07 12:34:56.123456000"},
      {"e8", "b"},
      {"e16", "y'z"},
  };
}

class ClickHouseTypes : public ::testing::Test {
 protected:
  void SetUp() override {
    SDE_REQUIRE_DSN("SDE_CLICKHOUSE_DSN", dsn_);
    scope_ = std::make_unique<ClickHouseScope>(dsn_);
    std::string ddl;
    std::string values;
    for (const Column& column : kColumns) {
      ddl += (ddl.empty() ? "" : ", ") + ("`" + column.name + "` " + column.type);
      values += (values.empty() ? "" : ", ") + column.literal;
    }
    admin().run("CREATE TABLE t (" + ddl + ") ENGINE = ReplacingMergeTree ORDER BY id");
    admin().run("INSERT INTO t VALUES (" + values + ")");
    engine_ = std::make_unique<sde::ClickHouseEngine>(scope_->dsn());
    engine_->connect();
  }

  [[nodiscard]] ClickHouseAdmin admin() const { return scope_->admin(); }
  /// `toString` of one column of the row with this id, as the administrator reads it.
  [[nodiscard]] std::string stored(const std::string& column, int id) const {
    const auto rows = admin().rows("SELECT toString(`" + column + "`) AS v FROM t WHERE id = " +
                                   std::to_string(id));
    return rows.empty() ? "<no row>" : rows[0].find("v")->as_string();
  }

  std::string dsn_;
  std::unique_ptr<ClickHouseScope> scope_;
  std::unique_ptr<sde::ClickHouseEngine> engine_;
};

TEST_F(ClickHouseTypes, EveryTypeIsReadAsTheReferenceReadsIt) {
  const std::optional<sde::Row> row = engine_->get("t", {{"id", std::int64_t{1}}});
  ASSERT_TRUE(row.has_value());
  const std::map<std::string, sde::Value> expected = read_by_the_reference();
  ASSERT_EQ(row->size(), expected.size());
  for (const auto& [name, value] : expected) {
    SCOPED_TRACE(name);
    ASSERT_TRUE(row->contains(name));
    EXPECT_EQ(row->at(name), value);
    if (const auto* exact = std::get_if<sde::Decimal>(&value)) {
      // Exact means the scale too: Decimal128(10) gives ten digits after the point, as there.
      EXPECT_EQ(std::get<sde::Decimal>(row->at(name)).to_string(), exact->to_string());
    }
  }
}

TEST_F(ClickHouseTypes, EveryTypeIsWrittenBackAsTheReferenceWritesIt) {
  sde::Row row = *engine_->get("t", {{"id", std::int64_t{1}}});
  row["id"] = std::int64_t{2};
  engine_->insert("t", row);
  for (const auto& [name, text] : written_by_the_reference()) {
    EXPECT_EQ(stored(name, 2), text) << name;
  }
  EXPECT_EQ(admin().rows("SELECT hex(fs) AS v, isNull(n) AS a, isNull(ln) AS b FROM t WHERE id = 2")[0]
                .find("v")
                ->as_string(),
            "61620000");
  EXPECT_EQ(*engine_->get("t", {{"id", std::int64_t{2}}}), row) << "a second round trip changed it";
}

TEST_F(ClickHouseTypes, WhatEachTypeTakesIsWrittenExactly) {
  const auto put = [&](const std::string& column, const sde::Value& value) {
    admin().run("TRUNCATE TABLE t");
    engine_->insert("t", {{"id", std::int64_t{3}}, {column, value}});
  };
  const auto write = [&](const std::string& column, const sde::Value& value) {
    put(column, value);
    return stored(column, 3);
  };
  EXPECT_EQ(write("e8", std::string("a")), "a");
  EXPECT_EQ(write("e8", std::int64_t{1}), "a");
  EXPECT_EQ(write("e16", std::int64_t{-1000}), "y'z");
  const auto hex = [&] { return admin().rows("SELECT hex(fs) AS v FROM t WHERE id = 3")[0].find("v")->as_string(); };
  put("fs", std::string("a"));
  EXPECT_EQ(hex(), "61000000");
  put("fs", sde::Bytes{{0xFF, 0, 1, 2}});
  EXPECT_EQ(hex(), "FF000102");
  EXPECT_EQ(write("dt64n", *sde::TimestampTz::parse("2026-01-01T00:00:00.123456Z")),
            "2026-01-01 00:00:00.123456000");
  EXPECT_EQ(write("i128", sde::Decimal("170141183460469231731687303715884105727")),
            "170141183460469231731687303715884105727");
  EXPECT_EQ(write("i128", std::int64_t{-5}), "-5");
  EXPECT_EQ(write("u128", sde::Decimal("340282366920938463463374607431768211455")),
            "340282366920938463463374607431768211455");
  EXPECT_EQ(write("u256", std::int64_t{0}), "0");
  EXPECT_EQ(write("f32", 1e-50), "0") << "a float too small for 32 bits is zero there too";
  EXPECT_EQ(write("dt", *sde::TimestampTz::parse("2026-01-01T00:00:01Z")), "2026-01-01 00:00:01");
}

TEST_F(ClickHouseTypes, AValueATypeCannotHoldIsRefusedWhereTheReferenceChangesIt) {
  // Measured through the reference on the same server: an enum name or number its type does not
  // declare is stored as 0, which no later read turns back into text; a fraction of a second is
  // cut from a DateTime and below a millisecond from a DateTime64(3); a decimal's third digit is
  // cut from a Decimal32(2); a Bool takes 1. Refused here, before anything is sent, with what both
  // refuse.
  const std::vector<std::pair<std::string, sde::Value>> refused = {
      {"e8", std::string("zz")},
      {"e8", std::int64_t{7}},
      {"fs", std::string("abcde")},
      {"fs", sde::Bytes{{'a'}}},
      {"u8", std::int64_t{-1}},
      {"i8", std::int64_t{128}},
      {"d", *sde::Date::parse("2149-06-07")},
      {"d", *sde::Date::parse("1969-12-31")},
      {"dt", *sde::TimestampTz::parse("2026-01-01T00:00:00.5Z")},
      {"dt64", *sde::TimestampTz::parse("2026-01-01T00:00:00.123456Z")},
      {"d32", sde::Decimal("1.001")},
      {"b", std::int64_t{1}},
      {"f32", 1e300},
      {"s", std::int64_t{5}},
      {"i128", sde::Decimal("170141183460469231731687303715884105728")},
      {"i128", sde::Decimal("1.5")},
      {"u128", std::int64_t{-1}},
      {"u256", sde::Decimal("115792089237316195423570985008687907853269984665640564039457584007913129639936")},
      {"i32", std::string("5")},
  };
  for (const auto& [column, value] : refused) {
    SCOPED_TRACE(column);
    const std::string message = refusal<sde::EngineError>(
        [&] { engine_->insert("t", {{"id", std::int64_t{3}}, {column, value}}); });
    EXPECT_TRUE(message.starts_with("insert into t failed: a ")) << message;
    EXPECT_NE(message.find("ClickHouse column of type"), std::string::npos) << message;
  }
  EXPECT_EQ(stored("id", 3), "<no row>");
}

TEST_F(ClickHouseTypes, AnAnswerThisLibraryCannotHoldIsRefusedNotRounded) {
  // The reference returns both: a UInt64 past 2^63 as a Python int, and nanoseconds cut to
  // microseconds without a word. A value here is a signed 64-bit integer, and a moment is cut by
  // nobody.
  admin().run("CREATE TABLE big (id Int64, u UInt64, ns DateTime64(9)) "
              "ENGINE = ReplacingMergeTree ORDER BY id");
  admin().run("INSERT INTO big VALUES (1, 18446744073709551615, '2026-10-07 12:34:56.000000000'), "
              "(2, 1, '2026-10-07 12:34:56.123456789')");
  EXPECT_EQ(refusal<sde::EngineError>([&] { (void)engine_->get("big", {{"id", std::int64_t{1}}}); }),
            "select from big failed: ClickHouse returned an integer past what 64 signed bits hold, "
            "which this library does not round");
  EXPECT_EQ(refusal<sde::EngineError>([&] { (void)engine_->get("big", {{"id", std::int64_t{2}}}); }),
            "select from big failed: ClickHouse returned a moment finer than a microsecond, which "
            "this library does not round");
}

}  // namespace
