/// Sizes from PostgreSQL's catalogue, read by a least-privilege runtime login - numbers only, never
/// a row - and carried by a session's telemetry window. Ported from the reference's
/// `test_storage_sizes_live.py` and `test_measure_storage_live.py`, the PostgreSQL half; the
/// reference's closed-session case has no C++ counterpart, a session here being closed by its end.
///
///     SDE_POSTGRES_DSN=postgresql://postgres:sde@127.0.0.1:55432/sde ctest -L live

#include <map>
#include <memory>
#include <optional>
#include <string>
#include <utility>
#include <vector>

#include <gtest/gtest.h>

#include "live/live.hpp"
#include "live/postgres.hpp"
#include "sde/errors.hpp"
#include "sde/json.hpp"
#include "sde/model.hpp"
#include "sde/placement.hpp"
#include "sde/postgres.hpp"
#include "sde/provisioning.hpp"
#include "sde/session.hpp"
#include "sde/telemetry.hpp"
#include "support/signer.hpp"

namespace {

using sde::live::Admin;
using sde::live::Roles;

const std::string kProject = "11111111111111111111111111111111";

std::unique_ptr<sde::PostgresEngine> connected(const std::string& dsn) {
  auto made = std::make_unique<sde::PostgresEngine>(dsn);
  made->connect();
  return made;
}

class PostgresStorage : public ::testing::Test {
 protected:
  void SetUp() override { SDE_REQUIRE_DSN("SDE_POSTGRES_DSN", dsn_); }
  std::string dsn_;
};

TEST_F(PostgresStorage, ARuntimeLoginReadsItsTablesSizesAndNothingOfAMissingOne) {
  Roles roles(dsn_);
  Admin admin(roles.operator_dsn());
  (void)admin.run("CREATE TABLE \"filled\" (k bigint PRIMARY KEY, v bigint)");
  (void)admin.run("CREATE INDEX \"filled_v\" ON \"filled\" (v)");
  (void)admin.run("INSERT INTO \"filled\" SELECT i, i % 97 FROM generate_series(1, 5000) i");
  (void)admin.run("CREATE TABLE \"empty\" (k bigint PRIMARY KEY)");
  roles.grant("filled");
  roles.grant("empty");
  const auto runtime = connected(roles.runtime_dsn());
  const auto provisioning = connected(roles.operator_dsn());
  const auto mine = runtime->storage_sizes({"filled", "empty", "missing"});
  EXPECT_EQ(mine, provisioning->storage_sizes({"filled", "empty", "missing"}))
      << "the runtime login reads what the administrator reads";
  ASSERT_EQ(mine.size(), 2U) << "a missing table is absent, not zero";
  const auto [total, secondary] = mine.at("filled");
  EXPECT_GT(total, secondary) << "the secondary index is part of the total, and not all of it";
  EXPECT_GT(secondary, 0);
  EXPECT_EQ(mine.at("empty").second, 0);
  EXPECT_TRUE(runtime->storage_sizes({}).empty());
}

TEST_F(PostgresStorage, ARuntimeSessionMeasuresItsGroupAndTheWindowCarriesIt) {
  Roles roles(dsn_);
  const sde::Model model = sde::load_neutral_model(
      R"({"entities": [{"name": "Event", "fields": [{"name": "id", "type": "int64"}],
                        "key": ["id"]}]})");
  const sde::testing_support::Signer signer("the PostgreSQL storage tests");
  sde::LoadOptions options;
  options.model = &model;
  options.public_keys = signer.keys();
  const sde::PlacementMap map = sde::load_map(
      signer.signed_document(sde::parse_json(
          R"({"contract": 4, "project_id": ")" + kProject + R"(", "model_version": ")" +
          model.version() + R"(", "map_version": 7, "groups": {"Event": {"write_epoch": 1,
          "source": {"engine": "db", "id": "source", "layout": {"tables": {"Event": "events"},
          "columns": {"Event": {"id": "bigint"}}}}}}})")),
      options);
  const auto provisioning = connected(roles.operator_dsn());
  sde::prepare_schema(model, map, {{"db", provisioning.get()}}, kProject);
  roles.grant("events");
  roles.grant(std::string(sde::WATERMARK_TABLE));
  const auto runtime = connected(roles.runtime_dsn());
  sde::Recorder recorder(model);
  sde::SessionOptions session_options;
  session_options.recorder = &recorder;
  session_options.project_id = kProject;
  sde::Session session(model, map, {{"db", runtime.get()}}, session_options);
  for (const std::int64_t start : {0, 1000}) {
    std::vector<sde::Row> rows;
    for (std::int64_t index = 0; index < 1000; ++index) rows.push_back({{"id", start + index}});
    session.save_many("Event", rows);
  }
  const sde::StorageMeasurement measured = session.measure_storage();
  EXPECT_TRUE(measured.unavailable.empty());
  ASSERT_EQ(measured.sizes.size(), 1U);
  const sde::StorageSize& size = measured.sizes[0];
  EXPECT_EQ(size.group, "Event");
  EXPECT_EQ(size.engine, "db");
  EXPECT_EQ(size.materialization, "source");
  const auto [total, secondary] = provisioning->storage_sizes({"events"}).at("events");
  EXPECT_EQ(size.total_bytes, total);
  EXPECT_EQ(size.secondary_index_bytes, secondary);
  EXPECT_GT(size.total_bytes, 0);
  const std::optional<sde::Window> window = recorder.roll();
  ASSERT_TRUE(window.has_value());
  const sde::Json record = window->as_record(model);
  const sde::Json& body = *record.find("groups")->find("Event");
  const sde::Json* carried = body.find("total_bytes");
  ASSERT_NE(carried, nullptr) << "the window does not carry the size";
  EXPECT_EQ(sde::dump_json(*carried), std::to_string(size.total_bytes));
  for (const sde::Json& missing : body.find("missing")->as_array()) {
    EXPECT_NE(missing.as_string(), "total_bytes");
  }
  const sde::Json* burstiness = body.find("write_burstiness");
  ASSERT_NE(burstiness, nullptr);
  EXPECT_GE(std::stod(sde::dump_json(*burstiness)), 1.0);
}

TEST_F(PostgresStorage, AGroupWhoseTableIsGoneIsUnknownAndSaysWhy) {
  Roles roles(dsn_);
  const sde::Model model = sde::load_neutral_model(
      R"({"entities": [{"name": "Event", "fields": [{"name": "id", "type": "int64"}],
                        "key": ["id"]}]})");
  sde::LoadOptions options;
  options.model = &model;
  const sde::PlacementMap map = sde::load_map(
      R"({"contract": 3, "model_version": ")" + model.version() + R"(", "map_version": 1,
          "groups": {"Event": {"source": {"engine": "db", "id": "source", "layout": {
          "tables": {"Event": "events"}, "columns": {"Event": {"id": "bigint"}}}}}}})",
      options);
  const auto provisioning = connected(roles.operator_dsn());
  (void)provisioning->ensure_schema(map.placement_of("Event").source.layout, {{"Event", {"id"}}});
  roles.grant("events");
  const auto runtime = connected(roles.runtime_dsn());
  sde::Session session(model, map, {{"db", runtime.get()}});
  Admin(roles.operator_dsn()).run("DROP TABLE events");
  const sde::StorageMeasurement measured = session.measure_storage();
  EXPECT_TRUE(measured.sizes.empty());
  EXPECT_EQ(measured.unavailable, (std::map<std::string, std::string>{{"Event", "missing_table"}}));
}

TEST_F(PostgresStorage, ASizeTheCatalogueRefusesIsRefusedNotFailed) {
  // A login the catalogue refuses - SQLSTATE 42501 - is told apart from an engine that failed, as
  // the reference tells them apart from its driver's error. The refusal is made by taking the size
  // function from PUBLIC for the length of the test, which every login but a superuser feels.
  Roles roles(dsn_);
  Admin admin(dsn_);
  struct Restore {
    Admin& admin;
    ~Restore() {
      try {
        (void)admin.run("GRANT EXECUTE ON FUNCTION pg_total_relation_size(regclass) TO PUBLIC");
      } catch (...) {
        ADD_FAILURE() << "pg_total_relation_size could not be given back to PUBLIC";
      }
    }
  };
  const sde::Model model = sde::load_neutral_model(
      R"({"entities": [{"name": "Event", "fields": [{"name": "id", "type": "int64"}],
                        "key": ["id"]}]})");
  sde::LoadOptions options;
  options.model = &model;
  const sde::PlacementMap map = sde::load_map(
      R"({"contract": 3, "model_version": ")" + model.version() + R"(", "map_version": 1,
          "groups": {"Event": {"source": {"engine": "db", "id": "source", "layout": {
          "tables": {"Event": "events"}, "columns": {"Event": {"id": "bigint"}}}}}}})",
      options);
  const auto provisioning = connected(roles.operator_dsn());
  (void)provisioning->ensure_schema(map.placement_of("Event").source.layout, {{"Event", {"id"}}});
  roles.grant("events");
  const auto runtime = connected(roles.runtime_dsn());
  sde::Session session(model, map, {{"db", runtime.get()}});
  const Restore restore{admin};
  (void)admin.run("REVOKE EXECUTE ON FUNCTION pg_total_relation_size(regclass) FROM PUBLIC");
  const sde::StorageMeasurement measured = session.measure_storage();
  EXPECT_TRUE(measured.sizes.empty());
  EXPECT_EQ(measured.unavailable, (std::map<std::string, std::string>{{"Event", "refused"}}));
  try {
    (void)runtime->storage_sizes({"events"});
    ADD_FAILURE() << "the size was read";
  } catch (const sde::EngineError& error) {
    EXPECT_EQ(std::string(error.what()),
              "storage sizes could not be read: permission denied for function "
              "pg_total_relation_size");
  }
}

}  // namespace
