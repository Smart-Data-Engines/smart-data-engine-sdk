/// Write generations against a real PostgreSQL: native CHECK constraints that refuse an unstamped,
/// an old and a future writer; a barrier that waits for the writer holding the table; sessions
/// that keep their own generation on a shared connection; a copy stamped with the generation in
/// force; and the comparison under named barriers that closes the native boundary rather than
/// inferring it from counts. Ported from the reference's `test_write_fence_live.py`,
/// `test_generation_session_live.py`, `test_generation_migration_live.py`,
/// `test_frozen_verification_live.py` and the generation case of `test_bulk_live.py`; the copies
/// the reference makes between PostgreSQL and ClickHouse run here between two PostgreSQL schemas,
/// and join ClickHouse with its adapter.
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

std::string message_of(const std::function<void()>& body) {
  try {
    body();
  } catch (const std::exception& error) {
    return error.what();
  }
  return "";
}

bool contains(const std::string& text, const std::string& part) {
  return text.find(part) != std::string::npos;
}

// --- one fenced table ---------------------------------------------------------------------------

/// The reference's `generation_map`: one entity, `Record`, in `table`, at a generation.
sde::Model records(bool with_label = false) {
  return sde::load_neutral_model(
      std::string(R"({"entities": [{"name": "Record", "fields": [{"name": "id", "type": "int64"})") +
      (with_label ? R"(, {"name": "label", "type": "string"})" : "") + R"(], "key": ["id"]}]})");
}

sde::PlacementMap generation_map(const sde::Model& model, const std::string& table, int epoch = 1,
                                 int version = 1,
                                 const sde::testing_support::Signer* signer = nullptr,
                                 bool with_label = false) {
  sde::Json columns = sde::Json::object();
  columns.set("id", "bigint");
  if (with_label) columns.set("label", "text");
  sde::Json tables = sde::Json::object();
  tables.set("Record", table);
  sde::Json typed = sde::Json::object();
  typed.set("Record", std::move(columns));
  sde::Json layout = sde::Json::object();
  layout.set("tables", std::move(tables));
  layout.set("columns", std::move(typed));
  sde::Json document = sde::parse_json(
      R"({"contract": 4, "project_id": ")" + kProject + R"(", "model_version": ")" +
      model.version() + R"(", "map_version": )" + std::to_string(version) +
      R"(, "groups": {"Record": {"write_epoch": )" + std::to_string(epoch) +
      R"(, "source": {"id": "source", "engine": "db"}}}})");
  sde::Json groups = *document.find("groups");
  sde::Json record = *groups.find("Record");
  sde::Json source = *record.find("source");
  source.set("layout", std::move(layout));
  record.set("source", std::move(source));
  groups.set("Record", std::move(record));
  document.set("groups", std::move(groups));
  sde::LoadOptions options;
  options.model = &model;
  if (signer == nullptr) return sde::load_map(document, options);
  options.public_keys = signer->keys();
  return sde::load_map(signer->signed_document(document), options);
}

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
  /// Freezes, advances and releases, as a maintenance does.
  void advance(std::int64_t epoch, const std::string& hold = std::string(32, '4')) {
    sde::WriteFence table = fence();
    (void)table.freeze(hold);
    (void)table.advance(epoch);
    (void)table.release(hold);
  }
  sde::Value stored_epoch(std::int64_t id) {
    return engine_->get(kTable, {{"id", id}})->at(kEpoch);
  }

  std::string dsn_;
  std::unique_ptr<Scope> scope_;
  std::string application_;
  std::unique_ptr<sde::PostgresEngine> engine_;
};

TEST_F(PostgresFence, NativeConstraintsRefuseUnstampedOldAndFutureWriters) {
  const std::string hold(32, '2');
  sde::WriteFence table = fence();
  EXPECT_EQ(table.prepare(1).epoch(), 1);
  EXPECT_THROW(insert(1), sde::EngineError);
  insert(2, 1);
  EXPECT_THROW(insert(3, 2), sde::EngineError);
  EXPECT_TRUE(table.freeze(hold).closed());
  EXPECT_THROW(insert(4, 1), sde::EngineError);
  EXPECT_TRUE(table.advance(2).closed());
  (void)table.release(hold);
  EXPECT_THROW(insert(5, 1), sde::EngineError);
  insert(6, 2);
  EXPECT_TRUE(engine_->get(kTable, {{"id", std::int64_t{2}}}).has_value());
  EXPECT_TRUE(engine_->get(kTable, {{"id", std::int64_t{6}}}).has_value());
  EXPECT_EQ(engine_->count(kTable), 2U);
}

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

TEST_F(PostgresFence, AnOpenSessionNeverInheritsANewSessionsGeneration) {
  const sde::Model model = records();
  const sde::PlacementMap first = generation_map(model, kTable);
  sde::prepare_schema(model, first, {{"db", engine_.get()}}, kProject);
  sde::SessionOptions options;
  options.project_id = kProject;
  sde::Session old(model, first, {{"db", engine_.get()}}, options);
  old.save("Record", {{"id", std::int64_t{1}}});
  EXPECT_EQ(old.get("Record", {{"id", std::int64_t{1}}}), (sde::Row{{"id", std::int64_t{1}}}));
  EXPECT_EQ(stored_epoch(1), sde::Value(std::int64_t{1}));
  advance(2);
  const sde::PlacementMap second = generation_map(model, kTable, 2, 2);
  sde::Session current(model, second, {{"db", engine_.get()}}, options);
  EXPECT_THROW(old.save("Record", {{"id", std::int64_t{2}}}), sde::EngineError);
  current.save("Record", {{"id", std::int64_t{3}}});
  EXPECT_EQ(current.get("Record", {{"id", std::int64_t{3}}}), (sde::Row{{"id", std::int64_t{3}}}));
  EXPECT_EQ(current.get("Record", {{"id", std::int64_t{1}}}), (sde::Row{{"id", std::int64_t{1}}}));
  EXPECT_EQ(stored_epoch(3), sde::Value(std::int64_t{2}));
  EXPECT_EQ(engine_->count(kTable), 2U);
}

TEST_F(PostgresFence, AFutureMapIsRefusedBeforeItMovesTheWatermark) {
  const sde::testing_support::Signer signer("the PostgreSQL fence tests");
  const sde::Model model = records();
  const sde::PlacementMap first = generation_map(model, kTable, 1, 1, &signer);
  sde::prepare_schema(model, first, {{"db", engine_.get()}}, kProject);
  sde::SessionOptions options;
  options.project_id = kProject;
  { const sde::Session opened(model, first, {{"db", engine_.get()}}, options); }
  EXPECT_EQ(engine_->map_watermark(), 1);
  const sde::PlacementMap future = generation_map(model, kTable, 2, 2, &signer);
  EXPECT_TRUE(contains(message_of([&] {
                         const sde::Session opened(model, future, {{"db", engine_.get()}}, options);
                       }),
                       "write generation"));
  EXPECT_EQ(engine_->map_watermark(), 1);
  sde::SessionOptions elsewhere;
  elsewhere.project_id = std::string(32, '9');
  EXPECT_TRUE(contains(message_of([&] {
                         const sde::Session opened(model, first, {{"db", engine_.get()}}, elsewhere);
                       }),
                       "locally configured"));
  EXPECT_EQ(engine_->map_watermark(), 1);
}

/// The adapter with schema creation forbidden: a running session checks and never provisions.
class NoDdl final : public sde::Engine {
 public:
  explicit NoDdl(sde::PostgresEngine& inner) : inner_(inner) {}
  [[nodiscard]] std::string_view dialect() const noexcept override { return inner_.dialect(); }
  std::vector<sde::PhysicalFinding> ensure_schema(const sde::PhysicalLayout&,
                                                  const sde::Keys&) override {
    ADD_FAILURE() << "the runtime tried to provision the schema";
    throw std::logic_error("no DDL at run time");
  }
  void insert(const std::string& table, const sde::Row& values) override {
    inner_.insert(table, values);
  }
  std::optional<sde::Row> get(const std::string& table, const sde::Row& key) override {
    return inner_.get(table, key);
  }
  void transaction(const std::function<void()>& body) override { inner_.transaction(body); }
  [[nodiscard]] sde::Capabilities capabilities() noexcept override { return inner_.capabilities(); }

 private:
  sde::PostgresEngine& inner_;
};

TEST_F(PostgresFence, TheRuntimesSchemaCheckNeitherProvisionsNorOverridesAGeneration) {
  // The reference also refuses a map copied after loading; in C++ only the loader makes one, and a
  // copy carries the loader's fingerprint, so there is no unloaded map to refuse.
  const sde::Model model = records();
  const sde::PlacementMap map = generation_map(model, kTable);
  sde::prepare_schema(model, map, {{"db", engine_.get()}}, kProject);
  NoDdl runtime(*engine_);
  sde::SessionOptions options;
  options.project_id = kProject;
  sde::Session session(model, map, {{"db", &runtime}}, options);
  session.ensure_schema();
  EXPECT_TRUE(contains(message_of([&] {
                         session.save("Record", {{"id", std::int64_t{1}}, {kEpoch, std::int64_t{1}}});
                       }),
                       "reserved"));
  EXPECT_EQ(engine_->count(kTable), 0U);
}

TEST_F(PostgresFence, AGenerationIsNoSubstituteForTheModelsColumns) {
  const sde::Model model = records(true);
  const sde::PlacementMap map = generation_map(model, kTable, 1, 1, nullptr, true);
  Admin admin(scope_->dsn());
  (void)admin.run("ALTER TABLE " + kQuoted + " ADD COLUMN label text");
  sde::prepare_schema(model, map, {{"db", engine_.get()}}, kProject);
  (void)admin.run("ALTER TABLE " + kQuoted + " RENAME COLUMN label TO other_label");
  sde::SessionOptions options;
  options.project_id = kProject;
  const std::string refused = message_of([&] {
    const sde::Session opened(model, map, {{"db", engine_.get()}}, options);
  });
  EXPECT_TRUE(contains(refused, "different shape")) << refused;
}

TEST_F(PostgresFence, AnOldSessionsBatchCannotBorrowTheNewGeneration) {
  const sde::Model model = records();
  const sde::PlacementMap first = generation_map(model, kTable);
  sde::prepare_schema(model, first, {{"db", engine_.get()}}, kProject);
  sde::SessionOptions options;
  options.project_id = kProject;
  sde::Session old(model, first, {{"db", engine_.get()}}, options);
  old.save_many("Record", {{{"id", std::int64_t{1}}}, {{"id", std::int64_t{2}}}});
  advance(2);
  const sde::PlacementMap second = generation_map(model, kTable, 2, 2);
  sde::Session current(model, second, {{"db", engine_.get()}}, options);
  EXPECT_THROW(old.save_many("Record", {{{"id", std::int64_t{3}}}, {{"id", std::int64_t{4}}}}),
               sde::EngineError);
  current.save_many("Record", {{{"id", std::int64_t{5}}}, {{"id", std::int64_t{6}}}});
  EXPECT_EQ(engine_->count(kTable), 4U);
  EXPECT_EQ(stored_epoch(5), sde::Value(std::int64_t{2}));
}

// --- a copy between two schemas -----------------------------------------------------------------

/// The reference's generation-migration fixture between two PostgreSQL schemas: `Event` in
/// `source_events` on engine `a` and its copy in `copy_events` on engine `b`, writes fanned out.
class PostgresGenerationCopy : public ::testing::Test {
 protected:
  void SetUp() override {
    SDE_REQUIRE_DSN("SDE_POSTGRES_DSN", dsn_);
    source_scope_ = std::make_unique<Scope>(dsn_);
    copy_scope_ = std::make_unique<Scope>(dsn_);
    source_ = std::make_unique<sde::PostgresEngine>(source_scope_->dsn());
    source_->connect();
    copy_ = std::make_unique<sde::PostgresEngine>(copy_scope_->dsn());
    copy_->connect();
  }

  sde::PlacementMap events_map(int epoch) const {
    const auto material = [](const std::string& id, const std::string& engine) {
      return R"({"id": ")" + id + R"(", "engine": ")" + engine +
             R"(", "layout": {"tables": {"Event": ")" + id +
             R"(_events"}, "columns": {"Event": {"id": "bigint", "value": "integer"}}}})";
    };
    std::string copy = material("copy", "b");
    copy.insert(copy.size() - 1, R"(, "lag_budget_ms": 60000)");
    sde::LoadOptions options;
    options.model = &model_;
    return sde::load_map(R"({"contract": 4, "project_id": ")" + kProject +
                             R"(", "model_version": ")" + model_.version() +
                             R"(", "map_version": )" + std::to_string(epoch) +
                             R"(, "groups": {"Event": {"write_epoch": )" + std::to_string(epoch) +
                             R"(, "source": )" + material("source", "a") + R"(, "derived": [)" +
                             copy + R"(], "also_write": ["copy"]}}})",
                         options);
  }

  std::map<std::string, sde::Engine*> engines() { return {{"a", source_.get()}, {"b", copy_.get()}}; }
  sde::PostgresEngine& engine(const std::string& name) { return name == "a" ? *source_ : *copy_; }

  /// Every table of the group frozen, advanced and released, as a maintenance does.
  void advance(const sde::PlacementMap& map, std::int64_t epoch) {
    for (const sde::Materialization* material : map.placement_of("Event").all()) {
      sde::WriteFence table =
          engine(material->engine).write_fence(material->layout.table_for("Event"), kProject);
      (void)table.freeze(std::string(32, '5'));
      (void)table.advance(epoch);
      (void)table.release(std::string(32, '5'));
    }
  }

  sde::SessionOptions options() const {
    sde::SessionOptions out;
    out.project_id = kProject;
    return out;
  }

  std::string dsn_;
  std::unique_ptr<Scope> source_scope_;
  std::unique_ptr<Scope> copy_scope_;
  std::unique_ptr<sde::PostgresEngine> source_;
  std::unique_ptr<sde::PostgresEngine> copy_;
  sde::Model model_ = sde::load_neutral_model(R"({"entities": [{"name": "Event", "fields": [
      {"name": "id", "type": "int64"}, {"name": "value", "type": "int32"}], "key": ["id"]}]})");
};

TEST_F(PostgresGenerationCopy, ACopyIsStampedWithTheGenerationInForceAndHistoryIsNotCompared) {
  const sde::PlacementMap first = events_map(1);
  sde::prepare_schema(model_, first, engines(), kProject);
  source_->insert("source_events", {{"id", std::int64_t{1}}, {"value", std::int64_t{11}},
                                    {kEpoch, std::int64_t{1}}});
  advance(first, 2);
  const sde::PlacementMap current = events_map(2);
  sde::Session session(model_, current, engines(), options());
  (void)sde::backfill(session, "Event");
  EXPECT_EQ(copy_->get("copy_events", {{"id", std::int64_t{1}}})->at(kEpoch),
            sde::Value(std::int64_t{2}));
  EXPECT_EQ(source_->get("source_events", {{"id", std::int64_t{1}}})->at(kEpoch),
            sde::Value(std::int64_t{1}));
  EXPECT_TRUE(sde::verify(session, "Event").matched());
  session.save("Event", {{"id", std::int64_t{2}}, {"value", std::int64_t{22}}});
  EXPECT_EQ(copy_->get("copy_events", {{"id", std::int64_t{2}}})->at(kEpoch),
            sde::Value(std::int64_t{2}));
  EXPECT_EQ(session.get("Event", {{"id", std::int64_t{2}}}),
            (sde::Row{{"id", std::int64_t{2}}, {"value", std::int64_t{22}}}));
}

/// The source adapter with something happening right after each transaction commits.
class AfterCommit final : public sde::Engine {
 public:
  AfterCommit(sde::PostgresEngine& inner, std::function<void()> then)
      : inner_(inner), then_(std::move(then)) {}
  [[nodiscard]] std::string_view dialect() const noexcept override { return inner_.dialect(); }
  std::vector<sde::PhysicalFinding> ensure_schema(const sde::PhysicalLayout& layout,
                                                  const sde::Keys& keys) override {
    return inner_.ensure_schema(layout, keys);
  }
  void insert(const std::string& table, const sde::Row& values) override {
    inner_.insert(table, values);
  }
  std::optional<sde::Row> get(const std::string& table, const sde::Row& key) override {
    return inner_.get(table, key);
  }
  void transaction(const std::function<void()>& body) override {
    inner_.transaction(body);
    then_();
  }
  [[nodiscard]] sde::Capabilities capabilities() noexcept override { return inner_.capabilities(); }

 private:
  sde::PostgresEngine& inner_;
  std::function<void()> then_;
};

TEST_F(PostgresGenerationCopy, ADeferredCopyKeepsTheOldGenerationAfterCommitAndANewSession) {
  const sde::PlacementMap first = events_map(1);
  sde::prepare_schema(model_, first, engines(), kProject);
  const sde::PlacementMap current = events_map(2);
  AfterCommit source(*source_, [&] {
    // Between the source's commit and the copy's deferred write: a maintenance moves both tables
    // to generation 2, a new session opens on it, and the copy gets a row of its own.
    advance(first, 2);
    const sde::Session opened(model_, current, engines(), options());
    copy_->insert("copy_events", {{"id", std::int64_t{1}}, {"value", std::int64_t{99}},
                                  {kEpoch, std::int64_t{2}}});
  });
  sde::Session old(model_, first, {{"a", &source}, {"b", copy_.get()}}, options());
  old.transaction({"Event"}, [&] {
    old.save("Event", {{"id", std::int64_t{1}}, {"value", std::int64_t{11}}});
  });
  EXPECT_EQ(source_->get("source_events", {{"id", std::int64_t{1}}})->at("value"),
            sde::Value(std::int64_t{11}));
  EXPECT_EQ(copy_->get("copy_events", {{"id", std::int64_t{1}}})->at("value"),
            sde::Value(std::int64_t{99}))
      << "the old session's deferred write reached the copy under the new generation";
}

// --- the comparison under named barriers --------------------------------------------------------

/// The copy's adapter, with something happening after its first keyset read: a comparison in
/// progress.
class DuringComparison final : public sde::Engine, public sde::Migratable {
 public:
  DuringComparison(sde::PostgresEngine& inner, std::function<void()> then)
      : inner_(inner), then_(std::move(then)) {}
  [[nodiscard]] std::string_view dialect() const noexcept override { return inner_.dialect(); }
  std::vector<sde::PhysicalFinding> ensure_schema(const sde::PhysicalLayout& layout,
                                                  const sde::Keys& keys) override {
    return inner_.ensure_schema(layout, keys);
  }
  void insert(const std::string& table, const sde::Row& values) override {
    inner_.insert(table, values);
  }
  std::optional<sde::Row> get(const std::string& table, const sde::Row& key) override {
    return inner_.get(table, key);
  }
  void transaction(const std::function<void()>& body) override { inner_.transaction(body); }
  [[nodiscard]] sde::Capabilities capabilities() noexcept override {
    sde::Capabilities offered = inner_.capabilities();
    offered.migration = this;
    return offered;
  }
  std::vector<sde::Row> key_range(const std::string& table, const std::vector<std::string>& order,
                                  const std::optional<std::vector<sde::Value>>& after,
                                  const std::optional<std::vector<sde::Value>>& upto,
                                  std::optional<std::size_t> limit) override {
    std::vector<sde::Row> rows = inner_.key_range(table, order, after, upto, limit);
    if (then_) std::exchange(then_, nullptr)();
    return rows;
  }
  std::optional<std::vector<sde::Value>> nth_key(const std::string& table,
                                                 const std::vector<std::string>& order,
                                                 std::int64_t position) override {
    return inner_.nth_key(table, order, position);
  }
  void copy_in(const std::string& table, const std::vector<sde::Row>& rows) override {
    inner_.copy_in(table, rows);
  }
  std::uint64_t count(const std::string& table) override { return inner_.count(table); }
  std::int64_t backfill_marker(const std::string& materialization,
                               const std::string& entity) override {
    return inner_.backfill_marker(materialization, entity);
  }
  void record_backfill_marker(const std::string& materialization, const std::string& entity,
                              std::int64_t rows) override {
    inner_.record_backfill_marker(materialization, entity, rows);
  }

 private:
  sde::PostgresEngine& inner_;
  std::function<void()> then_;
};

class PostgresFrozen : public PostgresGenerationCopy {
 protected:
  const std::string kHold = std::string(32, '6');

  void SetUp() override {
    PostgresGenerationCopy::SetUp();
    if (IsSkipped() || HasFatalFailure()) return;
    map_ = std::make_unique<sde::PlacementMap>(events_map(1));
    sde::prepare_schema(model_, *map_, engines(), kProject);
    session_ = std::make_unique<sde::Session>(model_, *map_, engines(), options());
    session_->save("Event", {{"id", std::int64_t{1}}, {"value", std::int64_t{11}}});
    request_ = std::make_unique<sde::VerificationRequest>(sde::verification_request(
        *map_, "Event", kProject, std::string(32, '7'), "2026-09-12T12:00:00Z"));
  }

  sde::FrozenVerifyReport frozen(const std::map<std::string, std::int64_t>& epochs,
                                 std::map<std::string, sde::Engine*> through = {}) {
    if (through.empty()) through = engines();
    const sde::InspectionContext context(model_, *map_, std::move(through), kProject);
    return sde::verify_frozen(context, "Event", *request_, kHold, epochs);
  }

  std::vector<std::string> holds(const std::string& engine_name, const std::string& table) {
    return engine(engine_name).write_fence(table, kProject).state().holds;
  }

  std::unique_ptr<sde::PlacementMap> map_;
  std::unique_ptr<sde::Session> session_;
  std::unique_ptr<sde::VerificationRequest> request_;
};

TEST_F(PostgresFrozen, ARowOnlyTheCopyHoldsCannotPassTheFrozenGate) {
  copy_->insert("copy_events", {{"id", std::int64_t{99}}, {"value", std::int64_t{99}},
                                {kEpoch, std::int64_t{1}}});
  EXPECT_TRUE(sde::verify(*session_, "Event").matched())
      << "the live comparison checks that the copy contains the source, and it does";
  const sde::FrozenVerifyReport report = frozen({{"source", 1}, {"copy", 1}});
  EXPECT_FALSE(report.matched());
  EXPECT_EQ(report.comparison.rows_source, 1);
  EXPECT_EQ(report.comparison.rows_target, 2);
  EXPECT_EQ(report.barriers.size(), 2U);
  const sde::Json record = report.as_record();
  EXPECT_EQ(sde::dump_json(*record.find("comparison")->find("request")),
            sde::dump_json(request_->as_record()));
  EXPECT_EQ(record.find("comparison")->find("differences"), nullptr);
  for (const auto& [name, table] : std::vector<std::pair<std::string, std::string>>{
           {"a", "source_events"}, {"b", "copy_events"}}) {
    const std::vector<std::string> held = holds(name, table);
    EXPECT_NE(std::find(held.begin(), held.end(), kHold), held.end()) << table;
    EXPECT_THROW(engine(name).insert(table, {{"id", std::int64_t{2}}, {"value", std::int64_t{22}},
                                             {kEpoch, std::int64_t{1}}}),
                 sde::EngineError)
        << table;
  }
}

TEST_F(PostgresFrozen, AnOperatorComparesDifferentGenerationsWithoutAdoptingAMap) {
  sde::WriteFence target = copy_->write_fence("copy_events", kProject);
  (void)target.freeze(std::string(32, '8'));
  (void)target.advance(2);
  (void)target.release(std::string(32, '8'));
  // The copy's row written again at generation 2: ClickHouse collapses the second write, and in
  // PostgreSQL the first goes before it.
  Admin(copy_scope_->dsn()).run("DELETE FROM copy_events WHERE id = 1");
  copy_->insert("copy_events", {{"id", std::int64_t{1}}, {"value", std::int64_t{11}},
                                {kEpoch, std::int64_t{2}}});
  EXPECT_TRUE(contains(message_of([&] {
                         const sde::Session opened(model_, *map_, engines(), options());
                       }),
                       "write generation"));
  const sde::FrozenVerifyReport report = frozen({{"source", 1}, {"copy", 2}});
  EXPECT_TRUE(report.matched());
  std::map<std::string, std::int64_t> epochs;
  for (const sde::FrozenTable& barrier : report.barriers) epochs[barrier.materialization] = barrier.epoch;
  EXPECT_EQ(epochs, (std::map<std::string, std::int64_t>{{"source", 1}, {"copy", 2}}));
  EXPECT_FALSE(source_->map_watermark().has_value());
  EXPECT_FALSE(copy_->map_watermark().has_value());
}

TEST_F(PostgresFrozen, ReleasingABarrierDuringTheComparisonInvalidatesIt) {
  DuringComparison copy(*copy_, [&] {
    (void)source_->write_fence("source_events", kProject).release(kHold);
  });
  const std::string refused =
      message_of([&] { (void)frozen({{"source", 1}, {"copy", 1}}, {{"a", source_.get()}, {"b", &copy}}); });
  EXPECT_TRUE(contains(refused, "lost its named barrier")) << refused;
}

TEST_F(PostgresFrozen, AWrongGenerationIsRefusedBeforeAnyTableIsClosed) {
  const std::string refused = message_of([&] { (void)frozen({{"source", 1}, {"copy", 2}}); });
  EXPECT_TRUE(contains(refused, "expected write generation")) << refused;
  EXPECT_TRUE(holds("a", "source_events").empty());
  EXPECT_TRUE(holds("b", "copy_events").empty());
}

TEST_F(PostgresFrozen, EqualCountsCannotHideAnotherValueInTheCopy) {
  Admin(copy_scope_->dsn()).run("DELETE FROM copy_events WHERE id = 1");
  copy_->insert("copy_events", {{"id", std::int64_t{1}}, {"value", std::int64_t{99}},
                                {kEpoch, std::int64_t{1}}});
  const sde::FrozenVerifyReport report = frozen({{"source", 1}, {"copy", 1}});
  EXPECT_EQ(report.comparison.rows_source, 1);
  EXPECT_EQ(report.comparison.rows_target, 1);
  EXPECT_FALSE(report.comparison.matched());
  EXPECT_FALSE(report.matched());
}

TEST_F(PostgresFrozen, ARetiredBarrierIdIsRefusedBeforeAnotherTableIsClosed) {
  sde::WriteFence source = source_->write_fence("source_events", kProject);
  (void)source.freeze(kHold);
  (void)source.release(kHold);
  const std::string refused = message_of([&] { (void)frozen({{"source", 1}, {"copy", 1}}); });
  EXPECT_TRUE(contains(refused, "retired barrier id")) << refused;
  EXPECT_TRUE(source.state().holds.empty());
  EXPECT_TRUE(holds("b", "copy_events").empty());
}

}  // namespace
