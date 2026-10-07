/// Write barriers against a real PostgreSQL, where PostgreSQL's own mechanism is the claim: a
/// barrier that waits for the writer holding the table - also when it is retried, the constraint
/// already in place - and what a fence refuses: DDL inside an application transaction, a table that
/// is not an ordinary one, a reserved column made by hand in another definition. Ported from the
/// PostgreSQL-only cases of the reference's `test_write_fence_live.py`, and from cases it does not
/// test, checked against it on the same server. What both engines share is `test_generations.cpp`.
///
///     SDE_POSTGRES_DSN=postgresql://postgres:sde@127.0.0.1:55432/sde ctest -L live

#include <algorithm>
#include <chrono>
#include <functional>
#include <future>
#include <map>
#include <memory>
#include <optional>
#include <stdexcept>
#include <string>
#include <thread>
#include <utility>
#include <vector>

#include <gtest/gtest.h>

#include "live/live.hpp"
#include "live/postgres.hpp"
#include "sde/errors.hpp"
#include "sde/json.hpp"
#include "sde/migration.hpp"
#include "sde/model.hpp"
#include "sde/placement.hpp"
#include "sde/postgres.hpp"
#include "sde/provisioning.hpp"
#include "sde/session.hpp"
#include "sde/verification.hpp"
#include "sde/write_fence.hpp"
#include "support/signer.hpp"

namespace {

using sde::live::Admin;
using sde::live::Scope;
using Clock = std::chrono::steady_clock;

const std::string kProject = "11111111111111111111111111111111";
const std::string kEpoch(sde::EPOCH_COLUMN);

/// A table name that needs its quotes doubled, so every statement a fence issues is one that
/// quotes it.
const std::string kTable = "event \"quoted";
const std::string kQuoted = "\"event \"\"quoted\"";

// --- one fenced table ---------------------------------------------------------------------------

/// A schema of its own holding one fenced table, `event "quoted`, and an adapter on it whose
/// backend another connection can find by its application name.
class PostgresFence : public ::testing::Test {
 protected:
  void SetUp() override {
    SDE_REQUIRE_DSN("SDE_POSTGRES_DSN", dsn_);
    scope_ = std::make_unique<Scope>(dsn_);
    application_ = "sde_fence_" + sde::live::fresh(12);
    engine_ = std::make_unique<sde::PostgresEngine>(
        sde::live::conninfo(scope_->dsn(), {{"application_name", application_},
                                            {"options", "-csearch_path=" + scope_->name()}}));
    engine_->connect();
    Admin(scope_->dsn()).run("CREATE TABLE " + kQuoted + " (id bigint PRIMARY KEY)");
  }

  sde::WriteFence fence() { return engine_->write_fence(kTable, kProject); }
  void insert(std::int64_t id, std::optional<std::int64_t> epoch = std::nullopt) {
    sde::Row row{{"id", id}};
    if (epoch) row[kEpoch] = *epoch;
    engine_->insert(kTable, row);
  }
  std::string dsn_;
  std::unique_ptr<Scope> scope_;
  std::string application_;
  std::unique_ptr<sde::PostgresEngine> engine_;
};

TEST_F(PostgresFence, ABarrierWaitsForTheWriterHoldingTheTable) {
  (void)fence().prepare(1);
  Admin writer(scope_->dsn());
  Admin observer(dsn_);
  (void)writer.run("BEGIN");
  (void)writer.run("INSERT INTO " + kQuoted + " (id, \"" + kEpoch + "\") VALUES (1, 1)");
  std::future<sde::FenceState> frozen =
      std::async(std::launch::async, [&] { return fence().freeze(std::string(32, '2')); });
  bool waiting = false;
  for (const auto deadline = Clock::now() + std::chrono::seconds(5); Clock::now() < deadline;) {
    const Admin::Rows rows = observer.rows(
        "SELECT wait_event_type FROM pg_stat_activity WHERE application_name = $1",
        {application_});
    if (!rows.empty() && rows[0][0] == std::optional<std::string>("Lock")) {
      waiting = true;
      break;
    }
    std::this_thread::sleep_for(std::chrono::milliseconds(10));
  }
  EXPECT_TRUE(waiting) << "the barrier was not seen waiting for the writer";
  EXPECT_EQ(frozen.wait_for(std::chrono::seconds(0)), std::future_status::timeout);
  (void)writer.run("COMMIT");
  ASSERT_EQ(frozen.wait_for(std::chrono::seconds(5)), std::future_status::ready);
  EXPECT_TRUE(frozen.get().closed());
  EXPECT_EQ(engine_->count(kTable), 1U);
  EXPECT_THROW(insert(2, 1), sde::EngineError);
}

TEST_F(PostgresFence, ARetriedBarrierStillWaitsForAWriterHoldingTheTable) {
  // On a retry the barrier's constraint exists, so no DDL runs and nothing else waits: the drain's
  // lock alone proves that every writer holding the table has finished. A delete is such a writer,
  // since no CHECK constraint refuses it.
  const std::string hold(32, '2');
  (void)fence().prepare(1);
  insert(1, 1);
  (void)fence().freeze(hold);
  Admin writer(scope_->dsn());
  Admin observer(dsn_);
  (void)writer.run("BEGIN");
  (void)writer.run("DELETE FROM " + kQuoted + " WHERE id = 1");
  std::future<sde::FenceState> frozen =
      std::async(std::launch::async, [&] { return fence().freeze(hold); });
  bool waiting = false;
  for (const auto deadline = Clock::now() + std::chrono::seconds(5); Clock::now() < deadline;) {
    const Admin::Rows rows = observer.rows(
        "SELECT wait_event_type FROM pg_stat_activity WHERE application_name = $1",
        {application_});
    if (!rows.empty() && rows[0][0] == std::optional<std::string>("Lock")) {
      waiting = true;
      break;
    }
    if (frozen.wait_for(std::chrono::milliseconds(10)) == std::future_status::ready) break;
  }
  EXPECT_TRUE(waiting) << "the retried barrier returned while a writer still held the table";
  (void)writer.run("COMMIT");
  ASSERT_EQ(frozen.wait_for(std::chrono::seconds(5)), std::future_status::ready);
  EXPECT_TRUE(frozen.get().closed());
  EXPECT_EQ(engine_->count(kTable), 0U);
}

TEST_F(PostgresFence, FenceDdlIsRefusedInsideAnApplicationTransaction) {
  // DDL on the application's connection would commit or roll back with the application's work.
  engine_->transaction([&] {
    try {
      (void)fence().prepare(1);
      ADD_FAILURE() << "a fence was installed inside an application transaction";
    } catch (const sde::MigrationRefused& error) {
      EXPECT_STREQ(error.what(), "write-fence DDL cannot run inside an application transaction");
    }
  });
  EXPECT_FALSE(fence().state().complete());
}

TEST_F(PostgresFence, AFenceIsForAnOrdinaryTableAlone) {
  Admin admin(scope_->dsn());
  (void)admin.run("CREATE TABLE parent (id bigint PRIMARY KEY)");
  (void)admin.run("CREATE TABLE child () INHERITS (parent)");
  (void)admin.run("CREATE VIEW shown AS SELECT id FROM parent");
  for (const std::string& table : std::vector<std::string>{"parent", "child", "shown"}) {
    try {
      (void)engine_->write_fence(table, kProject).state();
      ADD_FAILURE() << table << " was fenced";
    } catch (const sde::MigrationRefused& error) {
      EXPECT_STREQ(error.what(),
                   "write fences support ordinary PostgreSQL tables without inheritance")
          << table;
    }
  }
  try {
    (void)engine_->write_fence("absent", kProject).state();
    ADD_FAILURE() << "a table that does not exist was fenced";
  } catch (const sde::EngineError& error) {
    EXPECT_STREQ(error.what(), "write-fence table does not exist");
  }
}

TEST_F(PostgresFence, AReservedColumnOfAnotherDefinitionIsRefused) {
  Admin admin(scope_->dsn());
  int n = 0;
  for (const std::string& definition :
       std::vector<std::string>{"integer NOT NULL DEFAULT 0", "bigint DEFAULT 0",
                                "bigint NOT NULL DEFAULT 1", "bigint NOT NULL",
                                "bigint NOT NULL GENERATED ALWAYS AS (0) STORED"}) {
    const std::string table = "conflict_" + std::to_string(++n);
    (void)admin.run("CREATE TABLE " + table + " (id bigint PRIMARY KEY, \"" + kEpoch + "\" " +
                    definition + ")");
    try {
      (void)engine_->write_fence(table, kProject).state();
      ADD_FAILURE() << definition << " was taken for the reserved column";
    } catch (const sde::MigrationRefused& error) {
      EXPECT_STREQ(error.what(), "the reserved write-epoch column has an incompatible definition")
          << definition;
    }
  }
  // The reserved definition itself, made by hand, is the reserved column.
  (void)admin.run("CREATE TABLE reserved (id bigint PRIMARY KEY, \"" + kEpoch +
                  "\" bigint NOT NULL DEFAULT 0)");
  EXPECT_EQ(engine_->write_fence("reserved", kProject).state().column, sde::ColumnState::valid);
}

}  // namespace
