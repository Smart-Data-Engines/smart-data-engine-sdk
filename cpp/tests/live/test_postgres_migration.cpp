/// Backfill, verification, the forward-only bookkeeping and native batches against a real
/// PostgreSQL: everything here that is SQL - a keyset scan over a composite key written as a row
/// comparison, an idempotent copy through the primary key, `max()` over an empty table, an
/// append-only watermark, one statement per batch inside savepoints. Ported from the reference's
/// `test_migration_live.py`, `test_rollback_protection_live.py` and `test_bulk_live.py`, the
/// PostgreSQL directions; ClickHouse's join them with its adapter.
///
/// A copy within PostgreSQL runs both ways the reference's does and one more: through one adapter,
/// as the reference's pg-to-pg does, and through two adapters on two schemas, so the marker lives
/// in a target the source's connection cannot see.
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

const std::string kSource = "mig_src";
const std::string kTarget = "mig_dst";

/// No timestamp, deliberately: PostgreSQL keeps six sub-second digits and ClickHouse three, and a
/// copy between them refuses such a column before it starts. The subject here is the copy itself.
sde::Model readings() {
  return sde::load_neutral_model(R"({"entities": [{"name": "Reading", "fields": [
      {"name": "tenant", "type": "int32"}, {"name": "seq", "type": "int32"},
      {"name": "station", "type": "string"}], "key": ["tenant", "seq"]}]})");
}

sde::PlacementMap copying(const sde::Model& model) {
  const std::string columns =
      R"({"Reading": {"tenant": "integer", "seq": "integer", "station": "text"}})";
  sde::LoadOptions options;
  options.model = &model;
  return sde::load_map(
      R"({"contract": 3, "model_version": ")" + model.version() + R"(", "map_version": 1,
          "groups": {"Reading": {
            "source": {"id": "src", "engine": "source", "layout": {
              "tables": {"Reading": ")" + kSource + R"("}, "columns": )" + columns + R"(}},
            "derived": [{"id": "dst", "engine": "target", "lag_budget_ms": 30000, "layout": {
              "tables": {"Reading": ")" + kTarget + R"("}, "columns": )" + columns + R"(}}],
            "also_write": ["dst"]}}})",
      options);
}

/// A group copied within PostgreSQL: through one adapter, or two on two schemas.
class PostgresCopy : public ::testing::TestWithParam<bool> {
 protected:
  void SetUp() override {
    SDE_REQUIRE_DSN("SDE_POSTGRES_DSN", dsn_);
    source_scope_ = std::make_unique<Scope>(dsn_);
    source_ = connected(source_scope_->dsn());
    if (GetParam()) {
      target_scope_ = std::make_unique<Scope>(dsn_);
      target_ = connected(target_scope_->dsn());
    }
    model_ = std::make_unique<sde::Model>(readings());
    map_ = std::make_unique<sde::PlacementMap>(copying(*model_));
    open();
  }

  static std::unique_ptr<sde::PostgresEngine> connected(const std::string& dsn) {
    auto made = std::make_unique<sde::PostgresEngine>(dsn);
    made->connect();
    return made;
  }

  /// A fresh session on the adapters as they are, schema ensured.
  void open(sde::Recorder* recorder = nullptr) {
    sde::SessionOptions options;
    options.recorder = recorder;
    session_.reset();
    session_ = std::make_unique<sde::Session>(
        *model_, *map_, std::map<std::string, sde::Engine*>{{"source", source_.get()},
                                                            {"target", &target()}},
        options);
    session_->ensure_schema();
  }

  sde::PostgresEngine& target() { return target_ ? *target_ : *source_; }
  [[nodiscard]] std::string target_dsn() const {
    return target_scope_ ? target_scope_->dsn() : source_scope_->dsn();
  }

  /// Rows straight into the source, past the fan-out: the rows that existed when dual write began,
  /// which the backfill's whole job is to move.
  void fill(int rows) {
    for (int n = 1; n <= rows; ++n) {
      source_->insert(kSource, {{"tenant", std::int64_t{1 + n % 3}},
                                {"seq", std::int64_t{n}},
                                {"station", "s" + std::to_string(n)}});
    }
  }

  /// One row out of the copy, by hand: deliberate damage is the only way to test a gate.
  void remove(const std::string& where) {
    Admin(target_dsn()).run("DELETE FROM \"" + kTarget + "\" WHERE " + where);
  }

  sde::BackfillProgress copy(std::int64_t chunk, std::optional<std::int64_t> stop = {}) {
    sde::BackfillOptions options;
    options.chunk_rows = chunk;
    options.stop_after = stop;
    return sde::backfill(*session_, "Reading", options);
  }

  sde::VerifyReport compare(std::int64_t chunk) {
    sde::VerifyOptions options;
    options.chunk_rows = chunk;
    return sde::verify(*session_, "Reading", options);
  }

  std::string dsn_;
  std::unique_ptr<Scope> source_scope_;
  std::unique_ptr<Scope> target_scope_;
  std::unique_ptr<sde::PostgresEngine> source_;
  std::unique_ptr<sde::PostgresEngine> target_;
  std::unique_ptr<sde::Model> model_;
  std::unique_ptr<sde::PlacementMap> map_;
  std::unique_ptr<sde::Recorder> recorder_;  ///< declared before the session, so it outlives it
  std::unique_ptr<sde::Session> session_;
};

TEST_P(PostgresCopy, AGroupIsCopiedInChunksAndTheCopyVerifies) {
  fill(25);
  const sde::BackfillProgress progress = copy(7);
  EXPECT_TRUE(progress.complete());
  EXPECT_EQ(progress.rows_this_run(), 25);
  EXPECT_EQ(target().count(kTarget), 25U);
  const sde::VerifyReport report = compare(7);
  EXPECT_TRUE(report.matched()) << report.for_a_human();
  EXPECT_EQ(report.chunks_compared, 4);
  EXPECT_EQ(report.rows_source, 25);
  EXPECT_EQ(report.rows_target, 25);
}

TEST_P(PostgresCopy, TheMarkerSurvivesANewAdapterAndTheBackfillResumes) {
  // A fresh adapter and a fresh session, which is what an operator re-running an interrupted job
  // has. Anything kept in the process would have looked the same up to here.
  fill(20);
  const sde::BackfillProgress first = copy(5, 2);
  EXPECT_FALSE(first.complete());
  EXPECT_EQ(first.rows_this_run(), 10);
  session_.reset();
  source_ = connected(source_scope_->dsn());
  if (target_) target_ = connected(target_scope_->dsn());
  open();
  const sde::BackfillProgress second = copy(5);
  EXPECT_TRUE(second.complete());
  EXPECT_EQ(second.rows_this_run(), 10);
  EXPECT_EQ(target().count(kTarget), 20U);
  EXPECT_TRUE(compare(5).matched());
}

TEST_P(PostgresCopy, ARecopiedChunkLeavesOneRow) {
  // The idempotence the write-then-marker order depends on: PostgreSQL skips the duplicate through
  // the primary key the layout created.
  fill(12);
  EXPECT_TRUE(copy(12).complete());
  EXPECT_EQ(target().backfill_marker("dst", "Reading"), 12);
  // The crash window, reproduced: the chunk landed and the marker did not move.
  target().record_backfill_marker("dst", "Reading", 0);
  Admin(target_dsn()).run("DROP TABLE \"" + std::string(sde::BACKFILL_TABLE) + "\"");
  EXPECT_EQ(target().backfill_marker("dst", "Reading"), 0);
  EXPECT_TRUE(copy(12).complete());
  EXPECT_EQ(target().count(kTarget), 12U);
  EXPECT_TRUE(compare(12).matched());
}

TEST_P(PostgresCopy, ARowDeletedFromTheCopyStopsTheMigrationAndNamesTheRow) {
  fill(15);
  EXPECT_TRUE(copy(5).complete());
  remove("seq = 7");
  const sde::VerifyReport report = compare(5);
  EXPECT_FALSE(report.matched());
  EXPECT_EQ(report.chunks_mismatched, 1);
  EXPECT_EQ(report.tail_rows_missing_in_target, 0);
  ASSERT_FALSE(report.differences.empty());
  EXPECT_EQ(report.differences[0].key.at("seq"), sde::Value(std::int64_t{7}));
  EXPECT_TRUE(report.differences[0].absent());
}

TEST_P(PostgresCopy, AWriteTheFanOutLostIsCaughtAboveTheMarker) {
  // Tenant 4 sorts above every key the fill wrote, so the row lands above the marker.
  fill(10);
  EXPECT_TRUE(copy(10).complete());
  session_->save("Reading", {{"tenant", std::int64_t{4}},
                             {"seq", std::int64_t{999}},
                             {"station", std::string("arrived-by-fan-out")}});
  EXPECT_EQ(target().count(kTarget), 11U);
  remove("seq = 999");
  const sde::VerifyReport report = compare(10);
  EXPECT_FALSE(report.matched());
  EXPECT_EQ(report.chunks_mismatched, 0);
  EXPECT_EQ(report.tail_rows_read, 1);
  EXPECT_EQ(report.tail_rows_missing_in_target, 1);
}

TEST_P(PostgresCopy, AValueChangedInTheCopyIsFoundAndTheColumnNamed) {
  fill(6);
  EXPECT_TRUE(copy(6).complete());
  remove("seq = 3");
  target().insert(kTarget, {{"tenant", std::int64_t{1}},
                            {"seq", std::int64_t{3}},
                            {"station", std::string("not-what-was-written")}});
  const sde::VerifyReport report = compare(6);
  EXPECT_FALSE(report.matched());
  ASSERT_FALSE(report.differences.empty());
  EXPECT_EQ(report.differences[0].columns, std::vector<std::string>{"station"});
}

TEST_P(PostgresCopy, AnUntouchedEngineReportsNoMarkerAndTheMarkerIsTheHighest) {
  // max() over nothing is null in PostgreSQL, and means nothing has been copied.
  EXPECT_EQ(target().backfill_marker("dst", "Reading"), 0);
  EXPECT_EQ(target().backfill_marker("dst", "Reading"), 0);
  target().record_backfill_marker("dst", "Reading", 3);
  target().record_backfill_marker("dst", "Reading", 1);
  EXPECT_EQ(target().backfill_marker("dst", "Reading"), 3)
      << "append-only, and the answer is max() - a stale row cannot lower the marker";
}

TEST_P(PostgresCopy, TheMeasuredLagOfACopyIsARealServersWrite) {
  // The number is the point: not that a percentile exists, but that it is a plausible write.
  recorder_ = std::make_unique<sde::Recorder>(*model_);
  open(recorder_.get());
  for (int n = 1; n <= 10; ++n) {
    session_->save("Reading", {{"tenant", std::int64_t{1}},
                               {"seq", std::int64_t{n}},
                               {"station", "s" + std::to_string(n)}});
  }
  const std::optional<sde::Window> window = recorder_->roll();
  ASSERT_TRUE(window.has_value());
  const std::vector<sde::CopyFreshness> copies = window->copies("Reading");
  ASSERT_EQ(copies.size(), 1U);
  EXPECT_EQ(copies[0].materialization, "dst");
  EXPECT_EQ(copies[0].writes, 10U);
  EXPECT_EQ(copies[0].failures, 0U);
  EXPECT_TRUE(copies[0].complete());
  ASSERT_TRUE(copies[0].lag_p50_ms.has_value());
  ASSERT_TRUE(copies[0].lag_p99_ms.has_value());
  EXPECT_GE(*copies[0].lag_p50_ms, 0.001) << "a fan-out to a real server is not that fast";
  EXPECT_LT(*copies[0].lag_p99_ms, 5000.0) << "and it is a write, not a hang";
  EXPECT_EQ(target().count(kTarget), 10U);
}

INSTANTIATE_TEST_SUITE_P(PostgresToPostgres, PostgresCopy, ::testing::Values(false, true),
                         [](const ::testing::TestParamInfo<bool>& param_info) {
                           return param_info.param ? std::string("TwoAdapters")
                                             : std::string("OneAdapter");
                         });

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
