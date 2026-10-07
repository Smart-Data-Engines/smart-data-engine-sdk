/// The forward-only bookkeeping and native batches against a real PostgreSQL: `max()` over an
/// empty table, an append-only watermark, one statement per batch inside savepoints. Ported from
/// the reference's `test_rollback_protection_live.py` and `test_bulk_live.py`. Copies between
/// engines, PostgreSQL's own among them, are `test_copies.cpp`.
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

// --- the forward-only bookkeeping ---------------------------------------------------------------

class PostgresWatermark : public ::testing::Test {
 protected:
  void SetUp() override {
    SDE_REQUIRE_DSN("SDE_POSTGRES_DSN", dsn_);
    scope_ = std::make_unique<Scope>(dsn_);
    engine_ = std::make_unique<sde::PostgresEngine>(scope_->dsn());
    engine_->connect();
  }

  /// A signed contract-1 map of the model's every group in this engine, at `version`.
  sde::PlacementMap signed_map(int version) {
    std::string groups;
    for (const sde::Group& group : model_.groups()) {
      groups += (groups.empty() ? "\"" : ", \"") + group.name + R"(": {"source": {"id": ")" +
                group.name + R"(@e", "engine": "e", "layout": {"auto": true}}})";
    }
    sde::LoadOptions options;
    options.model = &model_;
    options.public_keys = signer_.keys();
    return sde::load_map(signer_.signed_document(sde::parse_json(
                             R"({"contract": 1, "model_version": ")" + model_.version() +
                             R"(", "map_version": )" + std::to_string(version) + R"(, "groups": {)" +
                             groups + "}}")),
                         options);
  }

  /// Opens a session on the map of `version`, which records it or refuses it.
  sde::WatermarkCheck open(int version) {
    const sde::PlacementMap map = signed_map(version);
    const sde::Session session(model_, map, {{"e", engine_.get()}});
    return session.rollback_protection();
  }

  std::int64_t rows() {
    return std::stoll(*Admin(scope_->dsn())
                           .rows("SELECT count(*) FROM \"" + std::string(sde::WATERMARK_TABLE) +
                                 "\"")
                           .at(0)
                           .at(0));
  }

  std::string dsn_;
  std::unique_ptr<Scope> scope_;
  std::unique_ptr<sde::PostgresEngine> engine_;
  sde::Model model_ = sde::load_neutral_model(R"({"entities": [{"name": "Reading", "fields": [
      {"name": "id", "type": "uuid"}, {"name": "station", "type": "string"}], "key": ["id"]}]})");
  sde::testing_support::Signer signer_{"the PostgreSQL watermark tests"};
};

TEST_F(PostgresWatermark, AnEmptyEngineReportsNoWatermarkAndCreatesTheTable) {
  // max() over nothing: PostgreSQL's null comes back as "nothing has been applied".
  EXPECT_FALSE(engine_->map_watermark().has_value());
  // The table exists now, and asking again is the ordinary path on every restart.
  EXPECT_FALSE(engine_->map_watermark().has_value());
  EXPECT_EQ(rows(), 0);
}

TEST_F(PostgresWatermark, TheWatermarkAdvancesAndARollbackIsRefused) {
  EXPECT_EQ(open(4).protection, "enforced");
  EXPECT_EQ(engine_->map_watermark(), 4);
  (void)open(9);
  EXPECT_EQ(engine_->map_watermark(), 9);
  try {
    (void)open(8);
    ADD_FAILURE() << "an older map was accepted";
  } catch (const sde::MapRolledBack& error) {
    EXPECT_NE(std::string(error.what()).find("version 9 has already been applied"),
              std::string::npos)
        << error.what();
  }
  // Equal is allowed, and the watermark does not move.
  (void)open(9);
  EXPECT_EQ(engine_->map_watermark(), 9);
}

TEST_F(PostgresWatermark, TheDocumentedEscapeActuallyWorks) {
  // The refusal tells an operator to delete the rows above the version they want. It has to work:
  // a message naming a remedy that does not is worse than one naming none.
  (void)open(9);
  EXPECT_THROW((void)open(5), sde::MapRolledBack);
  Admin(scope_->dsn())
      .run("DELETE FROM \"" + std::string(sde::WATERMARK_TABLE) + "\" WHERE map_version > 5");
  EXPECT_EQ(open(5).protection, "enforced");
  EXPECT_EQ(engine_->map_watermark(), 5);
}

TEST_F(PostgresWatermark, TheBookkeepingHoldsOneRowPerVersionAndNotPerStart) {
  for (const int version : {1, 2, 2, 2, 3}) (void)open(version);
  EXPECT_EQ(rows(), 3);
  EXPECT_EQ(engine_->map_watermark(), 3);
}

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

TEST_F(PostgresBatches, OneNativeBatchKeepsEveryRowAndEveryMicrosecond) {
  Batches batches(dsn_, "timestamptz");
  std::vector<sde::Row> values;
  for (int i = 0; i < 1000; ++i) {
    values.push_back({{"id", std::int64_t{i}}, {"value", instant(i)}});
  }
  batches.session->save_many("Event", values);
  EXPECT_EQ(batches.provisioning->count(batches.table), 1000U);
  EXPECT_EQ(batches.provisioning->key_range(batches.table, {"id"}, std::nullopt, std::nullopt,
                                            std::nullopt),
            values);
  EXPECT_EQ(batches.session->get("Event", {{"id", std::int64_t{999}}}), values.back());
}

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

TEST_F(PostgresBatches, ABadBatchNeverReachesTheServer) {
  Batches batches(dsn_, "timestamptz");
  EXPECT_THROW(batches.session->save_many(
                   "Event", {{{"id", std::int64_t{1}}, {"value", instant(0)}}, {{"id", std::int64_t{2}}}}),
               sde::BulkWriteRefused);
  EXPECT_EQ(batches.provisioning->count(batches.table), 0U);
  EXPECT_THROW(batches.runtime->insert_many(
                   batches.table, {{{"a b", std::int64_t{1}}, {"c", std::int64_t{2}}},
                                   {{"a", std::int64_t{1}}, {"b c", std::int64_t{2}}}}),
               sde::BulkWriteRefused);
  EXPECT_EQ(batches.provisioning->count(batches.table), 0U);
}

}  // namespace
