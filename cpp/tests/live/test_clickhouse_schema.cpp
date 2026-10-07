/// Schemas against a real ClickHouse: what `prepare_schema` creates - the sort key, the partition
/// and the data-skipping indexes - what a table of another design costs where it is provisioned and
/// in a running session, what the catalogue check refuses and what it only names, a runtime login
/// that holds no DDL right and cannot read the index catalogue, and every `schema/` vector through
/// this adapter. Ported from the reference's `test_physical_live.py`,
/// `test_runtime_privileges_live.py`, `test_schema_vectors_live.py` and the schema half of
/// `test_clickhouse_slice.py`. Every expected text is the reference's, captured from it against the
/// same server; the catalogue is read back with SQL written here, not with the adapter.
///
///     SDE_CLICKHOUSE_DSN=clickhouse://default:sde@127.0.0.1:58123/sde ctest -L live

#include <cstdint>
#include <map>
#include <memory>
#include <optional>
#include <set>
#include <string>
#include <tuple>
#include <utility>
#include <vector>

#include <gtest/gtest.h>

#include "live/capture.hpp"
#include "live/clickhouse.hpp"
#include "live/http_stub.hpp"
#include "live/live.hpp"
#include "sde/clickhouse.hpp"
#include "sde/errors.hpp"
#include "sde/json.hpp"
#include "sde/layout.hpp"
#include "sde/model.hpp"
#include "sde/physical.hpp"
#include "sde/placement.hpp"
#include "sde/provisioning.hpp"
#include "sde/schema.hpp"
#include "sde/session.hpp"
#include "sde/write_fence.hpp"
#include "support/signer.hpp"
#include "support/vectors.hpp"

namespace {

using sde::live::Captured;
using sde::live::ClickHouseAdmin;
using sde::live::ClickHouseRoles;
using sde::live::ClickHouseScope;
using sde::live::refusal;

const std::string kProject(32, '1');

const std::string kUnverified =
    "unverified: this login cannot read system.data_skipping_indices (GRANT SELECT ON "
    "system.data_skipping_indices to verify it)";

const std::string kTypeTail =
    ". `CREATE TABLE IF NOT EXISTS` keeps a table of that name whatever shape it is in, and this "
    "library never alters a column's type - so the table came from somewhere else, or from a map "
    "that rendered this column differently. Refusing rather than writing into it: with a timestamp "
    "the difference is usually precision, and a write that succeeds and comes back rounded is worse "
    "than one that fails.";

sde::Model readings() {
  return sde::load_neutral_model(R"({"entities": [{"name": "Reading", "fields": [
      {"name": "at", "type": "timestamptz"}, {"name": "humidity", "type": "int32"},
      {"name": "station", "type": "string"}, {"name": "temperature", "type": "float64"}],
      "key": ["station", "at"]}]})");
}

const sde::Keys kReadingKeys{{"Reading", {"station", "at"}}};

/// The reference's map: `readings` in engine `db`, generation 1, and with `design` its physical
/// design - the key ordered by time, a monthly partition on it, and three data-skipping indexes.
sde::PlacementMap readings_map(const sde::Model& model, bool design, int version = 1,
                               int contract = 5) {
  std::string layout = R"json({"tables": {"Reading": "readings"}, "columns": {"Reading": {
      "at": "DateTime64(6, 'UTC')", "humidity": "Int32", "station": "String",
      "temperature": "Float64"}}})json";
  if (design) {
    layout.pop_back();
    layout += R"(, "key_order": {"Reading": ["at", "station"]},
        "partition_by": {"Reading": {"field": "at", "granularity": "month"}}, "indexes": [
        {"entity": "Reading", "name": "reading_temperature", "columns": ["temperature"],
         "method": "minmax", "granularity": 4},
        {"entity": "Reading", "name": "reading_humidity", "columns": ["humidity"],
         "method": "set", "granularity": 2, "max_rows": 100},
        {"entity": "Reading", "name": "reading_station", "columns": ["station"],
         "method": "bloom_filter", "granularity": 1}]})";
  }
  const std::string fenced = contract >= 4 ? R"("write_epoch": 1, )" : "";
  const std::string project =
      contract >= 4 ? R"("project_id": ")" + kProject + R"(", )" : std::string();
  sde::LoadOptions options;
  options.model = &model;
  return sde::load_map(R"({"contract": )" + std::to_string(contract) + ", " + project +
                           R"("model_version": ")" + model.version() + R"(", "map_version": )" +
                           std::to_string(version) + R"(, "groups": {"Reading": {)" + fenced +
                           R"("source": {"id": "s", "engine": "db", "layout": )" + layout +
                           "}}}}",
                       options);
}

/// A finding as a tuple, so a list of them prints whole when it differs.
using Finding = std::tuple<std::string, std::string, std::string, std::string>;
std::vector<Finding> as_tuples(const std::vector<sde::PhysicalFinding>& findings) {
  std::vector<Finding> out;
  for (const sde::PhysicalFinding& finding : findings) {
    out.emplace_back(finding.table, finding.aspect, finding.declared, finding.found);
  }
  return out;
}

/// The findings of the reference's design against a table created without it, the indexes'
/// `found` given: "absent" to a login that can read the index catalogue, `kUnverified` to one that
/// cannot.
std::vector<Finding> undesigned(const std::string& indexes) {
  return {{"readings", "sort key", "['at', 'station']", "('station', 'at')"},
          {"readings", "partition", "('toYYYYMM', 'at')", "None"},
          {"readings", "index reading_humidity", "set(100) on ['humidity'] granularity 2", indexes},
          {"readings", "index reading_station", "bloom_filter on ['station'] granularity 1", indexes},
          {"readings", "index reading_temperature", "minmax on ['temperature'] granularity 4",
           indexes}};
}

/// A server's refusal as the reference's driver words it.
std::string server_said(int code, const std::string& what, const std::string& version,
                        const std::string& url) {
  return "Received ClickHouse exception, code: " + std::to_string(code) +
         ", server response: Code: " + std::to_string(code) + ". DB::Exception: " + what +
         " (version " + version + " (official build)) (for url " + url + ")";
}

class ClickHouseSchema : public ::testing::Test {
 protected:
  void SetUp() override { SDE_REQUIRE_DSN("SDE_CLICKHOUSE_DSN", dsn_); }

  /// An engine on `dsn`, connected, with its events in `log` when one is given.
  static std::unique_ptr<sde::ClickHouseEngine> engine(const std::string& dsn,
                                                       Captured* log = nullptr) {
    sde::ClickHouseOptions options;
    if (log != nullptr) options.log = log->sink();
    auto made = std::make_unique<sde::ClickHouseEngine>(dsn, std::move(options));
    made->connect();
    return made;
  }

  std::string dsn_;
};

// --- a designed layout --------------------------------------------------------------------------

TEST_F(ClickHouseSchema, ADesignedLayoutIsCreatedAsDeclared) {
  const ClickHouseRoles roles(dsn_);
  const sde::Model model = readings();
  const sde::PlacementMap map = readings_map(model, true);
  const auto provisioning = engine(roles.operator_dsn());
  sde::prepare_schema(model, map, {{"db", provisioning.get()}}, kProject);

  const std::vector<sde::Json> table = roles.rows(
      "SELECT sorting_key, partition_key FROM system.tables "
      "WHERE database = currentDatabase() AND name = 'readings'");
  ASSERT_EQ(table.size(), 1U);
  EXPECT_EQ(table[0].find("sorting_key")->as_string(), "at, station");
  EXPECT_EQ(table[0].find("partition_key")->as_string(), "toYYYYMM(at)");
  std::map<std::string, std::tuple<std::string, std::string, std::string>> indexes;
  for (const sde::Json& row : roles.rows(
           "SELECT name, type_full, expr, toString(granularity) AS granularity "
           "FROM system.data_skipping_indices WHERE database = currentDatabase() "
           "AND table = 'readings'")) {
    indexes[row.find("name")->as_string()] = {row.find("type_full")->as_string(),
                                              row.find("expr")->as_string(),
                                              row.find("granularity")->as_string()};
  }
  EXPECT_EQ(indexes, (std::map<std::string, std::tuple<std::string, std::string, std::string>>{
                         {"reading_humidity", {"set(100)", "humidity", "2"}},
                         {"reading_station", {"bloom_filter", "station", "1"}},
                         {"reading_temperature", {"minmax", "temperature", "4"}}}));

  const sde::PhysicalLayout& layout = map.placement_of("Reading").source.layout;
  EXPECT_TRUE(provisioning->validate_schema(layout, kReadingKeys).empty());
}

TEST_F(ClickHouseSchema, AnotherDesignIsRefusedAtProvisioningAndReportedByARunningSession) {
  const ClickHouseRoles roles(dsn_);
  const sde::Model model = readings();
  const sde::PlacementMap first = readings_map(model, false);
  const auto provisioning = engine(roles.operator_dsn());
  sde::prepare_schema(model, first, {{"db", provisioning.get()}}, kProject);
  const sde::PlacementMap second = readings_map(model, true, 2);

  // A person provisioning the new map over the old tables is told, table and aspect named.
  EXPECT_EQ(
      refusal<sde::EngineError>(
          [&] { sde::prepare_schema(model, second, {{"db", provisioning.get()}}, kProject); }),
      "existing tables differ from the physical design this map declares: readings: sort key is "
      "('station', 'at') and the map declares ['at', 'station']; readings: partition is None and "
      "the map declares ('toYYYYMM', 'at'); readings: index reading_humidity is absent and the map "
      "declares set(100) on ['humidity'] granularity 2; readings: index reading_station is absent "
      "and the map declares bloom_filter on ['station'] granularity 1; readings: index "
      "reading_temperature is absent and the map declares minmax on ['temperature'] granularity 4. "
      "`CREATE ... IF NOT EXISTS` keeps whatever table or index already has the name, so this came "
      "from an earlier map or from outside SDE. A new layout needs fresh tables (staging), not the "
      "old ones under the new declaration.");

  // An application started on the same new map keeps serving rows and says what it found. Its
  // login cannot read the index catalogue: unverified, not absent, and not a session that failed
  // to start - the regression the first version of this check caused.
  roles.grant("readings");
  Captured log;
  const auto runtime = engine(roles.runtime_dsn(), &log);
  sde::SessionOptions options;
  options.log = log.sink();
  options.project_id = kProject;
  sde::Session session(model, second, {{"db", runtime.get()}}, options);
  EXPECT_EQ(as_tuples(session.physical()), undesigned(kUnverified));
  // The same two events the reference writes, the first naming the generation's own column
  // (a finding about the reference, recorded for all three libraries).
  EXPECT_EQ(log.names(),
            (std::vector<std::string>{"sde.schema.extra_columns", "sde.schema.physical_mismatch"}));
  EXPECT_EQ(log.of("sde.schema.extra_columns"),
            (std::vector<std::string>{R"({"columns":["__sde_write_epoch"],"table":"readings"})"}));
  EXPECT_EQ(log.of("sde.schema.physical_mismatch"),
            (std::vector<std::string>{R"({"findings":5,"tables":["readings"]})"}));
  const sde::Row row{{"station", std::string("s1")},
                     {"at", *sde::TimestampTz::parse("2026-09-23T12:00:00.123456Z")},
                     {"humidity", std::int64_t{40}},
                     {"temperature", 21.5}};
  session.save("Reading", row);
  EXPECT_EQ(session.get("Reading", {{"station", row.at("station")}, {"at", row.at("at")}}), row);

  // The operator, who can read the index catalogue, finds the same table and the indexes absent.
  const sde::PhysicalLayout& layout = second.placement_of("Reading").source.layout;
  EXPECT_EQ(as_tuples(provisioning->validate_schema(layout, kReadingKeys)), undesigned("absent"));
}

TEST_F(ClickHouseSchema, AnIndexOfTheDeclaredNameButAnotherShapeIsNamedAsItIs) {
  const ClickHouseRoles roles(dsn_);
  const sde::Model model = readings();
  const auto provisioning = engine(roles.operator_dsn());
  sde::prepare_schema(model, readings_map(model, false), {{"db", provisioning.get()}}, kProject);
  roles.command(
      "ALTER TABLE readings ADD INDEX reading_temperature humidity TYPE set(7) GRANULARITY 3");
  roles.command("ALTER TABLE readings ADD INDEX reading_station (station, humidity) "
                "TYPE bloom_filter(0.01) GRANULARITY 1");
  const sde::PlacementMap designed = readings_map(model, true, 2);
  std::vector<Finding> expected = undesigned("absent");
  std::get<3>(expected[3]) = "bloom_filter(0.01) on ('station', 'humidity') granularity 1";
  std::get<3>(expected[4]) = "set(7) on ('humidity',) granularity 3";
  EXPECT_EQ(as_tuples(provisioning->validate_schema(
                designed.placement_of("Reading").source.layout, kReadingKeys)),
            expected);
}

TEST_F(ClickHouseSchema, AMatchingTableReportsNothingToARestrictedSessionThatMayReadTheIndexes) {
  const ClickHouseRoles roles(dsn_);
  const sde::Model model = readings();
  const sde::PlacementMap map = readings_map(model, true);
  const auto provisioning = engine(roles.operator_dsn());
  sde::prepare_schema(model, map, {{"db", provisioning.get()}}, kProject);
  roles.grant("readings");
  // With the optional grant the index catalogue is readable and the design is verified.
  roles.command("GRANT SELECT ON system.data_skipping_indices TO `" + roles.username() + "`");
  Captured log;
  const auto runtime = engine(roles.runtime_dsn(), &log);
  sde::SessionOptions options;
  options.log = log.sink();
  options.project_id = kProject;
  const sde::Session session(model, map, {{"db", runtime.get()}}, options);
  EXPECT_TRUE(session.physical().empty());
  EXPECT_TRUE(log.of("sde.schema.physical_mismatch").empty());
}

TEST_F(ClickHouseSchema, ThePartitionRuleStillHoldsOnThisClickHouse) {
  // The measurement behind a partition having to follow the key, repeated on this server: two
  // writes of one key are one row after OPTIMIZE FINAL when the partition is on the key, and stay
  // two when it is on a column outside it - which the map format refuses, so the control table is
  // made by hand. If a release ever collapsed across partitions, this says the refusal is moot.
  const ClickHouseScope scope(dsn_);
  const ClickHouseAdmin admin = scope.admin();
  std::map<std::string, std::string> rows;
  for (const auto& [name, partition] : std::vector<std::pair<std::string, std::string>>{
           {"on_key", "toYYYYMM(at)"}, {"off_key", "humidity"}}) {
    admin.run("CREATE TABLE " + name + " (station String, at DateTime64(6, 'UTC'), humidity "
              "Int32) ENGINE = ReplacingMergeTree PARTITION BY " + partition +
              " ORDER BY (station, at)");
    for (const char* humidity : {"10", "20"}) {
      admin.run("INSERT INTO " + name + " VALUES ('s1', '2026-09-23 12:00:00.000000', " +
                humidity + ")");
    }
    admin.run("OPTIMIZE TABLE " + name + " FINAL");
    rows[name] = admin.rows("SELECT toString(count()) AS n FROM " + name)[0].find("n")->as_string();
  }
  EXPECT_EQ(rows, (std::map<std::string, std::string>{{"off_key", "2"}, {"on_key", "1"}}));
}

TEST_F(ClickHouseSchema, ACompositeKeyKeepsItsDeclaredOrder) {
  // ORDER BY is the key as declared, not sorted: ClickHouse prunes on a prefix of it, so
  // (region, at) and (at, region) are two tables with the same map.
  const ClickHouseScope scope(dsn_);
  const sde::Model model = sde::load_neutral_model(R"json({"entities": [{"name": "Reading",
      "fields": [{"name": "region", "type": "string"}, {"name": "at", "type": "timestamptz"},
      {"name": "value", "type": "decimal(10,2)"}], "key": ["region", "at"]}]})json");
  const sde::PlacementMap map = sde::live::default_map(model, "clickhouse");
  const auto ch = engine(scope.dsn());
  (void)ch->ensure_schema(map.placement_of("Reading").source.layout,
                          {{"Reading", model.entity("Reading").key}});
  EXPECT_EQ(scope.admin()
                .rows("SELECT sorting_key FROM system.tables WHERE database = currentDatabase() "
                      "AND name = 'reading'")[0]
                .find("sorting_key")
                ->as_string(),
            "region, at");
  const sde::Value at = *sde::TimestampTz::parse("2026-08-27T12:00:00Z");
  ch->insert("reading", {{"region", std::string("eu")}, {"at", at}, {"value", sde::Decimal("1.50")}});
  EXPECT_EQ(ch->get("reading", {{"region", std::string("eu")}, {"at", at}})->at("value"),
            sde::Value(sde::Decimal("1.50")));
}

TEST_F(ClickHouseSchema, ACompatibilityViewReadsFinalAndSoCountsEntities) {
  // A hand-written query moved to a ClickHouse target reads a ReplacingMergeTree without FINAL and
  // counts a key written twice twice. The view stands in for the old name, so it must read FINAL,
  // or it would look like the thing that made the old query safe. Merges are stopped: a background
  // merge would collapse the duplicate and a view without FINAL would pass.
  const ClickHouseScope scope(dsn_);
  const sde::Model model = sde::load_neutral_model(R"json({"entities": [{"name": "Event", "fields": [
      {"name": "id", "type": "uuid"}, {"name": "name", "type": "string"}], "key": ["id"]}]})json");
  const sde::PlacementMap map = sde::live::default_map(model, "clickhouse");
  const sde::PhysicalLayout& layout = map.placement_of("Event").source.layout;
  const auto ch = engine(scope.dsn());
  (void)ch->ensure_schema(layout, {{"Event", {"id"}}});
  const sde::CompatibilityViews views =
      sde::compatibility_views(layout, {{"Event", "old_event"}}, "clickhouse");
  ASSERT_FALSE(views.create.empty());
  const ClickHouseAdmin admin = scope.admin();
  admin.run("SYSTEM STOP MERGES `event`");
  for (const std::string& statement : views.create) {
    admin.run(statement);
    admin.run(statement);  // rendering is idempotent by construction
  }
  const sde::Value id = *sde::Uuid::parse("6ba7b810-9dad-11d1-80b4-00c04fd430c8");
  ch->insert("event", {{"id", id}, {"name", std::string("first")}});
  ch->insert("event", {{"id", id}, {"name", std::string("second")}});
  const auto count = [&](const std::string& table) {
    return admin.rows("SELECT toString(count()) AS n FROM `" + table + "`")[0].find("n")->as_string();
  };
  ASSERT_EQ(count("event"), "2") << "two parts were expected; the rest is about the view";
  EXPECT_EQ(count("old_event"), "1") << "the view returned the duplicate, so it reads without FINAL";
  EXPECT_EQ(admin.rows("SELECT name FROM `old_event`")[0].find("name")->as_string(), "second");
  for (const std::string& statement : views.drop) admin.run(statement);
  admin.run("SYSTEM START MERGES `event`");
  EXPECT_TRUE(admin.rows("SELECT name FROM system.tables WHERE database = currentDatabase() AND "
                         "name = 'old_event'")
                  .empty());
}

// --- the catalogue check ------------------------------------------------------------------------

TEST_F(ClickHouseSchema, ATableMissingAColumnIsRefusedNamingBoth) {
  const ClickHouseScope scope(dsn_);
  scope.admin().run("CREATE TABLE readings (station String, at DateTime64(6, 'UTC')) "
                    "ENGINE = MergeTree ORDER BY station");
  const sde::Model model = readings();
  const sde::PlacementMap map = readings_map(model, false, 1, 3);
  const auto ch = engine(scope.dsn());
  EXPECT_EQ(refusal<sde::EngineError>([&] {
              (void)ch->ensure_schema(map.placement_of("Reading").source.layout, kReadingKeys);
            }),
            "'readings' already existed with a different shape: the map needs ['humidity', "
            "'temperature'] and the table has ['at', 'station']. `CREATE TABLE IF NOT EXISTS` keeps "
            "whatever is there, so this table came from somewhere else. Refusing here rather than at "
            "the first insert, which would fail in your request path with an error naming a column "
            "and not the cause.");
}

TEST_F(ClickHouseSchema, AMomentOfAnotherPrecisionIsAnotherType) {
  // The catalogue writes back exactly the type this library rendered, so the comparison is
  // literal: DateTime64(3) where the map says DateTime64(6) would round every write.
  const ClickHouseScope scope(dsn_);
  scope.admin().run("CREATE TABLE readings (station String, at DateTime64(3, 'UTC'), humidity "
                    "Int32, temperature Float64) ENGINE = MergeTree ORDER BY station");
  const sde::Model model = readings();
  const sde::PlacementMap map = readings_map(model, false, 1, 3);
  const auto ch = engine(scope.dsn());
  EXPECT_EQ(refusal<sde::EngineError>([&] {
              (void)ch->ensure_schema(map.placement_of("Reading").source.layout, kReadingKeys);
            }),
            "readings.at is \"DateTime64(3, 'UTC')\" and this map declares it \"DateTime64(6, "
            "'UTC')\"" + kTypeTail);
}

TEST_F(ClickHouseSchema, ATypesModifierIsPartOfTheType) {
  const ClickHouseScope scope(dsn_);
  scope.admin().run("CREATE TABLE prices (id Int64, amount Decimal(8, 2)) "
                    "ENGINE = ReplacingMergeTree ORDER BY id");
  const sde::Model model = sde::load_neutral_model(R"json({"entities": [{"name": "Price", "fields": [
      {"name": "id", "type": "int64"}, {"name": "amount", "type": "decimal(12,2)"}],
      "key": ["id"]}]})json");
  sde::LoadOptions options;
  options.model = &model;
  const sde::PlacementMap map = sde::load_map(
      R"({"contract": 3, "model_version": ")" + model.version() + R"json(", "map_version": 1,
          "groups": {"Price": {"source": {"id": "p", "engine": "db", "layout": {
          "tables": {"Price": "prices"},
          "columns": {"Price": {"id": "Int64", "amount": "Decimal(12, 2)"}}}}}}})json",
      options);
  const auto ch = engine(scope.dsn());
  EXPECT_EQ(refusal<sde::EngineError>([&] {
              (void)ch->ensure_schema(map.placement_of("Price").source.layout,
                                      {{"Price", {"id"}}});
            }),
            "prices.amount is 'Decimal(8, 2)' and this map declares it 'Decimal(12, 2)'" + kTypeTail);
}

TEST_F(ClickHouseSchema, AnExtraColumnIsNamedNotRefused) {
  const ClickHouseScope scope(dsn_);
  scope.admin().run("CREATE TABLE readings (station String, at DateTime64(6, 'UTC'), humidity "
                    "Int32, temperature Float64, zeta String, alpha Int64) "
                    "ENGINE = ReplacingMergeTree ORDER BY (station, at)");
  const sde::Model model = readings();
  const sde::PlacementMap map = readings_map(model, false, 1, 3);
  Captured log;
  const auto ch = engine(scope.dsn(), &log);
  EXPECT_TRUE(ch->validate_schema(map.placement_of("Reading").source.layout, kReadingKeys).empty());
  EXPECT_EQ(log.names(), std::vector<std::string>{"sde.schema.extra_columns"});
  EXPECT_EQ(log.of("sde.schema.extra_columns"),
            (std::vector<std::string>{R"({"columns":["alpha","zeta"],"table":"readings"})"}));
}

TEST_F(ClickHouseSchema, ALoginGrantedNothingSeesNoTable) {
  // system.columns lists only what the login may read, so a runtime login without its grant is
  // told the table is missing - "a permissions or database-selection problem", as the text says.
  const ClickHouseRoles roles(dsn_);
  const sde::Model model = readings();
  const sde::PlacementMap map = readings_map(model, false, 1, 3);
  const auto provisioning = engine(roles.operator_dsn());
  sde::prepare_schema(model, map, {{"db", provisioning.get()}}, kProject);
  const auto runtime = engine(roles.runtime_dsn());
  EXPECT_EQ(refusal<sde::EngineError>([&] {
              (void)runtime->validate_schema(map.placement_of("Reading").source.layout,
                                             kReadingKeys);
            }),
            "'readings' does not exist after applying the schema. The statement reported success, "
            "so this is a permissions or database-selection problem rather than a bad map.");
}

TEST_F(ClickHouseSchema, ALayoutCarryingIndexesForAnotherDialectIsRefusedInASentence) {
  // A map derived for PostgreSQL handed to this engine: CREATE INDEX here builds a data-skipping
  // index with a type and a granularity, so a B-tree is not something to approximate.
  const ClickHouseScope scope(dsn_);
  const sde::Model model = sde::load_neutral_model(R"json({"entities": [{"name": "Event", "fields": [
      {"name": "id", "type": "uuid"}, {"name": "name", "type": "string"}], "key": ["id"]}]})json");
  sde::PhysicalLayout wrong = sde::default_layout(model, model.groups().front(), "postgres");
  sde::Index index;
  index.entity = "Event";
  index.name = "i";
  index.columns = {"id"};
  wrong.indexes.push_back(index);
  const auto ch = engine(scope.dsn());
  EXPECT_EQ(refusal<sde::EngineError>([&] { (void)ch->ensure_schema(wrong, {{"Event", {"id"}}}); }),
            "the layout carries 1 index definitions and this engine has no B-tree to put them in. A "
            "ClickHouse index is a data-skipping index with a type and a granularity, so this map "
            "was built for another dialect.");
  const sde::PhysicalLayout right = sde::default_layout(model, model.groups().front(), "clickhouse");
  EXPECT_EQ(refusal<sde::EngineError>(
                [&] { (void)ch->ensure_schema(right, {{"Event", {"not_a_column"}}}); }),
            "the key of 'Event' names columns the layout does not have: ['not_a_column']. In "
            "ClickHouse the key becomes ORDER BY, so this would produce a table that cannot be "
            "created rather than one with a missing constraint.");
  EXPECT_TRUE(scope.admin().rows("SELECT name FROM system.tables WHERE database = currentDatabase()")
                  .empty())
      << "a refused layout created something";
}

// --- what the check lets through ----------------------------------------------------------------

/// One table of one column, for the failures below.
sde::PhysicalLayout one_table() {
  sde::PhysicalLayout layout;
  layout.tables = {{"Event", "events"}};
  layout.columns = {{"Event", {{"id", "Int64"}}}};
  return layout;
}

TEST_F(ClickHouseSchema, AServerRefusalInTheCheckIsAnEngineErrorInTheServersWords) {
  // The reference lets its driver's own error through here, unwrapped, in these words; this
  // library's is an EngineError with the same text, so nothing outside the contract's classes
  // leaves the adapter. The database is dropped under a connected engine.
  const ClickHouseScope scope(dsn_);
  const auto ch = engine(scope.dsn());
  scope.root().run("DROP DATABASE `" + scope.name() + "` SYNC");
  const std::string said =
      server_said(81, "Database " + scope.name() + " does not exist. (UNKNOWN_DATABASE)",
                  ch->server_version(), sde::live::url_of(scope.dsn()));
  EXPECT_EQ(refusal<sde::EngineError>(
                [&] { (void)ch->validate_schema(one_table(), {{"Event", {"id"}}}); }),
            said);
  EXPECT_EQ(refusal<sde::EngineError>([&] { (void)ch->validate_schema(one_table(), {}); }), said);
}

TEST_F(ClickHouseSchema, ATransportFailureInTheCheckIsAnEngineErrorWithItsOutcomeUnknown) {
  // A server that answers the handshake - and for `ensure_schema` the CREATE TABLE - and then
  // closes the connection on the catalogue read. Nothing is sent again.
  const std::string lost =
      "ClickHouse transport failed; the operation was not replayed and its outcome may be unknown";
  {
    sde::live::ScriptedServer server({sde::live::version_answer("24.8.14.39"), ""});
    sde::ClickHouseEngine ch("clickhouse://fixture:canary@127.0.0.1:" +
                             std::to_string(server.port()) + "/default");
    ch.connect();
    EXPECT_EQ(refusal<sde::EngineError>(
                  [&] { (void)ch.validate_schema(one_table(), {{"Event", {"id"}}}); }),
              lost);
    EXPECT_EQ(server.requests().size(), 2U);
  }
  {
    sde::live::ScriptedServer server(
        {sde::live::version_answer("24.8.14.39"), sde::live::http_answer("200 OK", ""), ""});
    sde::ClickHouseEngine ch("clickhouse://fixture:canary@127.0.0.1:" +
                             std::to_string(server.port()) + "/default");
    ch.connect();
    EXPECT_EQ(refusal<sde::EngineError>(
                  [&] { (void)ch.ensure_schema(one_table(), {{"Event", {"id"}}}); }),
              lost);
    const std::vector<std::string> requests = server.requests();
    ASSERT_EQ(requests.size(), 3U);
    EXPECT_NE(requests[1].find("CREATE TABLE IF NOT EXISTS"), std::string::npos) << requests[1];
  }
}

// --- the schema vectors -------------------------------------------------------------------------

TEST_F(ClickHouseSchema, EverySchemaVectorIsCreatedByThisAdapterAndVerifiesClean) {
  // As for PostgreSQL: each ClickHouse case through `ensure_schema`, twice as a restart does, in a
  // database of its own, and its names read back from the catalogue - inside backticks ClickHouse
  // reads a backslash as an escape, so an accepted statement can create another name.
  using namespace sde::testing_support;
  std::size_t ran = 0;
  for (const std::string& name : cases("schema")) {
    const auto directory = vectors_root() / "schema" / name;
    const sde::Model model = sde::load_neutral_model(read_json(directory / "model.json"));
    sde::LoadOptions options;
    options.model = &model;
    const sde::PlacementMap map = sde::load_map(read_json(directory / "map.json"), options);
    const sde::Json expectations = read_json(directory / "cases.json");
    for (const sde::Json& expectation : expectations.as_array()) {
      if (const sde::Json* runs = expectation.find("runs"); runs != nullptr && !runs->as_bool()) {
        EXPECT_NE(expectation.find("dialect")->as_string(), "clickhouse")
            << "a ClickHouse case says it cannot run, and this test would never see it";
        continue;
      }
      if (expectation.find("dialect")->as_string() != "clickhouse" ||
          expectation.find("error") != nullptr) {
        continue;
      }
      const std::string& id = expectation.find("materialization")->as_string();
      SCOPED_TRACE("schema/" + name + " " + id);
      std::string group_name;
      const sde::Materialization* material = nullptr;
      for (const auto& [group, spot] : map.groups()) {
        for (const sde::Materialization* candidate : spot.all()) {
          if (candidate->id == id) {
            group_name = group;
            material = candidate;
          }
        }
      }
      ASSERT_NE(material, nullptr);
      sde::PhysicalLayout layout = material->layout;
      if (const sde::Json* columns = expectation.find("layout_columns")) {
        layout.columns.clear();
        for (const auto& [entity, types] : columns->as_object()) {
          for (const auto& [column, type] : types.as_object()) {
            layout.columns[entity][column] = type.as_string();
          }
        }
      }
      sde::Keys keys;
      if (const sde::Json* given = expectation.find("keys")) {
        for (const auto& [entity, key] : given->as_object()) {
          for (const sde::Json& field : key.as_array()) keys[entity].push_back(field.as_string());
        }
      } else {
        for (const sde::Group& group : model.groups()) {
          if (group.name != group_name) continue;
          for (const std::string& member : group.members) keys[member] = model.entity(member).key;
        }
      }
      const ClickHouseScope scope(dsn_);
      const auto ch = engine(scope.dsn());
      EXPECT_EQ(ch->ensure_schema(layout, keys), std::vector<sde::PhysicalFinding>{});
      EXPECT_EQ(ch->ensure_schema(layout, keys), std::vector<sde::PhysicalFinding>{});
      std::map<std::string, std::set<std::string>> found;
      for (const sde::Json& row : scope.admin().rows(
               "SELECT table, name FROM system.columns WHERE database = currentDatabase()")) {
        found[row.find("table")->as_string()].insert(row.find("name")->as_string());
      }
      for (const auto& [entity, table] : layout.tables) {
        ASSERT_EQ(found.count(table), 1U)
            << "no table called " << table << ": the statement ran and the name is not the one "
            << "the layout declared, which is what a wrong identifier escaper looks like";
        const auto declared = layout.columns.find(entity);
        if (declared == layout.columns.end()) continue;
        for (const auto& [column, unused] : declared->second) {
          EXPECT_EQ(found[table].count(column), 1U) << table << " has no column " << column;
        }
      }
      ++ran;
    }
  }
  EXPECT_GT(ran, 0U) << "a test that runs nothing passes for the wrong reason";
}

// --- a runtime login without DDL ----------------------------------------------------------------

/// The reference's contract-4 map: one entity, `events`, generation 1, map version 7, signed when
/// a signer is given.
sde::PlacementMap events_map(const sde::Model& model, const sde::testing_support::Signer* signer) {
  const sde::Json document = sde::parse_json(
      R"({"contract": 4, "project_id": ")" + kProject + R"(", "model_version": ")" +
      model.version() + R"(", "map_version": 7, "groups": {"Event": {"write_epoch": 1, "source": {
      "engine": "db", "id": "source", "layout": {"tables": {"Event": "events"},
      "columns": {"Event": {"id": "Int64"}}}}}}})");
  sde::LoadOptions options;
  options.model = &model;
  if (signer == nullptr) return sde::load_map(document, options);
  options.public_keys = signer->keys();
  return sde::load_map(signer->signed_document(document), options);
}

sde::Model events() {
  return sde::load_neutral_model(
      R"({"entities": [{"name": "Event", "fields": [{"name": "id", "type": "int64"}],
                        "key": ["id"]}]})");
}

const std::string kWatermark(sde::WATERMARK_TABLE);

/// The versions recorded in a database's bookkeeping, or nothing when it has no such table.
std::optional<std::vector<std::string>> recorded(const ClickHouseRoles& roles) {
  if (roles.rows("SELECT name FROM system.tables WHERE database = currentDatabase() AND name = '" +
                 kWatermark + "'")
          .empty()) {
    return std::nullopt;
  }
  std::vector<std::string> out;
  for (const sde::Json& row : roles.rows("SELECT toString(map_version) AS v FROM `" + kWatermark +
                                         "` ORDER BY map_version")) {
    out.push_back(row.find("v")->as_string());
  }
  return out;
}

TEST_F(ClickHouseSchema, ASignedRuntimeRunsWithoutTheRightToIssueDdl) {
  const ClickHouseRoles roles(dsn_);
  const sde::Model model = events();
  const sde::testing_support::Signer signer("the ClickHouse live tests");
  const sde::PlacementMap map = events_map(model, &signer);
  const auto provisioning = engine(roles.operator_dsn());
  sde::prepare_schema(model, map, {{"db", provisioning.get()}}, kProject);
  (void)provisioning->map_watermark();  // an existing installation can already have the table
  roles.grant("events");
  roles.grant(kWatermark);
  const auto runtime = engine(roles.runtime_dsn());
  sde::SessionOptions options;
  options.project_id = kProject;
  sde::Session session(model, map, {{"db", runtime.get()}}, options);
  session.save("Event", {{"id", std::int64_t{1}}});
  EXPECT_EQ(session.get("Event", {{"id", std::int64_t{1}}}), (sde::Row{{"id", std::int64_t{1}}}));
  EXPECT_EQ(session.rollback_protection().protection, "enforced");
  EXPECT_EQ(provisioning->map_watermark(), 7);

  const std::string url = sde::live::url_of(roles.runtime_dsn());
  const std::string version = runtime->server_version();
  try {
    roles.command("CREATE TABLE forbidden_table (id Int64) ENGINE = MergeTree ORDER BY id", true);
    ADD_FAILURE() << "the runtime login created a table";
  } catch (const std::exception& error) {
    EXPECT_EQ(std::string(error.what()),
              server_said(497,
                          roles.username() + ": Not enough privileges. To execute this query, "
                          "it's necessary to have the grant CREATE TABLE ON " + roles.database() +
                              ".forbidden_table. (ACCESS_DENIED)",
                          version, url));
  }
  EXPECT_EQ(refusal<sde::EngineError>(
                [&] { (void)runtime->write_fence("events", kProject).freeze(std::string(32, '6')); }),
            "write-fence DDL failed; the table may remain closed or detached; resume the same "
            "operation: " +
                server_said(497,
                            roles.username() + ": Not enough privileges. To execute this query, "
                            "it's necessary to have the grant ALTER ADD CONSTRAINT ON " +
                                roles.database() + ".events. (ACCESS_DENIED)",
                            version, url));
  EXPECT_TRUE(provisioning->write_fence("events", kProject).state().holds.empty());
}

TEST_F(ClickHouseSchema, ProvisioningPreparesTheBookkeepingWithoutAdoptingTheMap) {
  const ClickHouseRoles roles(dsn_);
  const sde::Model model = events();
  const sde::testing_support::Signer signer("the ClickHouse live tests");
  const sde::PlacementMap map = events_map(model, &signer);
  const auto provisioning = engine(roles.operator_dsn());
  sde::prepare_schema(model, map, {{"db", provisioning.get()}}, kProject);
  EXPECT_EQ(recorded(roles), std::vector<std::string>{})
      << "signed provisioning left creating the bookkeeping to the runtime, or recorded the map";
  provisioning->record_map_version(2, model.version());
  sde::prepare_schema(model, map, {{"db", provisioning.get()}}, kProject);
  EXPECT_EQ(recorded(roles), std::vector<std::string>{"2"});
}

TEST_F(ClickHouseSchema, UnreadableBookkeepingIsNotAnEmptyWatermark) {
  const ClickHouseRoles roles(dsn_);
  const sde::Model model = events();
  const sde::testing_support::Signer signer("the ClickHouse live tests");
  const sde::PlacementMap map = events_map(model, &signer);
  const auto provisioning = engine(roles.operator_dsn());
  sde::prepare_schema(model, map, {{"db", provisioning.get()}}, kProject);
  (void)provisioning->map_watermark();
  provisioning->record_map_version(9, model.version());
  roles.grant("events");
  roles.grant(kWatermark);
  roles.grant(kWatermark, true);
  const auto runtime = engine(roles.runtime_dsn());
  sde::SessionOptions options;
  options.project_id = kProject;
  EXPECT_EQ(refusal<sde::EngineError>(
                [&] { const sde::Session session(model, map, {{"db", runtime.get()}}, options); }),
            "reading sde_map_state failed: " +
                server_said(497,
                            roles.username() + ": Not enough privileges. To execute this query, "
                            "it's necessary to have the grant SELECT(map_version) ON " +
                                roles.database() + ".sde_map_state. (ACCESS_DENIED)",
                            runtime->server_version(), sde::live::url_of(roles.runtime_dsn())));
  EXPECT_EQ(recorded(roles), std::vector<std::string>{"9"});
}

TEST_F(ClickHouseSchema, AnUnsignedMapNeedsNoBookkeepingAnywhere) {
  const ClickHouseRoles roles(dsn_);
  const sde::Model model = events();
  const sde::PlacementMap map = events_map(model, nullptr);
  const auto provisioning = engine(roles.operator_dsn());
  sde::prepare_schema(model, map, {{"db", provisioning.get()}}, kProject);
  EXPECT_FALSE(recorded(roles).has_value());
  roles.grant("events");
  const auto runtime = engine(roles.runtime_dsn());
  sde::SessionOptions options;
  options.project_id = kProject;
  sde::Session session(model, map, {{"db", runtime.get()}}, options);
  session.save("Event", {{"id", std::int64_t{1}}});
  EXPECT_EQ(session.get("Event", {{"id", std::int64_t{1}}}), (sde::Row{{"id", std::int64_t{1}}}));
  EXPECT_EQ(session.rollback_protection().protection, "not_applicable");
  EXPECT_FALSE(recorded(roles).has_value());
}

TEST_F(ClickHouseSchema, AnEngineNoGroupNamesGetsTheRuntimeBookkeepingToo) {
  const ClickHouseRoles roles(dsn_);
  const ClickHouseRoles spare(dsn_);
  const sde::Model model = events();
  const sde::testing_support::Signer signer("the ClickHouse live tests");
  const sde::PlacementMap map = events_map(model, &signer);
  const auto provisioning = engine(roles.operator_dsn());
  const auto spare_provisioning = engine(spare.operator_dsn());
  sde::prepare_schema(model, map,
                      {{"db", provisioning.get()}, {"spare", spare_provisioning.get()}}, kProject);
  roles.grant("events");
  roles.grant(kWatermark);
  spare.grant(kWatermark);
  const auto runtime = engine(roles.runtime_dsn());
  const auto spare_runtime = engine(spare.runtime_dsn());
  sde::SessionOptions options;
  options.project_id = kProject;
  sde::Session session(model, map, {{"db", runtime.get()}, {"spare", spare_runtime.get()}},
                       options);
  session.save("Event", {{"id", std::int64_t{1}}});
  EXPECT_EQ(session.get("Event", {{"id", std::int64_t{1}}}), (sde::Row{{"id", std::int64_t{1}}}));
  EXPECT_EQ(session.rollback_protection().participating,
            (std::vector<std::string>{"db", "spare"}));
  EXPECT_EQ(recorded(spare), std::vector<std::string>{"7"});
}

}  // namespace
