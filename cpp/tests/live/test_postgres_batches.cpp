/// Native batches against a real PostgreSQL, where they differ from ClickHouse's: one statement a
/// batch, conflicts and nested rollbacks inside savepoints, and a JSON value that is the document.
/// Ported from the PostgreSQL-only cases of the reference's `test_bulk_live.py`; what both engines
/// share is `test_bookkeeping.cpp`, and copies are `test_copies.cpp`.
///
///     SDE_POSTGRES_DSN=postgresql://postgres:sde@127.0.0.1:55432/sde ctest -L live

#include <functional>
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
#include "sde/migration.hpp"
#include "sde/model.hpp"
#include "sde/placement.hpp"
#include "sde/postgres.hpp"
#include "sde/session.hpp"
#include "sde/telemetry.hpp"
#include "support/signer.hpp"

namespace {

using sde::live::Admin;
using sde::live::Roles;
using sde::live::Scope;

// --- native batches -----------------------------------------------------------------------------

/// One entity, `Event`, with a value of `type`, created by the provisioning login and written by a
/// session on the runtime one.
struct Batches {
  Roles roles;
  sde::Model model;
  sde::PlacementMap map;
  std::unique_ptr<sde::PostgresEngine> provisioning;
  std::unique_ptr<sde::PostgresEngine> runtime;
  std::unique_ptr<sde::Session> session;
  std::string table;

  static sde::PlacementMap auto_map(const sde::Model& model) {
    sde::LoadOptions options;
    options.model = &model;
    return sde::load_map(R"({"contract": 3, "model_version": ")" + model.version() +
                             R"(", "map_version": 1, "groups": {"Event": {"source": {
                             "id": "source", "engine": "db", "layout": {"auto": true}}}}})",
                         options);
  }

  Batches(const std::string& dsn, const std::string& type)
      : roles(dsn),
        model(sde::load_neutral_model(
            R"({"entities": [{"name": "Event", "fields": [{"name": "id", "type": "int64"},
                {"name": "value", "type": ")" + type + R"("}], "key": ["id"]}]})")),
        map(auto_map(model)) {
    const sde::PhysicalLayout& layout = map.placement_of("Event").source.layout;
    table = layout.table_for("Event");
    provisioning = std::make_unique<sde::PostgresEngine>(roles.operator_dsn());
    provisioning->connect();
    (void)provisioning->ensure_schema(layout, {{"Event", {"id"}}});
    roles.grant(table);
    runtime = std::make_unique<sde::PostgresEngine>(roles.runtime_dsn());
    runtime->connect();
    session = std::make_unique<sde::Session>(
        model, map, std::map<std::string, sde::Engine*>{{"db", runtime.get()}});
  }
};

sde::Value instant(int micros) {
  return *sde::TimestampTz::from_micros(1789344000000000LL + 123456 + micros);
}

class PostgresBatches : public ::testing::Test {
 protected:
  void SetUp() override { SDE_REQUIRE_DSN("SDE_POSTGRES_DSN", dsn_); }
  std::string dsn_;
};

TEST_F(PostgresBatches, ABatchIsOneStatementAndKeepsConflictsAndNestedRollbacks) {
  Batches batches(dsn_, "timestamptz");
  Admin admin(batches.roles.operator_dsn());
  (void)admin.run("CREATE TABLE insert_calls (n integer)");
  (void)admin.run("GRANT INSERT ON insert_calls TO \"" + batches.roles.username() + "\"");
  (void)admin.run(
      "CREATE FUNCTION record_insert() RETURNS trigger LANGUAGE plpgsql AS $$ "
      "BEGIN INSERT INTO insert_calls VALUES (1); RETURN NULL; END $$");
  (void)admin.run("CREATE TRIGGER count_insert AFTER INSERT ON \"" + batches.table +
                  "\" FOR EACH STATEMENT EXECUTE FUNCTION record_insert()");
  const auto count = [&] { return batches.provisioning->count(batches.table); };
  const sde::Value value = instant(0);
  const std::vector<sde::Row> batch = {{{"id", std::int64_t{1}}, {"value", value}},
                                       {{"id", std::int64_t{2}}, {"value", value}}};
  sde::Session& session = *batches.session;
  session.save_many("Event", batch);
  EXPECT_EQ(admin.rows("SELECT count(*) FROM insert_calls"),
            (Admin::Rows{{std::string("1")}}))
      << "a batch of two was not one statement";
  EXPECT_THROW(session.save_many("Event", {{{"id", std::int64_t{3}}, {"value", value}}, batch[0]}),
               sde::EngineError);
  EXPECT_EQ(count(), 2U);
  session.transaction({"Event"}, [&] {
    session.save_many("Event", {{{"id", std::int64_t{3}}, {"value", value}}});
    EXPECT_THROW(session.transaction({"Event"}, [&] {
      session.save_many("Event", {{{"id", std::int64_t{4}}, {"value", value}}, batch[0]});
    }),
                 sde::EngineError);
  });
  EXPECT_EQ(count(), 3U);
  struct Rollback {};
  EXPECT_THROW(session.transaction({"Event"}, [&] {
    session.transaction({"Event"}, [&] {
      session.save_many("Event", {{{"id", std::int64_t{5}}, {"value", value}}});
    });
    throw Rollback{};
  }),
               Rollback);
  EXPECT_EQ(count(), 3U);
  try {
    session.transaction({"Event"}, [&] {
      session.save_many("Event", {{{"id", std::int64_t{6}}, {"value", value}}});
      EXPECT_THROW(session.save_many("Event", batch), sde::EngineError);
    });
    ADD_FAILURE() << "a transaction whose statement failed was reported as committed";
  } catch (const sde::EngineError& error) {
    EXPECT_NE(std::string(error.what()).find("aborted"), std::string::npos) << error.what();
  }
  EXPECT_EQ(count(), 3U);
}

TEST_F(PostgresBatches, AJsonValueInABatchIsTheDocument) {
  Batches batches(dsn_, "json");
  const sde::Value nested = sde::JsonDocument{sde::parse_json(R"({"nested": ["zażółć", {"n": 7}]})")};
  const sde::Value listed = sde::JsonDocument{sde::parse_json(R"([1, 2, {"a": true}])")};
  batches.session->transaction({"Event"}, [&] {
    batches.session->save_many("Event", {{{"id", std::int64_t{1}}, {"value", nested}},
                                         {{"id", std::int64_t{2}}, {"value", listed}}});
  });
  EXPECT_EQ(batches.provisioning->get(batches.table, {{"id", std::int64_t{1}}})->at("value"), nested);
  EXPECT_EQ(batches.provisioning->get(batches.table, {{"id", std::int64_t{2}}})->at("value"), listed);
}

}  // namespace
