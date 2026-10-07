/// One value, two engines, one answer - or the product's central promise is not true. A group moves
/// between engines while the application keeps running and does not notice, and "does not notice"
/// means the value read back does not depend on the engine it came from: not its content, and not
/// its type. Ported from the reference's `test_engine_agreement.py`, which found two divergences
/// there - a wall-clock time read as local time by one driver, and an instant read back without its
/// zone by another.
///
///     export SDE_POSTGRES_DSN=postgresql://postgres:sde@127.0.0.1:55432/sde
///     export SDE_CLICKHOUSE_DSN=clickhouse://default:sde@127.0.0.1:58123/sde
///     ctest -L live

#include <map>
#include <memory>
#include <optional>
#include <string>
#include <variant>
#include <vector>

#include <gtest/gtest.h>

#include "live/clickhouse.hpp"
#include "live/live.hpp"
#include "live/postgres.hpp"
#include "sde/clickhouse.hpp"
#include "sde/layout.hpp"
#include "sde/physical.hpp"
#include "sde/postgres.hpp"
#include "sde/value.hpp"

namespace {

/// Every neutral type there is, and the decimal both dialects render with a precision.
const std::vector<std::string> kNeutral = {"bool",    "bytes", "date",   "float32",
                                           "float64", "int32", "int64",  "json",
                                           "string",  "timestamp", "timestamptz", "uuid"};

/// The neutral types both dialects map. Derived, never listed by hand: the day a dialect gains a
/// mapping, this grows, and the test below has to be given a value for it.
std::vector<std::string> covered() {
  std::vector<std::string> out;
  for (const std::string& neutral : kNeutral) {
    if (sde::can_store(neutral, "postgres") && sde::can_store(neutral, "clickhouse")) {
      out.push_back(neutral);
    }
  }
  out.emplace_back("decimal(12,2)");
  return out;
}

/// One value per type, chosen to break things rather than to pass: sub-second precision, an
/// integer past 2^53, a decimal whose cents a float would lose, text with combining characters
/// and an em dash, a float that has no exact binary form.
std::map<std::string, sde::Value> values() {
  return {{"bool", true},
          {"int32", std::int64_t{-2147483648LL}},
          {"int64", std::int64_t{9007199254740993LL}},
          {"float32", 0.5},
          {"float64", 0.1},
          {"string", std::string("Zamo\xCC\x81wienie o\xCC\x81s\xCC\x81\xC4\x87 \xE2\x80\x94 ok")},
          {"uuid", *sde::Uuid::parse("6ba7b810-9dad-11d1-80b4-00c04fd430c8")},
          {"date", *sde::Date::parse("2026-08-27")},
          {"timestamp", *sde::Timestamp::parse("2026-08-27T12:00:00.5")},
          {"timestamptz", *sde::TimestampTz::parse("2026-08-27T12:00:00.5Z")},
          {"decimal(12,2)", sde::Decimal("1234.56")}};
}

TEST(EngineAgreement, EverySharedNeutralTypeHasAValueToCheck) {
  const std::map<std::string, sde::Value> written = values();
  for (const std::string& neutral : covered()) {
    EXPECT_TRUE(written.contains(neutral))
        << "both dialects map " << neutral << " and this file has no value for it, so agreement on "
        << "that type is not being checked. Add one - chosen to break things, not to pass.";
  }
}

TEST(EngineAgreement, TheTypesLeftOutAreLeftOutForAWrittenReason) {
  // bytes: a ClickHouse String gives hex text back for bytes that are not UTF-8, and nothing tells
  // a binary String from a text one on the way back. json: PostgreSQL gives a document, a String
  // gives the text. Both are tasks; neither is a mapping.
  std::vector<std::string> unmapped;
  for (const std::string& neutral : kNeutral) {
    if (!sde::can_store(neutral, "clickhouse")) unmapped.push_back(neutral);
  }
  EXPECT_EQ(unmapped, (std::vector<std::string>{"bytes", "json"}));
}

class EngineAgreementLive : public ::testing::Test {
 protected:
  void SetUp() override {
    std::string pg;
    std::string ch;
    SDE_REQUIRE_DSN("SDE_POSTGRES_DSN", pg);
    SDE_REQUIRE_DSN("SDE_CLICKHOUSE_DSN", ch);
    pg_scope_ = std::make_unique<sde::live::Scope>(pg);
    ch_scope_ = std::make_unique<sde::live::ClickHouseScope>(ch);
    pg_ = std::make_unique<sde::PostgresEngine>(pg_scope_->dsn());
    pg_->connect();
    ch_ = std::make_unique<sde::ClickHouseEngine>(ch_scope_->dsn());
    ch_->connect();
    for (const auto& [engine, dialect] : std::vector<std::pair<sde::Engine*, std::string>>{
             {pg_.get(), "postgres"}, {ch_.get(), "clickhouse"}}) {
      sde::PhysicalLayout layout;
      layout.tables = {{"A", "agreement"}};
      for (const std::string& neutral : covered()) {
        layout.columns["A"][neutral] = sde::column_type(neutral, dialect);
      }
      layout.columns["A"]["id"] = sde::column_type("uuid", dialect);
      (void)engine->ensure_schema(layout, {{"A", {"id"}}});
    }
  }

  /// The row with this id from each engine.
  std::pair<sde::Row, sde::Row> both(const sde::Row& row) {
    pg_->insert("agreement", row);
    ch_->insert("agreement", row);
    const sde::Row key{{"id", row.at("id")}};
    return {*pg_->get("agreement", key), *ch_->get("agreement", key)};
  }

  std::unique_ptr<sde::live::Scope> pg_scope_;
  std::unique_ptr<sde::live::ClickHouseScope> ch_scope_;
  std::unique_ptr<sde::PostgresEngine> pg_;
  std::unique_ptr<sde::ClickHouseEngine> ch_;
};

TEST_F(EngineAgreementLive, TheSameValueReadFromEitherEngineIsTheSameValue) {
  // Field by field, content and type: a wall-clock time against an instant does not compare, and
  // a decimal becoming a float loses cents without ever raising.
  sde::Row row{{"id", *sde::Uuid::parse("00000000-0000-0000-0000-00000000a001")}};
  for (const auto& [neutral, value] : values()) row[neutral] = value;
  const auto [from_pg, from_ch] = both(row);
  for (const std::string& neutral : covered()) {
    const sde::Value& left = from_pg.at(neutral);
    const sde::Value& right = from_ch.at(neutral);
    EXPECT_EQ(left.index(), right.index()) << neutral << ": the engines give two types";
    EXPECT_EQ(left, right) << neutral;
    EXPECT_EQ(left, row.at(neutral)) << neutral << ": both agree, on another value";
  }
}

TEST_F(EngineAgreementLive, AWallClockTimeInAnInstantColumnMeansTheSameInstantInBoth) {
  // Asserted against an absolute value too: two engines that agree on the wrong instant would pass
  // the test above. A wall-clock time is read as UTC, as everywhere in this library.
  sde::Row row{{"id", *sde::Uuid::parse("00000000-0000-0000-0000-00000000a002")}};
  for (const auto& [neutral, value] : values()) row[neutral] = value;
  const sde::Value noon = *sde::Timestamp::parse("2026-08-27T12:00:00");
  row["timestamptz"] = noon;
  row["timestamp"] = noon;
  const auto [from_pg, from_ch] = both(row);
  const sde::Value expected = *sde::TimestampTz::parse("2026-08-27T12:00:00Z");
  EXPECT_EQ(from_pg.at("timestamptz"), expected) << "PostgreSQL moved a wall-clock time";
  EXPECT_EQ(from_ch.at("timestamptz"), expected) << "ClickHouse moved a wall-clock time";
  EXPECT_EQ(from_pg.at("timestamp"), noon);
  EXPECT_EQ(from_ch.at("timestamp"), noon);
}

}  // namespace
