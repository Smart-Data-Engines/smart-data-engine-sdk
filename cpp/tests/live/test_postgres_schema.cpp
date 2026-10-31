/// Schemas against a real PostgreSQL: what `prepare_schema` creates, what a table of another design
/// costs where it is provisioned and in a running session, what the catalogue check refuses and what
/// it only names, the runtime login that holds no DDL right, and every `schema/` vector through this
/// adapter. Ported from the reference's `test_physical_live.py`, `test_runtime_privileges_live.py`
/// and `test_schema_vectors_live.py`. Every expected text is the reference's, captured from it
/// against the same server; the catalogue is read back with SQL written here, not with the adapter.
///
///     SDE_POSTGRES_DSN=postgresql://postgres:sde@127.0.0.1:55432/sde ctest -L live

#include <algorithm>
#include <functional>
#include <map>
#include <memory>
#include <optional>
#include <set>
#include <string>
#include <utility>
#include <vector>

#include <gtest/gtest.h>

#include "engines/postgres/connection.hpp"
#include "live/capture.hpp"
#include "live/live.hpp"
#include "live/postgres.hpp"
#include "sde/canonical.hpp"
#include "sde/errors.hpp"
#include "sde/json.hpp"
#include "sde/model.hpp"
#include "sde/placement.hpp"
#include "sde/postgres.hpp"
#include "sde/provisioning.hpp"
#include "sde/session.hpp"
#include "sde/write_fence.hpp"
#include "support/signer.hpp"
#include "support/vectors.hpp"

namespace {

using sde::live::Admin;
using sde::live::Captured;
using sde::live::refusal;
using sde::live::Roles;
using sde::live::Scope;

const std::string kProject = "11111111111111111111111111111111";

const std::string kRemedy =
    "a staging into a fresh copy, whose text columns are created COLLATE \"C\"; or, by an "
    "administrator, ALTER COLUMN ... TYPE text COLLATE \"C\", which rebuilds the column's indexes "
    "under an exclusive lock on the table";

const std::string kTypeTail =
    ". `CREATE TABLE IF NOT EXISTS` keeps a table of that name whatever shape it is in, and this "
    "library never alters a column's type - so the table came from somewhere else, or from a map "
    "that rendered this column differently. Refusing rather than writing into it: a type that "
    "differs is either a write that fails in your request path or, worse, one that succeeds and "
    "hands the value back as something else.";

sde::Model readings() {
  return sde::load_neutral_model(R"({"entities": [{"name": "Reading", "fields": [
      {"name": "at", "type": "timestamptz"}, {"name": "humidity", "type": "int32"},
      {"name": "station", "type": "string"}, {"name": "temperature", "type": "float64"}],
      "key": ["station", "at"]}]})");
}

const sde::Keys kReadingKeys{{"Reading", {"station", "at"}}};

/// The reference's map: `readings` in engine `db`, generation 1, and with `design` its physical
/// design - the key ordered by time, a BRIN on the time and a B-tree on the temperature.
sde::PlacementMap readings_map(const sde::Model& model, bool design, int version = 1,
                               int contract = 5) {
  std::string layout = R"({"tables": {"Reading": "readings"}, "columns": {"Reading": {
      "at": "timestamptz", "humidity": "integer", "station": "text",
      "temperature": "double precision"}}})";
  if (design) {
    layout.pop_back();
    layout += R"(, "key_order": {"Reading": ["at", "station"]}, "indexes": [
        {"entity": "Reading", "name": "reading_at_brin", "columns": ["at"], "method": "brin"},
        {"entity": "Reading", "name": "reading_temperature", "columns": ["temperature"]}]})";
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

/// `sde.schema.text_collation` as the reference writes it for one index.
std::string collation_event(const std::string& table, const std::string& index,
                            const std::vector<std::string>& columns) {
  sde::Json fields = sde::Json::object();
  fields.set("table", table);
  fields.set("index", index);
  sde::Json named = sde::Json::array();
  for (const std::string& column : columns) named.as_array().push_back(column);
  fields.set("columns", std::move(named));
  fields.set("remedy", kRemedy);
  return sde::canonical_bytes(fields);
}

class PostgresSchema : public ::testing::Test {
 protected:
  void SetUp() override { SDE_REQUIRE_DSN("SDE_POSTGRES_DSN", dsn_); }

  /// An engine on `dsn`, connected, with its events in `log` when one is given.
  static std::unique_ptr<sde::PostgresEngine> engine(const std::string& dsn,
                                                     Captured* log = nullptr) {
    sde::PostgresOptions options;
    if (log != nullptr) options.log = log->sink();
    auto made = std::make_unique<sde::PostgresEngine>(dsn, std::move(options));
    made->connect();
    return made;
  }

  std::string dsn_;
};

// --- a designed layout --------------------------------------------------------------------------

TEST_F(PostgresSchema, ADesignedLayoutIsCreatedAsDeclared) {
  const Roles roles(dsn_);
  const sde::Model model = readings();
  const sde::PlacementMap map = readings_map(model, true);
  const auto provisioning = engine(roles.operator_dsn());
  sde::prepare_schema(model, map, {{"db", provisioning.get()}}, kProject);

  Admin catalogue(roles.operator_dsn());
  std::vector<std::string> key;
  for (const auto& row : catalogue.rows(
           "SELECT a.attname FROM pg_index i JOIN pg_class t ON t.oid = i.indrelid "
           "JOIN pg_namespace n ON n.oid = t.relnamespace "
           "JOIN LATERAL unnest(i.indkey) WITH ORDINALITY k(num, pos) ON true "
           "JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.num "
           "WHERE i.indisprimary AND n.nspname = current_schema() AND t.relname = 'readings' "
           "ORDER BY k.pos")) {
    key.push_back(*row[0]);
  }
  EXPECT_EQ(key, (std::vector<std::string>{"at", "station"}));
  std::map<std::string, std::string> indexes;
  for (const auto& row : catalogue.rows("SELECT indexname, indexdef FROM pg_indexes WHERE "
                                        "schemaname = current_schema() AND tablename = 'readings'")) {
    indexes[*row[0]] = *row[1];
  }
  EXPECT_NE(indexes["reading_at_brin"].find("USING brin (at)"), std::string::npos)
      << indexes["reading_at_brin"];
  EXPECT_NE(indexes["reading_temperature"].find("USING btree (temperature)"), std::string::npos)
      << indexes["reading_temperature"];
  // Text in the reads' collation, so the primary key can serve a read by the station.
  EXPECT_EQ(catalogue.rows("SELECT collation_name FROM information_schema.columns WHERE "
                           "table_schema = current_schema() AND table_name = 'readings' AND "
                           "column_name = 'station'"),
            (Admin::Rows{{std::string("C")}}));

  const sde::PhysicalLayout& layout = map.placement_of("Reading").source.layout;
  EXPECT_TRUE(provisioning->validate_schema(layout, kReadingKeys).empty());
}

TEST_F(PostgresSchema, AnotherDesignIsRefusedAtProvisioningAndReportedByARunningSession) {
  Roles roles(dsn_);
  const sde::Model model = readings();
  const sde::PlacementMap first = readings_map(model, false);
  const auto provisioning = engine(roles.operator_dsn());
  sde::prepare_schema(model, first, {{"db", provisioning.get()}}, kProject);
  const sde::PlacementMap second = readings_map(model, true, 2);

  // A person provisioning the new map over the old tables is told, table and aspect named.
  EXPECT_EQ(
      refusal<sde::EngineError>(
          [&] { sde::prepare_schema(model, second, {{"db", provisioning.get()}}, kProject); }),
      "existing tables differ from the physical design this map declares: readings: primary key "
      "is ['station', 'at'] and the map declares ['at', 'station']; readings: index "
      "reading_at_brin is absent and the map declares brin on ['at']; readings: index "
      "reading_temperature is absent and the map declares btree on ['temperature']. `CREATE ... "
      "IF NOT EXISTS` keeps whatever table or index already has the name, so this came from an "
      "earlier map or from outside SDE. A new layout needs fresh tables (staging), not the old "
      "ones under the new declaration.");

  // An application started on the same new map keeps serving rows and says what it found.
  roles.grant("readings");
  Captured log;
  const auto runtime = engine(roles.runtime_dsn(), &log);
  sde::SessionOptions options;
  options.log = log.sink();
  options.project_id = kProject;
  sde::Session session(model, second, {{"db", runtime.get()}}, options);
  EXPECT_EQ(session.physical(),
            (std::vector<sde::PhysicalFinding>{
                {"readings", "primary key", "['at', 'station']", "['station', 'at']"},
                {"readings", "index reading_at_brin", "brin on ['at']", "absent"},
                {"readings", "index reading_temperature", "btree on ['temperature']", "absent"}}));
  EXPECT_EQ(log.of("sde.schema.physical_mismatch"),
            (std::vector<std::string>{R"({"findings":3,"tables":["readings"]})"}));
  const sde::Row row{{"station", std::string("s1")},
                     {"at", *sde::TimestampTz::parse("2026-09-23T12:00:00.123456Z")},
                     {"humidity", std::int64_t{40}},
                     {"temperature", 21.5}};
  session.save("Reading", row);
  EXPECT_EQ(session.get("Reading", {{"station", row.at("station")}, {"at", row.at("at")}}), row);
}

TEST_F(PostgresSchema, AMatchingTableReportsNothingToARestrictedSession) {
  Roles roles(dsn_);
  const sde::Model model = readings();
  const sde::PlacementMap map = readings_map(model, true);
  const auto provisioning = engine(roles.operator_dsn());
  sde::prepare_schema(model, map, {{"db", provisioning.get()}}, kProject);
  roles.grant("readings");
  Captured log;
  const auto runtime = engine(roles.runtime_dsn(), &log);
  sde::SessionOptions options;
  options.log = log.sink();
  options.project_id = kProject;
  sde::Session session(model, map, {{"db", runtime.get()}}, options);
  session.ensure_schema();
  EXPECT_TRUE(session.physical().empty());
  // A table this library created is in the reads' collation: nothing to name, on opening or after.
  EXPECT_TRUE(log.of("sde.schema.text_collation").empty());
  EXPECT_TRUE(log.of("sde.schema.physical_mismatch").empty());
}

TEST_F(PostgresSchema, AUniqueOrUnfinishedIndexIsNotTheDeclaredOne) {
  // A failed CREATE INDEX CONCURRENTLY leaves its index in the catalogue, neither valid nor ready;
  // a unique index of the declared shape refuses writes the layout never refuses.
  const Roles roles(dsn_);
  const sde::Model model = readings();
  const sde::PlacementMap map = readings_map(model, true);
  const auto provisioning = engine(roles.operator_dsn());
  sde::prepare_schema(model, map, {{"db", provisioning.get()}}, kProject);
  const sde::PhysicalLayout& layout = map.placement_of("Reading").source.layout;
  Admin admin(roles.operator_dsn());
  const auto found = [&] {
    std::map<std::string, std::string> out;
    for (const sde::PhysicalFinding& finding : provisioning->validate_schema(layout, kReadingKeys)) {
      out[finding.aspect] = finding.found;
    }
    return out;
  };
  using Found = std::map<std::string, std::string>;
  EXPECT_EQ(found(), Found{});
  (void)admin.run("DROP INDEX reading_temperature");
  (void)admin.run("CREATE UNIQUE INDEX reading_temperature ON readings (temperature)");
  EXPECT_EQ(found(), (Found{{"index reading_temperature", "btree on ['temperature'], unique"}}));

  (void)admin.run("DROP INDEX reading_temperature");
  (void)admin.run(
      "INSERT INTO readings (station, at, humidity, temperature, __sde_write_epoch) "
      "VALUES ('a', now(), 1, 20.0, 1), ('b', now(), 1, 20.0, 1)");
  try {
    (void)admin.run("CREATE UNIQUE INDEX CONCURRENTLY reading_temperature ON readings (temperature)");
    ADD_FAILURE() << "the unique build over two equal temperatures succeeded";
  } catch (const sde::detail::postgres::ServerError& error) {
    EXPECT_EQ(error.class_name(), "UniqueViolation") << error.what();
  }
  EXPECT_EQ(found(), (Found{{"index reading_temperature",
                             "btree on ['temperature'], unique, not valid (an unfinished "
                             "concurrent build)"}}));

  // Not unique, only not valid: the catalogue flag a cancelled build leaves, set directly.
  (void)admin.run("DROP INDEX CONCURRENTLY reading_temperature");
  (void)admin.run("CREATE INDEX reading_temperature ON readings (temperature)");
  (void)admin.run("UPDATE pg_index SET indisvalid = false "
                  "WHERE indexrelid = 'reading_temperature'::regclass");
  EXPECT_EQ(found(), (Found{{"index reading_temperature",
                             "btree on ['temperature'], not valid (an unfinished concurrent "
                             "build)"}}));
}

// --- the catalogue check ------------------------------------------------------------------------

TEST_F(PostgresSchema, ATableMissingAColumnIsRefusedNamingBoth) {
  const Scope scope(dsn_);
  Admin(scope.dsn()).run("CREATE TABLE readings (station text, at timestamptz)");
  const sde::Model model = readings();
  const sde::PlacementMap map = readings_map(model, false, 1, 3);
  const auto db = engine(scope.dsn());
  EXPECT_EQ(refusal<sde::EngineError>([&] {
              (void)db->ensure_schema(map.placement_of("Reading").source.layout, kReadingKeys);
            }),
            "'readings' already existed with a different shape: the map needs ['humidity', "
            "'temperature'] and the table has ['at', 'station']. `CREATE TABLE IF NOT EXISTS` "
            "keeps whatever is there, so this table came from somewhere else - an older map, "
            "another application, a migration run by hand. Refusing here rather than at the first "
            "insert, which would fail in your request path with an error naming a column and not "
            "the cause.");
}

TEST_F(PostgresSchema, AColumnOfAnotherTypeIsRefused) {
  // Names alone passed this: a table whose `at` is text, where the map says timestamptz.
  const Scope scope(dsn_);
  Admin(scope.dsn()).run("CREATE TABLE readings (station text, at text, humidity integer, "
                         "temperature double precision)");
  const sde::Model model = readings();
  const sde::PlacementMap map = readings_map(model, false, 1, 3);
  const auto db = engine(scope.dsn());
  EXPECT_EQ(refusal<sde::EngineError>([&] {
              (void)db->ensure_schema(map.placement_of("Reading").source.layout, kReadingKeys);
            }),
            "readings.at is 'text' and this map declares it 'timestamptz'" + kTypeTail);
}

TEST_F(PostgresSchema, ATypesModifierIsPartOfTheType) {
  const Scope scope(dsn_);
  Admin(scope.dsn()).run("CREATE TABLE prices (id bigint PRIMARY KEY, amount numeric(8,2))");
  const sde::Model model = sde::load_neutral_model(R"json({"entities": [{"name": "Price", "fields": [
      {"name": "id", "type": "int64"}, {"name": "amount", "type": "decimal(12,2)"}],
      "key": ["id"]}]})json");
  sde::LoadOptions options;
  options.model = &model;
  const sde::PlacementMap map = sde::load_map(
      R"({"contract": 3, "model_version": ")" + model.version() + R"json(", "map_version": 1,
          "groups": {"Price": {"source": {"id": "p", "engine": "db", "layout": {
          "tables": {"Price": "prices"},
          "columns": {"Price": {"id": "bigint", "amount": "numeric(12,2)"}}}}}}})json",
      options);
  const auto db = engine(scope.dsn());
  EXPECT_EQ(refusal<sde::EngineError>([&] {
              (void)db->ensure_schema(map.placement_of("Price").source.layout,
                                      {{"Price", {"id"}}});
            }),
            "prices.amount is 'numeric(8,2)' and this map declares it 'numeric(12,2)'" + kTypeTail);
}

TEST_F(PostgresSchema, AnAliasIsTheSameTypeAndAnExtraColumnIsNamedNotRefused) {
  // Created by hand in other spellings, with two columns the map does not name, the generation
  // column provisioning adds, and the default collation: accepted, and both named - the extra
  // columns and the key no read can use. The generation column is ours and is not one of them.
  const Scope scope(dsn_);
  Admin(scope.dsn()).run("CREATE TABLE readings (station text, at timestamp with time zone, "
                         "humidity int4, temperature float8, zeta text, alpha int, "
                         "__sde_write_epoch bigint, PRIMARY KEY (station, at))");
  const sde::Model model = readings();
  const sde::PlacementMap map = readings_map(model, false, 1, 3);
  Captured log;
  const auto db = engine(scope.dsn(), &log);
  EXPECT_TRUE(db->validate_schema(map.placement_of("Reading").source.layout, kReadingKeys).empty());
  EXPECT_EQ(log.names(),
            (std::vector<std::string>{"sde.schema.extra_columns", "sde.schema.text_collation"}));
  EXPECT_EQ(log.of("sde.schema.extra_columns"),
            (std::vector<std::string>{R"({"columns":["alpha","zeta"],"table":"readings"})"}));
  EXPECT_EQ(log.of("sde.schema.text_collation"),
            (std::vector<std::string>{collation_event("readings", "readings_pkey", {"station"})}));
}

TEST_F(PostgresSchema, ATextKeyFromBeforeTheReadsCollationIsNamedWithItsRemedy) {
  // The DDL of SDK 0.1.x, before text columns had a collation of their own. Named once for each
  // read of the catalogue - ensure_schema reads it twice - and never a finding.
  const Scope scope(dsn_);
  Admin(scope.dsn()).run(
      "CREATE TABLE \"readings\" (\"at\" timestamptz, \"humidity\" integer, \"station\" text, "
      "\"temperature\" double precision, PRIMARY KEY (\"station\", \"at\"))");
  const sde::Model model = readings();
  const sde::PlacementMap map = readings_map(model, false, 1, 3);
  Captured log;
  const auto db = engine(scope.dsn(), &log);
  sde::SessionOptions options;
  options.log = log.sink();
  sde::Session session(model, map, {{"db", db.get()}}, options);
  session.ensure_schema();
  EXPECT_TRUE(session.physical().empty());
  const std::string named = collation_event("readings", "readings_pkey", {"station"});
  EXPECT_EQ(log.names(), (std::vector<std::string>{"sde.schema.text_collation",
                                                   "sde.schema.applied",
                                                   "sde.schema.text_collation",
                                                   "sde.schema.applied"}));
  EXPECT_EQ(log.of("sde.schema.text_collation"), (std::vector<std::string>{named, named}));
  EXPECT_EQ(log.of("sde.schema.applied"),
            (std::vector<std::string>{R"({"engine":"postgres","statements":1})",
                                      R"({"groups":1})"}));
}

// --- the schema vectors -------------------------------------------------------------------------

TEST_F(PostgresSchema, EverySchemaVectorIsCreatedByThisAdapterAndVerifiesClean) {
  // The vectors pin the statements, and the reference runs them against a server. What is left to
  // show is this adapter's path - libpq, the catalogue reads, the quoting of names like schema/011
  // - so each case is created through `ensure_schema`, twice as a restart does, in a schema of its
  // own, and its names are read back from the catalogue. The reference's adapter, run the same
  // way, reports no difference for any of them.
  using namespace sde::testing_support;
  std::size_t ran = 0;
  std::size_t not_runnable = 0;
  for (const std::string& name : cases("schema")) {
    const auto directory = vectors_root() / "schema" / name;
    const sde::Model model = sde::load_neutral_model(read_json(directory / "model.json"));
    sde::LoadOptions options;
    options.model = &model;
    const sde::PlacementMap map = sde::load_map(read_json(directory / "map.json"), options);
    const sde::Json expectations = read_json(directory / "cases.json");
    for (const sde::Json& expectation : expectations.as_array()) {
      if (const sde::Json* runs = expectation.find("runs"); runs != nullptr && !runs->as_bool()) {
        ++not_runnable;
        continue;
      }
      if (expectation.find("dialect")->as_string() != "postgres" ||
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
      const Scope scope(dsn_);
      const auto db = engine(scope.dsn());
      EXPECT_EQ(db->ensure_schema(layout, keys), std::vector<sde::PhysicalFinding>{});
      EXPECT_EQ(db->ensure_schema(layout, keys), std::vector<sde::PhysicalFinding>{});
      std::map<std::string, std::set<std::string>> found;
      for (const auto& row : Admin(scope.dsn()).rows(
               "SELECT table_name, column_name FROM information_schema.columns "
               "WHERE table_schema = current_schema()")) {
        found[*row[0]].insert(*row[1]);
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
  // Guards the guard: a test that runs nothing passes for the wrong reason, and one case of the
  // family - a ClickHouse layout rendered as postgres - says it cannot run. A second would be
  // coverage leaving this test quietly.
  EXPECT_GT(ran, 0U);
  EXPECT_EQ(not_runnable, 1U);
}

// --- a runtime login without DDL ----------------------------------------------------------------

/// The reference's signed contract-4 map: one entity, `events`, generation 1, map version 7.
sde::PlacementMap events_map(const sde::Model& model, const sde::testing_support::Signer* signer) {
  const sde::Json document = sde::parse_json(
      R"({"contract": 4, "project_id": ")" + kProject + R"(", "model_version": ")" +
      model.version() + R"(", "map_version": 7, "groups": {"Event": {"write_epoch": 1, "source": {
      "engine": "db", "id": "source", "layout": {"tables": {"Event": "events"},
      "columns": {"Event": {"id": "bigint"}}}}}}})");
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

/// The versions recorded in a schema's bookkeeping, or nothing when it has no such table.
std::optional<std::vector<std::string>> recorded(const std::string& dsn) {
  Admin admin(dsn);
  if (admin.rows("SELECT to_regclass($1)::text", {kWatermark})[0][0] == std::nullopt) {
    return std::nullopt;
  }
  std::vector<std::string> out;
  for (const auto& row : admin.rows("SELECT map_version::text FROM \"" + kWatermark +
                                    "\" ORDER BY map_version")) {
    out.push_back(*row[0]);
  }
  return out;
}

TEST_F(PostgresSchema, ASignedRuntimeRunsWithoutTheRightToIssueDdl) {
  Roles roles(dsn_);
  const sde::Model model = events();
  const sde::testing_support::Signer signer("the PostgreSQL live tests");
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

  Admin as_runtime(roles.runtime_dsn());
  try {
    (void)as_runtime.run("CREATE TABLE forbidden_table (id bigint)");
    ADD_FAILURE() << "the runtime login created a table";
  } catch (const sde::detail::postgres::ServerError& error) {
    EXPECT_TRUE(std::string(error.what()).starts_with("permission denied for schema " +
                                                      roles.schema()))
        << error.what();
  }
  EXPECT_EQ(refusal<sde::EngineError>(
                [&] { (void)runtime->write_fence("events", kProject).freeze(std::string(32, '6')); }),
            "write-fence operation failed; inspect or resume its state: must be owner of table "
            "events");
  EXPECT_TRUE(provisioning->write_fence("events", kProject).state().holds.empty());
}

TEST_F(PostgresSchema, ProvisioningPreparesTheBookkeepingWithoutAdoptingTheMap) {
  const Roles roles(dsn_);
  const sde::Model model = events();
  const sde::testing_support::Signer signer("the PostgreSQL live tests");
  const sde::PlacementMap map = events_map(model, &signer);
  const auto provisioning = engine(roles.operator_dsn());
  sde::prepare_schema(model, map, {{"db", provisioning.get()}}, kProject);
  EXPECT_EQ(recorded(roles.operator_dsn()), std::vector<std::string>{})
      << "signed provisioning left creating the bookkeeping to the runtime, or recorded the map";
  provisioning->record_map_version(2, model.version());
  sde::prepare_schema(model, map, {{"db", provisioning.get()}}, kProject);
  EXPECT_EQ(recorded(roles.operator_dsn()), std::vector<std::string>{"2"});
}

TEST_F(PostgresSchema, UnreadableBookkeepingIsNotAnEmptyWatermark) {
  Roles roles(dsn_);
  const sde::Model model = events();
  const sde::testing_support::Signer signer("the PostgreSQL live tests");
  const sde::PlacementMap map = events_map(model, &signer);
  const auto provisioning = engine(roles.operator_dsn());
  sde::prepare_schema(model, map, {{"db", provisioning.get()}}, kProject);
  provisioning->record_map_version(9, model.version());
  roles.grant("events");
  roles.grant(kWatermark);
  roles.grant(kWatermark, true);
  const auto runtime = engine(roles.runtime_dsn());
  sde::SessionOptions options;
  options.project_id = kProject;
  EXPECT_EQ(refusal<sde::EngineError>(
                [&] { sde::Session session(model, map, {{"db", runtime.get()}}, options); }),
            "reading sde_map_state failed: permission denied for table sde_map_state");
  EXPECT_EQ(recorded(roles.operator_dsn()), std::vector<std::string>{"9"});
}

TEST_F(PostgresSchema, AnUnsignedMapNeedsNoBookkeepingAnywhere) {
  Roles roles(dsn_);
  const sde::Model model = events();
  const sde::PlacementMap map = events_map(model, nullptr);
  const auto provisioning = engine(roles.operator_dsn());
  sde::prepare_schema(model, map, {{"db", provisioning.get()}}, kProject);
  EXPECT_FALSE(recorded(roles.operator_dsn()).has_value());
  roles.grant("events");
  const auto runtime = engine(roles.runtime_dsn());
  sde::SessionOptions options;
  options.project_id = kProject;
  sde::Session session(model, map, {{"db", runtime.get()}}, options);
  session.save("Event", {{"id", std::int64_t{1}}});
  EXPECT_EQ(session.get("Event", {{"id", std::int64_t{1}}}), (sde::Row{{"id", std::int64_t{1}}}));
  EXPECT_EQ(session.rollback_protection().protection, "not_applicable");
  EXPECT_FALSE(recorded(roles.operator_dsn()).has_value());
}

TEST_F(PostgresSchema, AnEngineNoGroupNamesGetsTheRuntimeBookkeepingToo) {
  Roles roles(dsn_);
  Roles spare(dsn_);
  const sde::Model model = events();
  const sde::testing_support::Signer signer("the PostgreSQL live tests");
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
  EXPECT_EQ(recorded(spare.operator_dsn()), std::vector<std::string>{"7"});
}

}  // namespace
