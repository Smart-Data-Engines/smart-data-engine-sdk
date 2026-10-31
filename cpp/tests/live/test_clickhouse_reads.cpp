/// Logical reads against a real ClickHouse: pages in one portable order that keep every tie across
/// page boundaries, counts and exact summaries over the values the server holds, every read of a
/// ReplacingMergeTree asking for FINAL as the server logged it, a UUID key that prunes granules,
/// and temporal keys that keep their microseconds whatever the session's or the process's zone.
/// Ported from the ClickHouse halves of the reference's `test_query_live.py` and
/// `test_index_use_live.py`, its `test_clickhouse_datetime_queries.py`, and its slice's structural
/// FINAL test - here read from the server's own query log rather than from a patched driver.
///
///     SDE_CLICKHOUSE_DSN=clickhouse://default:sde@127.0.0.1:58123/sde ctest -L live

#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <ctime>
#include <limits>
#include <map>
#include <memory>
#include <optional>
#include <regex>
#include <set>
#include <string>
#include <utility>
#include <variant>
#include <vector>

#include <gtest/gtest.h>

#include "engines/clickhouse/values.hpp"
#include "live/capture.hpp"
#include "live/clickhouse.hpp"
#include "live/live.hpp"
#include "live/reads.hpp"
#include "sde/canonical.hpp"
#include "sde/clickhouse.hpp"
#include "sde/errors.hpp"
#include "sde/hashing.hpp"
#include "sde/json.hpp"
#include "sde/layout.hpp"
#include "sde/model.hpp"
#include "sde/placement.hpp"
#include "sde/provisioning.hpp"
#include "sde/query.hpp"
#include "sde/session.hpp"
#include "sde/telemetry.hpp"

namespace {

using sde::live::ClickHouseAdmin;
using sde::live::ClickHouseRoles;
using sde::live::ClickHouseScope;
using sde::live::event_at;
using sde::live::sorted;

const std::string kProject(32, '1');

/// A contract-3 map of every group of `model` in engine `db`, each in ClickHouse's default layout.
sde::PlacementMap clickhouse_map(const sde::Model& model) {
  return sde::live::default_map(model, "clickhouse", "db");
}

std::unique_ptr<sde::ClickHouseEngine> connected(const std::string& dsn) {
  auto made = std::make_unique<sde::ClickHouseEngine>(dsn);
  made->connect();
  return made;
}

/// The reference's fixture: its six events saved through a session on the runtime login, telemetry
/// on, the saving window rolled away - with the declared names or their hashed form.
struct Events {
  ClickHouseRoles roles;
  sde::Model declared;
  std::pair<sde::Model, std::optional<sde::NameMap>> spoken;
  sde::PlacementMap map;
  std::unique_ptr<sde::ClickHouseEngine> provisioning;
  std::unique_ptr<sde::ClickHouseEngine> runtime;
  std::unique_ptr<sde::Recorder> recorder;
  std::unique_ptr<sde::Session> session;
  std::vector<sde::Row> rows;

  static std::pair<sde::Model, std::optional<sde::NameMap>> speak(const sde::Model& model,
                                                                 bool hash) {
    if (!hash) return {model, std::nullopt};
    auto [hashed, names] = sde::hash_identifiers(model, std::string(32, 'q'));
    return {std::move(hashed), std::move(names)};
  }

  Events(const std::string& dsn, bool hash)
      : roles(dsn),
        declared(sde::load_neutral_model(R"json({"entities": [{"name": "Event", "fields": [
            {"name": "id", "type": "uuid"}, {"name": "label", "type": "string", "nullable": true},
            {"name": "at", "type": "timestamptz"}, {"name": "amount", "type": "decimal(12,2)"}],
            "key": ["id"]}]})json")),
        spoken(speak(declared, hash)),
        map(clickhouse_map(spoken.first)),
        rows(sde::live::event_rows()) {
    const sde::Model& model = spoken.first;
    const std::string entity = model.entities().at(0).name;
    const sde::PhysicalLayout& layout = map.placement_of(entity).source.layout;
    provisioning = connected(roles.operator_dsn());
    (void)provisioning->ensure_schema(layout, {{entity, model.entity(entity).key}});
    roles.grant(layout.table_for(entity));
    runtime = connected(roles.runtime_dsn());
    recorder = std::make_unique<sde::Recorder>(model);
    sde::SessionOptions options;
    options.recorder = recorder.get();
    if (spoken.second) options.names = &*spoken.second;
    session = std::make_unique<sde::Session>(
        model, map, std::map<std::string, sde::Engine*>{{"db", runtime.get()}}, options);
    session->save_many("Event", rows);
    (void)recorder->roll();
  }

  [[nodiscard]] const sde::Model& model() const noexcept { return spoken.first; }
  [[nodiscard]] std::string table() const {
    return map.placement_of(model().entities().at(0).name).source.layout.table_for(
        model().entities().at(0).name);
  }

  /// Every page of a scan, following each page's position; at most twenty of them.
  std::vector<sde::Row> pages(sde::ScanOptions options) {
    std::vector<sde::Row> out;
    for (int page = 0; page < 20; ++page) {
      const sde::ScanPage found = session->scan("Event", options);
      EXPECT_LE(static_cast<std::int64_t>(found.rows.size()), options.limit.rows());
      out.insert(out.end(), found.rows.begin(), found.rows.end());
      if (!found.next_after) return out;
      options.after = found.next_after;
    }
    ADD_FAILURE() << "pagination did not end";
    return out;
  }
};

class ClickHouseReads : public ::testing::Test {
 protected:
  void SetUp() override { SDE_REQUIRE_DSN("SDE_CLICKHOUSE_DSN", dsn_); }
  std::string dsn_;
};

// --- pages, counts and summaries ----------------------------------------------------------------

TEST_F(ClickHouseReads, UuidPagesUseOnePortableOrderAndNeverDropTheSentinel) {
  // ClickHouse orders a UUID by its two halves as integers, low half first; the reads order it by
  // its bytes, as PostgreSQL does, and the ids are chosen so the two orders differ.
  Events events(dsn_, false);
  for (const int size : {1, 2, 3, 6}) {
    SCOPED_TRACE("pages of " + std::to_string(size));
    sde::ScanOptions options;
    options.limit = size;
    EXPECT_EQ(events.pages(options), sorted(events.rows, {"id"}));
    options.descending = true;
    EXPECT_EQ(events.pages(options), sorted(events.rows, {"id"}, true));
  }
}

TEST_F(ClickHouseReads, NullableSortTiesKeepNullsLastInBothDirections) {
  for (const bool hashed : {false, true}) {
    SCOPED_TRACE(hashed ? "hashed names" : "the client's names");
    Events events(dsn_, hashed);
    for (const bool descending : {false, true}) {
      std::vector<sde::Row> present;
      std::vector<sde::Row> absent;
      for (const sde::Row& row : events.rows) {
        (std::holds_alternative<sde::Null>(row.at("label")) ? absent : present).push_back(row);
      }
      std::vector<sde::Row> expected = sorted(present, {"label", "id"}, descending);
      for (const sde::Row& row : sorted(absent, {"id"}, descending)) expected.push_back(row);
      sde::ScanOptions options;
      options.order_by = "label";
      options.descending = descending;
      options.limit = 1;
      EXPECT_EQ(events.pages(options), expected) << (descending ? "descending" : "ascending");
    }
    sde::CountOptions nulls;
    nulls.where = sde::Row{{"label", sde::Null{}}};
    EXPECT_EQ(events.session->count("Event", nulls), 2U);
  }
}

TEST_F(ClickHouseReads, AHalfOpenTimeRangeWithEqualityAndMicrosecondTies) {
  Events events(dsn_, false);
  const sde::Range bounds{"at", event_at(0), event_at(3)};
  std::vector<sde::Row> wanted;
  for (const sde::Row& row : events.rows) {
    if (row.at("label") == sde::Value(std::string("a")) &&
        !sde::live::before(row.at("at"), event_at(0)) &&
        sde::live::before(row.at("at"), event_at(3))) {
      wanted.push_back(row);
    }
  }
  ASSERT_EQ(wanted.size(), 2U);
  sde::ScanOptions options;
  options.where = sde::Row{{"label", std::string("a")}};
  options.bounds = bounds;
  options.order_by = "at";
  options.limit = 1;
  EXPECT_EQ(events.pages(options), sorted(wanted, {"at", "id"}));
  sde::CountOptions counted;
  counted.where = options.where;
  counted.bounds = bounds;
  EXPECT_EQ(events.session->count("Event", counted), wanted.size());

  const std::optional<sde::Window> window = events.recorder->roll();
  ASSERT_TRUE(window.has_value());
  std::set<std::string> kinds;
  for (const sde::ShapeStats& stats : window->shapes) kinds.insert(stats.kind);
  EXPECT_EQ(kinds, (std::set<std::string>{"range_read", "aggregate"}));
  for (const sde::ShapeStats& stats : window->shapes) {
    const std::map<sde::Predicates, std::uint64_t> each{{sde::Predicates{{"label"}, "at"},
                                                         stats.calls}};
    EXPECT_EQ(stats.filtered, each) << stats.kind;
    if (stats.kind == "aggregate") {
      EXPECT_EQ(stats.rows, 1U);
    }
  }
}

TEST_F(ClickHouseReads, FiltersAreRecordedInTheModelsVocabularyWhenNamesAreHashed) {
  Events events(dsn_, true);
  sde::CountOptions counted;
  counted.where = sde::Row{{"label", std::string("a")}};
  counted.bounds = sde::Range{"at", event_at(0), event_at(3)};
  (void)events.session->count("Event", counted);
  sde::ScanOptions scanned;
  scanned.where = counted.where;
  scanned.limit = 2;
  (void)events.session->scan("Event", scanned);
  const std::optional<sde::Window> window = events.recorder->roll();
  ASSERT_TRUE(window.has_value());
  std::set<std::string> declared;
  for (const sde::Field& field : events.model().entities().at(0).fields) declared.insert(field.name);
  std::set<std::string> recorded;
  for (const sde::ShapeStats& stats : window->shapes) {
    for (const auto& [predicates, calls] : stats.filtered) {
      recorded.insert(predicates.equal.begin(), predicates.equal.end());
      if (!predicates.range.empty()) recorded.insert(predicates.range);
    }
  }
  EXPECT_EQ(recorded.size(), 2U);
  EXPECT_TRUE(std::includes(declared.begin(), declared.end(), recorded.begin(), recorded.end()));
  EXPECT_EQ(recorded.count("label") + recorded.count("at"), 0U);
}

TEST_F(ClickHouseReads, DecimalBoundsAreNotRoundedToTheStoredScale) {
  Events events(dsn_, false);
  const sde::Range bounds{"amount", sde::Decimal("1.249"), sde::Decimal("3.251")};
  std::vector<sde::Row> expected;
  for (const sde::Row& row : events.rows) {
    const auto& amount = std::get<sde::Decimal>(row.at("amount"));
    if (!(amount < sde::Decimal("1.249")) && amount < sde::Decimal("3.251")) expected.push_back(row);
  }
  sde::ScanOptions options;
  options.bounds = bounds;
  options.order_by = "amount";
  options.limit = 1;
  EXPECT_EQ(events.pages(options), sorted(expected, {"amount", "id"}));
  sde::CountOptions counted;
  counted.bounds = bounds;
  EXPECT_EQ(events.session->count("Event", counted), 3U);
}

TEST_F(ClickHouseReads, ANumericSummaryIsExactOnEmptyAndFilteredDecimalData) {
  Events events(dsn_, false);
  sde::SummaryOptions filtered;
  filtered.where = sde::Row{{"label", std::string("a")}};
  filtered.mean_scale = 3;
  const sde::NumericSummary result = events.session->summarize("Event", "amount", filtered);
  EXPECT_EQ(result.count, 2U);
  EXPECT_EQ(result.non_null_count, 2U);
  EXPECT_EQ(result.minimum, sde::Decimal("3.25"));
  EXPECT_EQ(result.maximum, sde::Decimal("5.25"));
  EXPECT_EQ(result.total->to_string(), "8.50");
  EXPECT_EQ(result.mean->to_string(), "4.250");
  sde::SummaryOptions nothing;
  nothing.where = sde::Row{{"label", std::string("absent")}};
  const sde::NumericSummary empty = events.session->summarize("Event", "amount", nothing);
  EXPECT_EQ(empty.count, 0U);
  EXPECT_EQ(empty.non_null_count, 0U);
  EXPECT_FALSE(empty.minimum || empty.maximum || empty.total || empty.mean);
  const std::optional<sde::Window> window = events.recorder->roll();
  ASSERT_TRUE(window.has_value());
  EXPECT_EQ(window->shapes.at(0).rows, 2U);
  EXPECT_EQ(sde::dump_json(window->as_record(events.model())).find("8.50"), std::string::npos)
      << "a value reached the telemetry window";
}

TEST_F(ClickHouseReads, ASummaryCastsInt64BeforeSummingAndKeepsLargeResults) {
  const ClickHouseRoles roles(dsn_);
  const sde::Model model = sde::load_neutral_model(R"({"entities": [{"name": "Number", "fields": [
      {"name": "id", "type": "int64"}, {"name": "value", "type": "int64", "nullable": true}],
      "key": ["id"]}]})");
  const sde::PlacementMap map = clickhouse_map(model);
  const auto provisioning = connected(roles.operator_dsn());
  (void)provisioning->ensure_schema(map.placement_of("Number").source.layout, {{"Number", {"id"}}});
  roles.grant("number");
  const auto runtime = connected(roles.runtime_dsn());
  sde::Session session(model, map, {{"db", runtime.get()}});
  constexpr std::int64_t kLargest = std::numeric_limits<std::int64_t>::max();
  session.save_many("Number", {{{"id", std::int64_t{1}}, {"value", kLargest}},
                               {{"id", std::int64_t{2}}, {"value", kLargest}},
                               {{"id", std::int64_t{3}}, {"value", sde::Null{}}}});
  const sde::NumericSummary result = session.summarize("Number", "value");
  EXPECT_EQ(result.total->to_string(), "18446744073709551614");
  EXPECT_EQ(result.mean->to_string(), "9223372036854775807.000000");
  EXPECT_EQ(result.count, 3U);
  EXPECT_EQ(result.non_null_count, 2U);
  sde::SummaryOptions third;
  third.where = sde::Row{{"id", std::int64_t{3}}};
  const sde::NumericSummary empty = session.summarize("Number", "value", third);
  EXPECT_EQ(empty.count, 1U);
  EXPECT_EQ(empty.non_null_count, 0U);
  EXPECT_FALSE(empty.minimum || empty.maximum || empty.total || empty.mean);
}

TEST_F(ClickHouseReads, AScanProjectsTheModelsFieldsAndARevokedGrantRefusesEveryRead) {
  Events events(dsn_, false);
  events.roles.command("ALTER TABLE `event` ADD COLUMN extra String");
  sde::ScanOptions one;
  one.limit = 1;
  const sde::ScanPage page = events.session->scan("Event", one);
  std::set<std::string> fields;
  for (const auto& [name, value] : page.rows.at(0)) fields.insert(name);
  EXPECT_EQ(fields, (std::set<std::string>{"amount", "at", "id", "label"}));
  events.roles.grant("event", true);
  EXPECT_THROW((void)events.session->scan("Event"), sde::EngineError);
  EXPECT_THROW((void)events.session->count("Event"), sde::EngineError);
  EXPECT_THROW((void)events.session->summarize("Event", "amount"), sde::EngineError);
}

TEST_F(ClickHouseReads, AGenerationColumnIsNotALogicalProjection) {
  const ClickHouseRoles roles(dsn_);
  roles.command("CREATE TABLE generations (id Int64) ENGINE = ReplacingMergeTree ORDER BY id");
  const sde::Model model = sde::load_neutral_model(
      R"({"entities": [{"name": "Record", "fields": [{"name": "id", "type": "int64"}],
                        "key": ["id"]}]})");
  sde::LoadOptions options;
  options.model = &model;
  const sde::PlacementMap map = sde::load_map(
      R"({"contract": 4, "project_id": ")" + kProject + R"(", "model_version": ")" +
          model.version() + R"(", "map_version": 1, "groups": {"Record": {"write_epoch": 1,
          "source": {"id": "Record@db", "engine": "db", "layout": {
          "tables": {"Record": "generations"}, "columns": {"Record": {"id": "Int64"}}}}}}})",
      options);
  const auto provisioning = connected(roles.operator_dsn());
  sde::prepare_schema(model, map, {{"db", provisioning.get()}}, kProject);
  roles.grant("generations");
  const auto runtime = connected(roles.runtime_dsn());
  sde::SessionOptions session_options;
  session_options.project_id = kProject;
  sde::Session session(model, map, {{"db", runtime.get()}}, session_options);
  session.save_many("Record", {{{"id", std::int64_t{1}}}, {{"id", std::int64_t{2}}}});
  sde::ScanOptions one;
  one.limit = 1;
  const sde::ScanPage page = session.scan("Record", one);
  EXPECT_EQ(page.rows, (std::vector<sde::Row>{{{"id", std::int64_t{1}}}}));
  EXPECT_EQ(page.next_after, (sde::Row{{"id", std::int64_t{1}}}));
  EXPECT_EQ(session.count("Record"), 2U);
  EXPECT_EQ(session.summarize("Record", "id").total->to_string(), "3");
}

TEST_F(ClickHouseReads, TimestampOrderAndBoundsDoNotDependOnTheSessionsTimeZone) {
  // The reference moves its runtime connection to New York; this adapter has no such knob, so the
  // runtime login's own default is New York instead, and every exchange still runs in UTC.
  const ClickHouseRoles roles(dsn_);
  const sde::Model model = sde::load_neutral_model(R"({"entities": [{"name": "Event", "fields": [
      {"name": "id", "type": "int64"}, {"name": "at", "type": "timestamp"}], "key": ["id"]}]})");
  const sde::PlacementMap map = clickhouse_map(model);
  const auto provisioning = connected(roles.operator_dsn());
  (void)provisioning->ensure_schema(map.placement_of("Event").source.layout, {{"Event", {"id"}}});
  roles.grant("event");
  const sde::Value first_at = *sde::Timestamp::parse("2026-09-14T10:00:00.123456");
  const sde::Value second_at = *sde::Timestamp::parse("2026-09-14T10:00:00.123457");
  provisioning->insert_many("event", {{{"id", std::int64_t{1}}, {"at", first_at}},
                                      {{"id", std::int64_t{2}}, {"at", second_at}}});
  roles.command("ALTER USER `" + roles.username() +
                "` SETTINGS session_timezone = 'America/New_York'");
  const std::vector<sde::Json> profile = ClickHouseAdmin(dsn_).rows(
      "SELECT value FROM system.settings_profile_elements WHERE user_name = '" + roles.username() +
      "' AND setting_name = 'session_timezone'");
  ASSERT_EQ(profile.size(), 1U);
  ASSERT_EQ(profile[0].find("value")->as_string(), "America/New_York")
      << "the login is not in the zone this test is about";
  const auto runtime = connected(roles.runtime_dsn());
  sde::Session session(model, map, {{"db", runtime.get()}});
  sde::ScanOptions options;
  options.bounds = sde::Range{"at", std::string("2026-09-14T12:00:00.123456+02:00"),
                              std::string("2026-09-14T10:00:00.123458Z")};
  options.order_by = "at";
  options.limit = 1;
  const sde::ScanPage first = session.scan("Event", options);
  EXPECT_EQ(first.rows, (std::vector<sde::Row>{{{"id", std::int64_t{1}}, {"at", first_at}}}));
  sde::ScanOptions next;
  next.order_by = "at";
  next.after = first.next_after;
  EXPECT_EQ(session.scan("Event", next).rows,
            (std::vector<sde::Row>{{{"id", std::int64_t{2}}, {"at", second_at}}}));
  EXPECT_EQ(runtime->get("event", {{"id", std::int64_t{1}}}),
            (sde::Row{{"id", std::int64_t{1}}, {"at", first_at}}));
}

TEST_F(ClickHouseReads, AFloatsNonFiniteValuesAreReadAndARangeExcludesNaN) {
  const ClickHouseRoles roles(dsn_);
  const sde::Model model = sde::load_neutral_model(R"({"entities": [{"name": "FloatValue",
      "fields": [{"name": "id", "type": "int64"}, {"name": "value", "type": "float64"}],
      "key": ["id"]}]})");
  const sde::PlacementMap map = clickhouse_map(model);
  const auto provisioning = connected(roles.operator_dsn());
  const sde::PhysicalLayout& layout = map.placement_of("FloatValue").source.layout;
  (void)provisioning->ensure_schema(layout, {{"FloatValue", {"id"}}});
  ASSERT_EQ(layout.table_for("FloatValue"), "float_value");
  roles.grant("float_value");
  roles.command("INSERT INTO float_value SELECT 1,0.25 UNION ALL SELECT 2,toFloat64('nan') "
                "UNION ALL SELECT 3,toFloat64('inf') UNION ALL SELECT 4,toFloat64('-inf')");
  const auto runtime = connected(roles.runtime_dsn());
  sde::Session session(model, map, {{"db", runtime.get()}});
  const sde::ScanPage page = session.scan("FloatValue");
  ASSERT_EQ(page.rows.size(), 4U);
  std::vector<std::int64_t> ids;
  for (const sde::Row& row : page.rows) ids.push_back(std::get<std::int64_t>(row.at("id")));
  EXPECT_EQ(ids, (std::vector<std::int64_t>{1, 2, 3, 4}));
  EXPECT_EQ(std::get<double>(page.rows[0].at("value")), 0.25);
  EXPECT_TRUE(std::isnan(std::get<double>(page.rows[1].at("value"))));
  EXPECT_EQ(std::get<double>(page.rows[2].at("value")), std::numeric_limits<double>::infinity());
  EXPECT_EQ(std::get<double>(page.rows[3].at("value")), -std::numeric_limits<double>::infinity());
  sde::ScanOptions positive;
  positive.bounds = sde::Range{"value", std::int64_t{0}};
  ids.clear();
  for (const sde::Row& row : session.scan("FloatValue", positive).rows) {
    ids.push_back(std::get<std::int64_t>(row.at("id")));
  }
  EXPECT_EQ(ids, (std::vector<std::int64_t>{1, 3}));
  sde::CountOptions counted;
  counted.bounds = positive.bounds;
  EXPECT_EQ(session.count("FloatValue", counted), 2U);
}

TEST_F(ClickHouseReads, AFloatBoundKeepsEveryDigitOfItsValue) {
  // Three floats a hundred-millionth apart, and a bound equal to the middle one: written with
  // fewer digits than a double carries, the bound would move past it and the read would lose a row.
  const ClickHouseRoles roles(dsn_);
  const sde::Model model = sde::load_neutral_model(R"({"entities": [{"name": "FloatValue",
      "fields": [{"name": "id", "type": "int64"}, {"name": "value", "type": "float64"}],
      "key": ["id"]}]})");
  const sde::PlacementMap map = clickhouse_map(model);
  const auto provisioning = connected(roles.operator_dsn());
  (void)provisioning->ensure_schema(map.placement_of("FloatValue").source.layout,
                                    {{"FloatValue", {"id"}}});
  roles.grant("float_value");
  const auto runtime = connected(roles.runtime_dsn());
  sde::Session session(model, map, {{"db", runtime.get()}});
  session.save_many("FloatValue", {{{"id", std::int64_t{1}}, {"value", 0.1234567}},
                                   {{"id", std::int64_t{2}}, {"value", 0.12345675}},
                                   {{"id", std::int64_t{3}}, {"value", 0.1234568}}});
  sde::ScanOptions from;
  from.bounds = sde::Range{"value", 0.12345675};
  std::vector<std::int64_t> ids;
  for (const sde::Row& row : session.scan("FloatValue", from).rows) {
    ids.push_back(std::get<std::int64_t>(row.at("id")));
  }
  EXPECT_EQ(ids, (std::vector<std::int64_t>{2, 3}));
  sde::CountOptions equal;
  equal.where = sde::Row{{"value", 0.12345675}};
  EXPECT_EQ(session.count("FloatValue", equal), 1U);
}

TEST_F(ClickHouseReads, ADecimalIsReadAtItsDeclaredScale) {
  const ClickHouseRoles roles(dsn_);
  const sde::Model model = sde::load_neutral_model(R"json({"entities": [{"name": "Price", "fields": [
      {"name": "id", "type": "int64"}, {"name": "value", "type": "decimal(38,18)"}],
      "key": ["id"]}]})json");
  const sde::PlacementMap map = clickhouse_map(model);
  const auto provisioning = connected(roles.operator_dsn());
  (void)provisioning->ensure_schema(map.placement_of("Price").source.layout, {{"Price", {"id"}}});
  roles.grant("price");
  const std::string literal = "99999999999999999999.123456789012345678";
  roles.command("INSERT INTO price SELECT 1,CAST('" + literal +
                "' AS Decimal(38,18)) UNION ALL SELECT 2,CAST('7' AS Decimal(38,18))");
  const auto runtime = connected(roles.runtime_dsn());
  sde::Session session(model, map, {{"db", runtime.get()}});
  const std::vector<sde::Row> rows = session.scan("Price").rows;
  ASSERT_EQ(rows.size(), 2U);
  EXPECT_EQ(std::get<sde::Decimal>(rows[0].at("value")).to_string(), literal);
  EXPECT_EQ(std::get<sde::Decimal>(rows[1].at("value")).to_string(), "7.000000000000000000");
}

TEST_F(ClickHouseReads, AScanReadsEachValueByItsFieldsTypeWhateverTheColumnsZone) {
  // A hand-written layout can give an instant field a column without a zone, and a wall-clock
  // field one with UTC. A scan reads each by the field's type, as the reference's does; a point read
  // gives the column's, as the reference's does too - recorded as a finding about both.
  const ClickHouseRoles roles(dsn_);
  const sde::Model model = sde::load_neutral_model(R"({"entities": [{"name": "Tick", "fields": [
      {"name": "id", "type": "int64"}, {"name": "at", "type": "timestamptz"},
      {"name": "wall", "type": "timestamp"}], "key": ["id"]}]})");
  sde::LoadOptions options;
  options.model = &model;
  const sde::PlacementMap map = sde::load_map(
      R"json({"contract": 3, "model_version": ")json" + model.version() + R"json(", "map_version": 1,
          "groups": {"Tick": {"source": {"id": "s", "engine": "db", "layout": {
          "tables": {"Tick": "tick"}, "columns": {"Tick": {"id": "Int64", "at": "DateTime64(6)",
          "wall": "DateTime64(6, 'UTC')"}}}}}}})json",
      options);
  const auto provisioning = connected(roles.operator_dsn());
  (void)provisioning->ensure_schema(map.placement_of("Tick").source.layout, {{"Tick", {"id"}}});
  roles.grant("tick");
  const auto runtime = connected(roles.runtime_dsn());
  sde::Session session(model, map, {{"db", runtime.get()}});
  const sde::Value instant = *sde::TimestampTz::parse("2026-10-07T12:00:00.123456Z");
  const sde::Value wall = *sde::Timestamp::parse("2026-10-07T12:00:00.123456");
  session.save("Tick", {{"id", std::int64_t{1}}, {"at", instant}, {"wall", wall}});
  EXPECT_EQ(session.scan("Tick").rows,
            (std::vector<sde::Row>{{{"id", std::int64_t{1}}, {"at", instant}, {"wall", wall}}}));
  EXPECT_EQ(session.get("Tick", {{"id", std::int64_t{1}}}),
            (sde::Row{{"id", std::int64_t{1}},
                      {"at", *sde::Timestamp::parse("2026-10-07T12:00:00.123456")},
                      {"wall", *sde::TimestampTz::parse("2026-10-07T12:00:00.123456Z")}}));
}

// --- what the server ran ------------------------------------------------------------------------

TEST_F(ClickHouseReads, EveryReadOfTheTableAsksForFinalAsTheServerLoggedIt) {
  // FINAL is what makes a ReplacingMergeTree deduplicate at read time. Its absence from a point
  // read cannot be pinned by its result - a background merge collapses the duplicate, and with
  // merges stopped LIMIT 1 reads the parts in parallel - so the reference patches its driver to see
  // the SQL. Here the server's query log says what ran, for every read path of the adapter.
  Events events(dsn_, false);
  const std::string marker = sde::live::fresh(16);
  const auto ch = connected(events.roles.operator_dsn());
  const sde::Row key{{"id", *sde::Uuid::parse(sde::live::kEventIds[0])}};
  (void)ch->get("event", key);
  (void)ch->count("event");
  (void)ch->key_range("event", {"id"}, std::nullopt, std::nullopt, 2);
  (void)ch->nth_key("event", {"id"}, 2);
  sde::ScanOptions scanned;
  scanned.limit = 2;
  (void)events.session->scan("Event", scanned);
  (void)events.session->count("Event");
  (void)events.session->summarize("Event", "amount");
  ClickHouseAdmin root(dsn_);
  root.run("SYSTEM FLUSH LOGS");
  std::vector<std::string> reads;
  for (const sde::Json& row : root.rows(
           "SELECT query FROM system.query_log WHERE type = 'QueryFinish' AND query_kind = "
           "'Select' AND current_database = '" + events.roles.database() +
           "' AND has(tables, '" + events.roles.database() + ".event') "
           "AND event_time > now() - 600 ORDER BY event_time_microseconds")) {
    reads.push_back(row.find("query")->as_string());
  }
  // The six events were saved through the session before; every read of the table since is ours.
  EXPECT_GE(reads.size(), 7U) << "the log holds fewer reads than were made";
  for (const std::string& sql : reads) {
    EXPECT_NE(sql.find(" FINAL"), std::string::npos)
        << "a read went out without FINAL: " << sql << ". On a ReplacingMergeTree that returns rows "
        << "the engine considers superseded, which is a wrong answer rather than a slow one.";
  }
  (void)marker;
}

TEST_F(ClickHouseReads, AScanByAUuidKeyPrunesGranules) {
  // A UUID equality written as toString(id) = '...' read every granule; compared natively the
  // primary key keeps one. Asked of the server's planner for the statement the adapter ran.
  const ClickHouseScope scope(dsn_);
  const sde::Model model = sde::load_neutral_model(R"({"entities": [{"name": "Order", "fields": [
      {"name": "account", "type": "string"}, {"name": "id", "type": "uuid"}], "key": ["id"]}]})");
  const sde::PlacementMap map = clickhouse_map(model);
  sde::ClickHouseEngine engine(scope.dsn());
  engine.connect();
  sde::live::Spy<sde::ClickHouseEngine> spy(engine);
  sde::Session session(model, map, {{"db", &spy}});
  session.ensure_schema();
  // 40,000 ids, the reference's multiplicative sequence: n * 2654435761, below 2^64 for every n
  // here, so the high half of each is zero and the low half spreads.
  std::vector<sde::Value> ids;
  for (std::uint64_t n = 1; n <= 40000; ++n) {
    const std::uint64_t low = n * 2654435761ULL;
    char text[40];
    std::snprintf(text, sizeof text, "00000000-0000-0000-%04llx-%012llx",
                  static_cast<unsigned long long>(low >> 48U),
                  static_cast<unsigned long long>(low & 0xFFFFFFFFFFFFULL));
    ids.emplace_back(*sde::Uuid::parse(text));
  }
  for (std::size_t start = 0; start < ids.size(); start += 1000) {
    std::vector<sde::Row> rows;
    for (std::size_t n = start; n < start + 1000; ++n) {
      rows.push_back({{"id", ids[n]}, {"account", "a-" + std::to_string(n % 97)}});
    }
    session.save_many("Order", rows);
  }
  scope.admin().run("OPTIMIZE TABLE `order` FINAL");
  sde::ScanOptions options;
  options.where = sde::Row{{"id", ids[12345]}};
  options.limit = 5;
  const sde::ScanPage page = session.scan("Order", options);
  ASSERT_EQ(page.rows.size(), 1U);
  EXPECT_EQ(page.rows[0].at("id"), ids[12345]);
  const auto& read = spy.reads.back();
  const std::string statement = sde::read_sql(
      read.table, read.plan, "clickhouse",
      [](const sde::Value& value) { return sde::detail::clickhouse::literal(value); }, read.count);
  std::string text;
  for (const sde::Json& row : scope.admin().rows("EXPLAIN indexes = 1 " + statement)) {
    text += row.find("explain")->as_string() + "\n";
  }
  std::smatch granules;
  ASSERT_TRUE(std::regex_search(text, granules, std::regex(R"(Granules: (\d+)/(\d+))"))) << text;
  EXPECT_GT(std::stoi(granules[2].str()), 1) << text;
  EXPECT_EQ(std::stoi(granules[1].str()), 1) << text;
}

// --- temporal keys ------------------------------------------------------------------------------

/// The reference's readings keyed by a hostile station and a moment: four rows a microsecond apart,
/// in 2026 or before the epoch, of `kind` - `timestamp` or `timestamptz`.
struct Temporal {
  ClickHouseRoles roles;
  sde::Model model;
  std::unique_ptr<sde::ClickHouseEngine> provisioning;
  std::unique_ptr<sde::ClickHouseEngine> runtime;
  std::vector<sde::Row> rows;

  Temporal(const std::string& dsn, const std::string& kind, bool before_epoch = false)
      : roles(dsn),
        model(sde::load_neutral_model(R"({"entities": [{"name": "Reading", "fields": [
            {"name": "station", "type": "string"}, {"name": "at", "type": ")" + kind + R"("},
            {"name": "value", "type": "int32"}], "key": ["station", "at"]}]})")) {
    const sde::PhysicalLayout layout =
        sde::default_layout(model, model.groups().front(), "clickhouse");
    provisioning = connected(roles.operator_dsn());
    (void)provisioning->ensure_schema(layout, {{"Reading", {"station", "at"}}});
    roles.grant("reading");
    runtime = connected(roles.runtime_dsn());
    const std::int64_t base = before_epoch ? -135604800000000 + 123456  // 1965-09-14T12:00:00.123456
                                           : 1789387200000000 + 123456;  // 2026-09-14T12:00:00.123456
    for (std::int64_t n = 0; n < 4; ++n) {
      sde::Value at = kind == "timestamptz" ? sde::Value(*sde::TimestampTz::from_micros(base + n))
                                            : sde::Value(*sde::Timestamp::from_micros(base + n));
      rows.push_back({{"station", std::string("station'\\\\\xC3\xA9")}, {"at", at}, {"value", n}});
    }
    for (const sde::Row& row : rows) provisioning->insert("reading", row);
  }
  [[nodiscard]] static sde::Row key_of(const sde::Row& row) {
    return {{"station", row.at("station")}, {"at", row.at("at")}};
  }
};

TEST_F(ClickHouseReads, PointReadsKeepFractionalTemporalKeys) {
  for (const char* kind : {"timestamp", "timestamptz"}) {
    SCOPED_TRACE(kind);
    Temporal temporal(dsn_, kind);
    EXPECT_EQ(temporal.provisioning->count("reading"), temporal.rows.size());
    for (const sde::Row& expected : temporal.rows) {
      EXPECT_EQ(temporal.runtime->get("reading", Temporal::key_of(expected)), expected);
    }
  }
}

TEST_F(ClickHouseReads, AKeysetCursorAdvancesAndItsUpperBoundIsInclusive) {
  for (const char* kind : {"timestamp", "timestamptz"}) {
    SCOPED_TRACE(kind);
    Temporal temporal(dsn_, kind);
    const std::vector<sde::Row>& rows = temporal.rows;
    const std::vector<std::string> order{"station", "at"};
    const std::vector<sde::Row> first =
        temporal.runtime->key_range("reading", order, std::nullopt, std::nullopt, 1);
    ASSERT_EQ(first, std::vector<sde::Row>(rows.begin(), rows.begin() + 1));
    const std::vector<sde::Value> cursor{first[0].at("station"), first[0].at("at")};
    EXPECT_EQ(temporal.runtime->key_range("reading", order, cursor, std::nullopt, 1),
              std::vector<sde::Row>(rows.begin() + 1, rows.begin() + 2));
    EXPECT_EQ(temporal.runtime->key_range("reading", order, std::nullopt, cursor, std::nullopt),
              std::vector<sde::Row>(rows.begin(), rows.begin() + 1));
    const std::vector<sde::Value> high{rows[2].at("station"), rows[2].at("at")};
    EXPECT_EQ(temporal.runtime->key_range("reading", order, cursor, high, std::nullopt),
              std::vector<sde::Row>(rows.begin() + 1, rows.begin() + 3));
  }
}

TEST_F(ClickHouseReads, AnInstantKeyIsTheInstantWhateverItsOffset) {
  // Kathmandu is +05:45: a key given there is the same instant, and finds the row.
  Temporal temporal(dsn_, "timestamptz");
  const sde::Row& expected = temporal.rows[0];
  const std::int64_t micros = std::get<sde::TimestampTz>(expected.at("at")).micros();
  ASSERT_EQ(*sde::TimestampTz::parse("2026-09-14T17:45:00.123456+05:45"),
            *sde::TimestampTz::from_micros(micros));
  EXPECT_EQ(temporal.runtime->get(
                "reading", {{"station", expected.at("station")},
                            {"at", *sde::TimestampTz::parse("2026-09-14T17:45:00.123456+05:45")}}),
            expected);
}

TEST_F(ClickHouseReads, APreEpochTemporalKeyKeepsItsExactFraction) {
  for (const char* kind : {"timestamp", "timestamptz"}) {
    SCOPED_TRACE(kind);
    Temporal temporal(dsn_, kind, true);
    const sde::Row& expected = temporal.rows[0];
    EXPECT_EQ(temporal.provisioning->get("reading", Temporal::key_of(expected)), expected);
  }
}

TEST_F(ClickHouseReads, AWallClockKeyIsUtcWhateverTheProcessTimeZone) {
  // Nothing here reads the process's zone; the reference proves its driver does not either.
  Temporal temporal(dsn_, "timestamp");
  const char* saved = std::getenv("TZ");
  const std::optional<std::string> previous = saved ? std::optional<std::string>(saved) : std::nullopt;
  ::setenv("TZ", "Pacific/Honolulu", 1);
  ::tzset();
  const std::optional<sde::Row> found =
      temporal.runtime->get("reading", Temporal::key_of(temporal.rows[0]));
  if (previous) {
    ::setenv("TZ", previous->c_str(), 1);
  } else {
    ::unsetenv("TZ");
  }
  ::tzset();
  EXPECT_EQ(found, temporal.rows[0]);
}

}  // namespace
