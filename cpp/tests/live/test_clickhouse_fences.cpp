/// Write barriers against a real ClickHouse, where ClickHouse's own mechanism is the claim: a
/// barrier is a constraint plus a drain - the table detached and attached again, so an insert that
/// began under the old metadata ends before it closes - and the drain is a durable intent first,
/// bound to the table's exact Atomic UUID, so an interrupted drain resumes the table it detached and
/// no other. Ported from the ClickHouse-only cases of the reference's `test_write_fence_live.py` and
/// `test_bulk_live.py`, with a proxy where the reference patches its driver; and what a fence
/// refuses, which the reference does not test, checked against it on the same server.
///
///     SDE_CLICKHOUSE_DSN=clickhouse://default:sde@127.0.0.1:58123/sde ctest -L live

#include <atomic>
#include <chrono>
#include <cstdint>
#include <functional>
#include <future>
#include <map>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <thread>
#include <vector>

#include <gtest/gtest.h>

#include "engines/clickhouse/dsn.hpp"
#include "live/capture.hpp"
#include "live/clickhouse.hpp"
#include "live/http_stub.hpp"
#include "live/live.hpp"
#include "sde/clickhouse.hpp"
#include "sde/errors.hpp"
#include "sde/json.hpp"
#include "sde/write_fence.hpp"

namespace {

using sde::live::ClickHouseAdmin;
using sde::live::ClickHouseRoles;
using sde::live::ClickHouseScope;
using sde::live::ForwardingProxy;
using sde::live::refusal;
using Clock = std::chrono::steady_clock;

const std::string kProject(32, '1');
const std::string kEpoch(sde::EPOCH_COLUMN);
const std::string kHold(32, '2');

std::unique_ptr<sde::ClickHouseEngine> connected(const std::string& dsn) {
  auto made = std::make_unique<sde::ClickHouseEngine>(dsn);
  made->connect();
  return made;
}

/// The body of an HTTP request: the statement, when it carries no rows.
std::string body_of(const std::string& request) {
  const std::size_t end = request.find("\r\n\r\n");
  return end == std::string::npos ? std::string() : request.substr(end + 4);
}

class ClickHouseFences : public ::testing::Test {
 protected:
  void SetUp() override { SDE_REQUIRE_DSN("SDE_CLICKHOUSE_DSN", dsn_); }
  std::string dsn_;
};

// --- what a fence refuses -----------------------------------------------------------------------

TEST_F(ClickHouseFences, AFenceNeedsAnAtomicDatabase) {
  // Its drain detaches and attaches a table by UUID, which only an Atomic database keeps.
  const ClickHouseScope scope(dsn_, "sde_live_", "Memory");
  scope.admin().run("CREATE TABLE t (id Int64) ENGINE = Memory");
  const auto ch = connected(scope.dsn());
  const std::string refused = "write fences require a local Atomic ClickHouse database";
  EXPECT_EQ(refusal<sde::MigrationRefused>([&] { (void)ch->write_fence("t", kProject).prepare(1); }),
            refused);
  EXPECT_EQ(refusal<sde::MigrationRefused>([&] { (void)ch->write_fence("t", kProject).state(); }),
            refused);
}

TEST_F(ClickHouseFences, AFenceIsForAMergeTreeTableThatExists) {
  const ClickHouseScope scope(dsn_);
  const auto ch = connected(scope.dsn());
  const std::string missing =
      "write-fence table does not exist or is detached; resume the same operation";
  EXPECT_EQ(refusal<sde::EngineError>([&] { (void)ch->write_fence("nope", kProject).prepare(1); }),
            missing);
  EXPECT_EQ(refusal<sde::EngineError>([&] { (void)ch->write_fence("nope", kProject).state(); }),
            missing);
  scope.admin().run("CREATE TABLE logged (id Int64) ENGINE = Log");
  EXPECT_EQ(refusal<sde::MigrationRefused>(
                [&] { (void)ch->write_fence("logged", kProject).prepare(1); }),
            "write fences support local MergeTree and ReplacingMergeTree tables");
}

TEST_F(ClickHouseFences, AReservedColumnOfAnotherDefinitionIsRefused) {
  const ClickHouseScope scope(dsn_);
  const auto ch = connected(scope.dsn());
  scope.admin().run("CREATE TABLE text_epoch (id Int64, " + kEpoch +
                    " String) ENGINE = MergeTree ORDER BY id");
  scope.admin().run("CREATE TABLE other_default (id Int64, " + kEpoch +
                    " Int64 DEFAULT 1) ENGINE = MergeTree ORDER BY id");
  const std::string refused = "the reserved write-epoch column has an incompatible definition";
  for (const char* table : {"text_epoch", "other_default"}) {
    EXPECT_EQ(refusal<sde::MigrationRefused>(
                  [&] { (void)ch->write_fence(table, kProject).prepare(1); }),
              refused)
        << table;
    EXPECT_EQ(refusal<sde::MigrationRefused>([&] { (void)ch->write_fence(table, kProject).state(); }),
              refused)
        << table;
  }
}

TEST_F(ClickHouseFences, ADrainLogOfAnotherSchemaOrEngineIsRefusedAndTheTableStaysClosed) {
  // A refused drain fails closed, as the reference's does: the hold it was installing stays, and
  // the operation is resumed or released by name.
  const ClickHouseScope scope(dsn_);
  const ClickHouseAdmin admin = scope.admin();
  admin.run("CREATE TABLE __sde_fence_drains (table_name String, hold String) "
            "ENGINE = MergeTree ORDER BY hold");
  admin.run("CREATE TABLE events (id Int64) ENGINE = ReplacingMergeTree ORDER BY id");
  const auto ch = connected(scope.dsn());
  sde::WriteFence fence = ch->write_fence("events", kProject);
  const std::string schema = "the reserved write-fence drain log has an incompatible schema";
  EXPECT_EQ(refusal<sde::MigrationRefused>([&] { (void)fence.prepare(1); }), schema);
  EXPECT_EQ(refusal<sde::MigrationRefused>([&] { (void)fence.freeze(kHold); }), schema);
  sde::FenceState state = fence.state();
  EXPECT_TRUE(state.closed());
  EXPECT_EQ(state.holds, (std::vector<std::string>{kHold, "setup"}));
  state = fence.release(kHold);
  EXPECT_EQ(state.holds, std::vector<std::string>{"setup"});
  EXPECT_EQ(state.retired, std::vector<std::string>{kHold});

  admin.run("DROP TABLE __sde_fence_drains SYNC");
  admin.run("CREATE TABLE __sde_fence_drains (table_name String, table_uuid UUID, project_id "
            "FixedString(32), hold String) ENGINE = Log");
  const std::string other(32, '3');
  EXPECT_EQ(refusal<sde::MigrationRefused>([&] { (void)fence.freeze(other); }),
            "the reserved write-fence drain log has an incompatible engine");
  state = fence.release(other);
  EXPECT_EQ(state.holds, std::vector<std::string>{"setup"});
  EXPECT_EQ(state.retired, (std::vector<std::string>{kHold, other}));
}

TEST_F(ClickHouseFences, ATableDetachedByHandIsNotResumedWithoutItsDurableIntent) {
  // The intent names the table's UUID and the hold; a table detached by somebody else has neither,
  // and resuming it on the strength of its name would attach whatever has that name.
  const ClickHouseScope scope(dsn_);
  const ClickHouseAdmin admin = scope.admin();
  admin.run("CREATE TABLE hand (id Int64) ENGINE = ReplacingMergeTree ORDER BY id");
  const auto ch = connected(scope.dsn());
  sde::WriteFence fence = ch->write_fence("hand", kProject);
  EXPECT_EQ(fence.prepare(1).epoch(), 1);
  admin.run("DETACH TABLE hand PERMANENTLY SYNC");
  const std::string hold(32, '4');
  EXPECT_EQ(refusal<sde::MigrationRefused>([&] { (void)fence.resume(hold); }),
            "no matching durable intent for this detached write-fence table");
  const std::string detached =
      "write-fence table does not exist or is detached; resume the same operation";
  EXPECT_EQ(refusal<sde::EngineError>([&] { (void)fence.state(); }), detached);
  EXPECT_EQ(refusal<sde::EngineError>([&] { (void)fence.freeze(hold); }), detached);
  admin.run("ATTACH TABLE hand");
  EXPECT_FALSE(fence.state().closed()) << "attached by hand, the table is as it was";
}

// --- the drain ----------------------------------------------------------------------------------

TEST_F(ClickHouseFences, TheDrainBindsTheTablesExactUuid) {
  const ClickHouseScope scope(dsn_);
  const ClickHouseAdmin admin = scope.admin();
  admin.run("CREATE TABLE events (id Int64) ENGINE = ReplacingMergeTree ORDER BY id");
  const std::string uuid =
      admin.rows("SELECT toString(uuid) AS u FROM system.tables WHERE database = currentDatabase() "
                 "AND name = 'events'")[0]
          .find("u")
          ->as_string();
  const auto ch = connected(scope.dsn());
  sde::WriteFence fence = ch->write_fence("events", kProject);
  EXPECT_EQ(fence.prepare(1).epoch(), 1);
  EXPECT_TRUE(fence.freeze(kHold).closed());
  std::vector<std::string> intents;
  for (const sde::Json& row : admin.rows(
           "SELECT table_name, toString(table_uuid) AS id, toString(project_id) AS project, hold "
           "FROM __sde_fence_drains ORDER BY hold")) {
    intents.push_back(row.find("table_name")->as_string() + " " + row.find("id")->as_string() +
                      " " + row.find("project")->as_string() + " " + row.find("hold")->as_string());
  }
  EXPECT_EQ(intents, (std::vector<std::string>{"events " + uuid + " " + kProject + " " + kHold,
                                               "events " + uuid + " " + kProject + " setup"}));
  std::map<std::string, std::string> columns;
  for (const sde::Json& row : admin.rows("SELECT name, type FROM system.columns WHERE database = "
                                         "currentDatabase() AND table = '__sde_fence_drains'")) {
    columns[row.find("name")->as_string()] = row.find("type")->as_string();
  }
  EXPECT_EQ(columns, (std::map<std::string, std::string>{{"hold", "String"},
                                                         {"project_id", "FixedString(32)"},
                                                         {"table_name", "String"},
                                                         {"table_uuid", "UUID"}}));
  const sde::Json table = admin.rows("SELECT engine, sorting_key FROM system.tables WHERE database "
                                     "= currentDatabase() AND name = '__sde_fence_drains'")[0];
  EXPECT_EQ(table.find("engine")->as_string(), "MergeTree");
  EXPECT_EQ(table.find("sorting_key")->as_string(), "table_uuid, project_id, hold");
}

TEST_F(ClickHouseFences, AnInterruptionAfterDetachResumesOnlyTheLoggedTable) {
  // The reference makes its executor die after a confirmed DETACH; here a proxy refuses to pass the
  // ATTACH that follows it. The table is detached, every write refused, and resuming the same
  // operation attaches it - by the intent's UUID - with the barrier in place.
  const ClickHouseScope scope(dsn_);
  scope.admin().run("CREATE TABLE events (id Int64) ENGINE = ReplacingMergeTree ORDER BY id");
  const sde::detail::clickhouse::Target upstream = sde::detail::clickhouse::parse_dsn(scope.dsn());
  std::atomic<bool> interrupt{false};
  ForwardingProxy proxy(upstream.host, upstream.port, [&](const std::string& request) {
    return interrupt && body_of(request).starts_with("ATTACH TABLE") ? ForwardingProxy::Action::refuse
                                                                      : ForwardingProxy::Action::relay;
  });
  const auto ch = connected(sde::live::with_port(scope.dsn(), proxy.port()));
  sde::WriteFence fence = ch->write_fence("events", kProject);
  (void)fence.prepare(1);
  ch->insert("events", {{"id", std::int64_t{1}}, {kEpoch, std::int64_t{1}}});
  interrupt = true;
  EXPECT_EQ(refusal<sde::EngineError>([&] { (void)fence.freeze(kHold); }),
            "write-fence DDL failed; the table may remain closed or detached; resume the same "
            "operation: ClickHouse transport failed; the operation was not replayed and its outcome "
            "may be unknown");
  EXPECT_THROW(ch->insert("events", {{"id", std::int64_t{2}}, {kEpoch, std::int64_t{1}}}),
               sde::EngineError);
  interrupt = false;
  EXPECT_TRUE(fence.resume(kHold).closed());
  EXPECT_EQ(ch->count("events"), 1U);
  (void)fence.release(kHold);
  ch->insert("events", {{"id", std::int64_t{3}}, {kEpoch, std::int64_t{1}}});
  EXPECT_EQ(ch->count("events"), 2U);
}

TEST_F(ClickHouseFences, ABarrierWaitsForAnInsertUsingOldMetadata) {
  // An INSERT that began before the constraint holds the table's old metadata, and a metadata-only
  // ALTER returns before that row commits. The drain - DETACH, which waits for it - is what makes
  // the barrier closed when freeze returns: the old insert has committed, and it succeeded.
  const ClickHouseScope scope(dsn_);
  scope.admin().run("CREATE TABLE events (id Int64) ENGINE = ReplacingMergeTree ORDER BY id");
  const auto ch = connected(scope.dsn());
  sde::WriteFence fence = ch->write_fence("events", kProject);
  (void)fence.prepare(1);
  const std::string marker = "fence_insert_" + sde::live::fresh(16);
  auto writer = std::async(std::launch::async, [&] {
    ClickHouseAdmin(scope.dsn()).run("INSERT INTO events SELECT 1, 1 + sleep(2) /* " + marker + " */");
  });
  const auto deadline = Clock::now() + std::chrono::seconds(5);
  bool running = false;
  while (!running && Clock::now() < deadline) {
    running = scope.admin()
                  .rows("SELECT toString(count()) AS n FROM system.processes WHERE "
                        "startsWith(query, 'INSERT') AND position(query, '" + marker +
                        "') > 0 AND elapsed > 0.1")[0]
                  .find("n")
                  ->as_string() == "1";
    if (!running) std::this_thread::sleep_for(std::chrono::milliseconds(10));
  }
  ASSERT_TRUE(running) << "the old INSERT was not observed running";
  EXPECT_TRUE(fence.freeze(kHold).closed());
  EXPECT_EQ(ch->count("events"), 1U) << "freeze returned before the old insert committed";
  writer.get();  // and that insert succeeded
  EXPECT_THROW(ch->insert("events", {{"id", std::int64_t{2}}, {kEpoch, std::int64_t{1}}}),
               sde::EngineError);
}

TEST_F(ClickHouseFences, TheDrainIntentIsConfirmedEvenWithAsynchronousDefaults) {
  // A login whose defaults make an INSERT asynchronous and unacknowledged would let DETACH begin
  // before the intent is in the table, and an interrupted drain would then have no record to
  // resume by. The intent is written with those settings overridden; checked at the moment the
  // DETACH reaches the proxy, before it reaches the server.
  const ClickHouseRoles roles(dsn_, true);
  roles.command("CREATE TABLE events (id Int64) ENGINE = ReplacingMergeTree ORDER BY id");
  const std::string login = roles.operator_dsn();
  ClickHouseAdmin(dsn_).run("ALTER USER `" + roles.database() + "_op` SETTINGS async_insert = 1, "
                            "wait_for_async_insert = 0, async_insert_busy_timeout_ms = 2000, "
                            "async_insert_use_adaptive_busy_timeout = 0");
  const std::vector<sde::Json> settings = ClickHouseAdmin(login).rows(
      "SELECT toString(getSetting('async_insert')) AS a, "
      "toString(getSetting('wait_for_async_insert')) AS w");
  ASSERT_EQ(settings[0].find("a")->as_string(), "true");
  ASSERT_EQ(settings[0].find("w")->as_string(), "false");
  const sde::detail::clickhouse::Target upstream = sde::detail::clickhouse::parse_dsn(login);
  std::atomic<bool> watching{false};
  std::mutex mutex;
  std::vector<std::string> observed;
  ForwardingProxy proxy(upstream.host, upstream.port, [&](const std::string& request) {
    if (watching && body_of(request).starts_with("DETACH TABLE")) {
      const std::lock_guard<std::mutex> lock(mutex);
      observed.push_back(ClickHouseAdmin(dsn_)
                             .rows("SELECT toString(count()) AS n FROM `" + roles.database() +
                                   "`.__sde_fence_drains WHERE hold = '" + kHold + "'")[0]
                             .find("n")
                             ->as_string());
    }
    return ForwardingProxy::Action::relay;
  });
  const auto ch = connected(sde::live::with_port(login, proxy.port()));
  sde::WriteFence fence = ch->write_fence("events", kProject);
  (void)fence.prepare(1);
  watching = true;
  EXPECT_TRUE(fence.freeze(kHold).closed());
  const std::lock_guard<std::mutex> lock(mutex);
  EXPECT_EQ(observed, std::vector<std::string>{"1"});
}

}  // namespace
