/// Sizes from ClickHouse's catalogue, read by a least-privilege runtime login - numbers only, never
/// a row - and carried by a session's telemetry window. Measured on 24.8: a login with table grants
/// alone is refused `system.parts`, and a column grant on the parts' size columns is all it needs,
/// after which it sees only its own tables. Ported from the ClickHouse halves of the reference's
/// `test_storage_sizes_live.py` and `test_measure_storage_live.py`.
///
///     SDE_CLICKHOUSE_DSN=clickhouse://default:sde@127.0.0.1:58123/sde ctest -L live

#include <cstdint>
#include <map>
#include <memory>
#include <optional>
#include <set>
#include <string>
#include <vector>

#include <gtest/gtest.h>

#include "live/capture.hpp"
#include "live/clickhouse.hpp"
#include "live/live.hpp"
#include "sde/clickhouse.hpp"
#include "sde/errors.hpp"
#include "sde/json.hpp"
#include "sde/model.hpp"
#include "sde/placement.hpp"
#include "sde/provisioning.hpp"
#include "sde/session.hpp"
#include "sde/telemetry.hpp"
#include "support/signer.hpp"

namespace {

using sde::live::ClickHouseAdmin;
using sde::live::ClickHouseRoles;

const std::string kProject(32, '1');

/// The columns of `system.parts` a size reads, and all a runtime login is granted: the reference's
/// `STORAGE_COLUMNS`.
const std::string kStorageColumns =
    "database, table, active, bytes_on_disk, secondary_indices_compressed_bytes, "
    "secondary_indices_marks_bytes";

std::unique_ptr<sde::ClickHouseEngine> connected(const std::string& dsn) {
  auto made = std::make_unique<sde::ClickHouseEngine>(dsn);
  made->connect();
  return made;
}

/// A table of five thousand rows with a data-skipping index, and an empty one, both granted.
void tables(const ClickHouseRoles& roles) {
  roles.command("CREATE TABLE `filled` (k Int64, v Int64, INDEX filled_v v TYPE minmax "
                "GRANULARITY 1) ENGINE = MergeTree ORDER BY k");
  roles.command("INSERT INTO `filled` SELECT number, number % 97 FROM numbers(5000)");
  roles.command("CREATE TABLE `empty` (k Int64) ENGINE = MergeTree ORDER BY k");
  roles.grant("filled");
  roles.grant("empty");
}

void grant_sizes(const ClickHouseRoles& roles) {
  roles.command("GRANT SELECT(" + kStorageColumns + ") ON system.parts TO `" + roles.username() +
                "`");
}

class ClickHouseStorage : public ::testing::Test {
 protected:
  void SetUp() override { SDE_REQUIRE_DSN("SDE_CLICKHOUSE_DSN", dsn_); }
  std::string dsn_;
};

TEST_F(ClickHouseStorage, ARuntimeLoginReadsItsTablesSizesAndNothingOfAMissingOne) {
  const ClickHouseRoles roles(dsn_);
  tables(roles);
  const auto runtime = connected(roles.runtime_dsn());
  const auto provisioning = connected(roles.operator_dsn());
  // Without the column grant the read is refused, an error here; a session turns it into an
  // unknown size.
  const std::string refused =
      sde::live::refusal<sde::EngineError>([&] { (void)runtime->storage_sizes({"filled"}); });
  EXPECT_TRUE(refused.starts_with("storage sizes could not be read: Received ClickHouse "
                                  "exception, code: 497"))
      << refused;
  grant_sizes(roles);
  const auto mine = runtime->storage_sizes({"filled", "empty", "missing"});
  EXPECT_EQ(mine, provisioning->storage_sizes({"filled", "empty", "missing"}))
      << "the runtime login reads what the administrator reads";
  ASSERT_EQ(mine.size(), 2U) << "a missing table is absent, not zero";
  const auto [total, secondary] = mine.at("filled");
  EXPECT_GT(total, secondary) << "the secondary index is part of the total, and not all of it";
  EXPECT_GT(secondary, 0);
  EXPECT_EQ(mine.at("empty"), (std::pair<std::int64_t, std::int64_t>{0, 0}))
      << "an empty MergeTree has no parts";
  EXPECT_TRUE(runtime->storage_sizes({}).empty());
}

TEST_F(ClickHouseStorage, TheGrantShowsOnlyTheLoginsOwnTables) {
  // The column grant on system.parts is not a window onto other databases: another namespace's
  // tables carry the same names, and the login reads only the database it is in.
  const ClickHouseRoles roles(dsn_);
  const ClickHouseRoles other(dsn_);
  tables(roles);
  tables(other);
  grant_sizes(roles);
  const auto runtime = connected(roles.runtime_dsn());
  EXPECT_EQ(runtime->storage_sizes({"filled", "empty"}).size(), 2U);
  std::set<std::string> databases;
  for (const sde::Json& row :
       ClickHouseAdmin(roles.runtime_dsn()).rows("SELECT DISTINCT database FROM system.parts WHERE active")) {
    databases.insert(row.find("database")->as_string());
  }
  EXPECT_EQ(databases, std::set<std::string>{roles.database()});
}

TEST_F(ClickHouseStorage, ARuntimeSessionMeasuresItsGroupAndTheWindowCarriesIt) {
  // Without the grant the size is unknown, "refused", and the application sees no exception; with
  // it the size is what the administrator reads, and the window carries it.
  const ClickHouseRoles roles(dsn_);
  const sde::Model model = sde::load_neutral_model(
      R"({"entities": [{"name": "Event", "fields": [{"name": "id", "type": "int64"}],
                        "key": ["id"]}]})");
  const sde::testing_support::Signer signer("the ClickHouse storage tests");
  sde::LoadOptions options;
  options.model = &model;
  options.public_keys = signer.keys();
  const sde::PlacementMap map = sde::load_map(
      signer.signed_document(sde::parse_json(
          R"({"contract": 4, "project_id": ")" + kProject + R"(", "model_version": ")" +
          model.version() + R"(", "map_version": 7, "groups": {"Event": {"write_epoch": 1,
          "source": {"engine": "db", "id": "source", "layout": {"tables": {"Event": "events"},
          "columns": {"Event": {"id": "Int64"}}}}}}})")),
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
  const sde::StorageMeasurement refused = session.measure_storage();
  EXPECT_TRUE(refused.sizes.empty());
  EXPECT_EQ(refused.unavailable, (std::map<std::string, std::string>{{"Event", "refused"}}));
  grant_sizes(roles);
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

}  // namespace
