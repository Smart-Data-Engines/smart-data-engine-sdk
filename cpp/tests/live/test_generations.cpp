/// Write generations against real engines, one test body for each, as the reference parametrises
/// them: native constraints that refuse an unstamped, an old and a future writer; sessions that keep
/// their own generation on a shared adapter; a runtime that checks and never provisions; a copy
/// stamped with the generation in force; and the comparison under named barriers, which closes the
/// native boundary rather than inferring it from counts. Ported from the reference's
/// `test_write_fence_live.py`, `test_generation_session_live.py`,
/// `test_generation_migration_live.py`, `test_frozen_verification_live.py` and the generation case
/// of `test_bulk_live.py`. The copies run in every direction the built adapters allow, the
/// reference's PostgreSQL-to-ClickHouse among them.
///
///     export SDE_POSTGRES_DSN=postgresql://postgres:sde@127.0.0.1:55432/sde
///     export SDE_CLICKHOUSE_DSN=clickhouse://default:sde@127.0.0.1:58123/sde
///     ctest -L live

#include <algorithm>
#include <cstdint>
#include <functional>
#include <map>
#include <memory>
#include <optional>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

#include <gtest/gtest.h>

#include "live/live.hpp"
#include "live/sides.hpp"
#include "sde/errors.hpp"
#include "sde/json.hpp"
#include "sde/layout.hpp"
#include "sde/migration.hpp"
#include "sde/model.hpp"
#include "sde/placement.hpp"
#include "sde/provisioning.hpp"
#include "sde/schema.hpp"
#include "sde/session.hpp"
#include "sde/verification.hpp"
#include "sde/write_fence.hpp"
#include "support/signer.hpp"

namespace {

using sde::live::Direction;
using sde::live::Side;
using sde::live::side_of;

const std::string kProject(32, '1');
const std::string kEpoch(sde::EPOCH_COLUMN);

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

sde::PlacementMap generation_map(const sde::Model& model, const std::string& dialect,
                                 const std::string& table, int epoch = 1, int version = 1,
                                 const sde::testing_support::Signer* signer = nullptr,
                                 bool with_label = false) {
  sde::Json columns = sde::Json::object();
  columns.set("id", sde::column_type("int64", dialect));
  if (with_label) columns.set("label", sde::column_type("string", dialect));
  sde::Json tables = sde::Json::object();
  tables.set("Record", table);
  sde::Json typed = sde::Json::object();
  typed.set("Record", std::move(columns));
  sde::Json layout = sde::Json::object();
  layout.set("tables", std::move(tables));
  layout.set("columns", std::move(typed));
  sde::Json source = sde::Json::object();
  source.set("id", "source");
  source.set("engine", "db");
  source.set("layout", std::move(layout));
  sde::Json record = sde::Json::object();
  record.set("write_epoch", std::int64_t{epoch});
  record.set("source", std::move(source));
  sde::Json groups = sde::Json::object();
  groups.set("Record", std::move(record));
  sde::Json document = sde::parse_json(R"({"contract": 4, "project_id": ")" + kProject +
                                       R"(", "model_version": ")" + model.version() +
                                       R"(", "map_version": )" + std::to_string(version) + "}");
  document.set("groups", std::move(groups));
  sde::LoadOptions options;
  options.model = &model;
  if (signer == nullptr) return sde::load_map(document, options);
  options.public_keys = signer->keys();
  return sde::load_map(signer->signed_document(document), options);
}

/// A table name each engine has to quote with care: a double quote inside PostgreSQL's, a backtick
/// inside ClickHouse's.
std::string guarded_table(const std::string& dialect) {
  return dialect == "postgres" ? "event \"quoted" : "event `quoted";
}

/// One fenced table in a namespace of its own, and the provisioning login's adapter on it - the
/// reference's `guarded` fixture.
class Guarded : public ::testing::TestWithParam<std::string> {
 protected:
  void SetUp() override {
    side_ = side_of(GetParam());
    SDE_REQUIRE_SIDE(side_, GetParam());
    table_ = guarded_table(GetParam());
    side_->run(GetParam() == "postgres"
                   ? "CREATE TABLE " + quoted() + " (id bigint PRIMARY KEY)"
                   : "CREATE TABLE " + quoted() + " (id Int64) ENGINE = ReplacingMergeTree ORDER BY id");
  }

  [[nodiscard]] std::string quoted() const { return sde::quote_identifier(GetParam(), table_); }
  sde::Engine& engine() { return side_->engine(); }
  sde::WriteFence fence() { return engine().capabilities().fences->write_fence(table_, kProject); }
  void insert(std::int64_t id, std::optional<std::int64_t> epoch = std::nullopt) {
    sde::Row row{{"id", id}};
    if (epoch) row[kEpoch] = *epoch;
    engine().insert(table_, row);
  }
  /// Freezes, advances and releases, as a maintenance does.
  void advance(std::int64_t epoch, const std::string& hold = std::string(32, '4')) {
    sde::WriteFence table = fence();
    (void)table.freeze(hold);
    (void)table.advance(epoch);
    (void)table.release(hold);
  }
  sde::Value stored_epoch(std::int64_t id) { return engine().get(table_, {{"id", id}})->at(kEpoch); }
  std::uint64_t count() { return side_->migration().count(table_); }
  sde::SessionOptions options() const {
    sde::SessionOptions out;
    out.project_id = kProject;
    return out;
  }

  std::unique_ptr<Side> side_;
  std::string table_;
};

TEST_P(Guarded, NativeConstraintsRefuseUnstampedOldAndFutureWriters) {
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
  EXPECT_TRUE(engine().get(table_, {{"id", std::int64_t{2}}}).has_value());
  EXPECT_TRUE(engine().get(table_, {{"id", std::int64_t{6}}}).has_value());
  EXPECT_EQ(count(), 2U);
}

TEST_P(Guarded, AnOpenSessionNeverInheritsANewSessionsGeneration) {
  const sde::Model model = records();
  const sde::PlacementMap first = generation_map(model, GetParam(), table_);
  sde::prepare_schema(model, first, {{"db", &engine()}}, kProject);
  sde::Session old(model, first, {{"db", &engine()}}, options());
  old.save("Record", {{"id", std::int64_t{1}}});
  EXPECT_EQ(old.get("Record", {{"id", std::int64_t{1}}}), (sde::Row{{"id", std::int64_t{1}}}));
  EXPECT_EQ(stored_epoch(1), sde::Value(std::int64_t{1}));
  advance(2);
  const sde::PlacementMap second = generation_map(model, GetParam(), table_, 2, 2);
  sde::Session current(model, second, {{"db", &engine()}}, options());
  EXPECT_THROW(old.save("Record", {{"id", std::int64_t{2}}}), sde::EngineError);
  current.save("Record", {{"id", std::int64_t{3}}});
  EXPECT_EQ(current.get("Record", {{"id", std::int64_t{3}}}), (sde::Row{{"id", std::int64_t{3}}}));
  EXPECT_EQ(current.get("Record", {{"id", std::int64_t{1}}}), (sde::Row{{"id", std::int64_t{1}}}));
  EXPECT_EQ(stored_epoch(3), sde::Value(std::int64_t{2}));
  EXPECT_EQ(count(), 2U);
}

TEST_P(Guarded, AFutureMapIsRefusedBeforeItMovesTheWatermark) {
  const sde::testing_support::Signer signer("the generation tests");
  const sde::Model model = records();
  const sde::PlacementMap first = generation_map(model, GetParam(), table_, 1, 1, &signer);
  sde::prepare_schema(model, first, {{"db", &engine()}}, kProject);
  { const sde::Session opened(model, first, {{"db", &engine()}}, options()); }
  sde::WatermarkStore& store = *engine().capabilities().watermark;
  EXPECT_EQ(store.map_watermark(), 1);
  const sde::PlacementMap future = generation_map(model, GetParam(), table_, 2, 2, &signer);
  EXPECT_TRUE(contains(message_of([&] {
                         const sde::Session opened(model, future, {{"db", &engine()}}, options());
                       }),
                       "write generation"));
  EXPECT_EQ(store.map_watermark(), 1);
  sde::SessionOptions elsewhere;
  elsewhere.project_id = std::string(32, '9');
  EXPECT_TRUE(contains(message_of([&] {
                         const sde::Session opened(model, first, {{"db", &engine()}}, elsewhere);
                       }),
                       "locally configured"));
  EXPECT_EQ(store.map_watermark(), 1);
}

/// The adapter with schema creation forbidden: a running session checks and never provisions.
class NoDdl final : public sde::Engine {
 public:
  explicit NoDdl(sde::Engine& inner) : inner_(inner) {}
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
  sde::Engine& inner_;
};

TEST_P(Guarded, TheRuntimesSchemaCheckNeitherProvisionsNorOverridesAGeneration) {
  // The reference also refuses a map copied after loading; in C++ only the loader makes one, and a
  // copy carries the loader's fingerprint, so there is no unloaded map to refuse.
  const sde::Model model = records();
  const sde::PlacementMap map = generation_map(model, GetParam(), table_);
  sde::prepare_schema(model, map, {{"db", &engine()}}, kProject);
  NoDdl runtime(engine());
  sde::Session session(model, map, {{"db", &runtime}}, options());
  session.ensure_schema();
  EXPECT_TRUE(contains(message_of([&] {
                         session.save("Record", {{"id", std::int64_t{1}}, {kEpoch, std::int64_t{1}}});
                       }),
                       "reserved"));
  EXPECT_EQ(count(), 0U);
}

TEST_P(Guarded, AGenerationIsNoSubstituteForTheModelsColumns) {
  const sde::Model model = records(true);
  const sde::PlacementMap map = generation_map(model, GetParam(), table_, 1, 1, nullptr, true);
  side_->run("ALTER TABLE " + quoted() + " ADD COLUMN label " +
             sde::column_type("string", GetParam()));
  sde::prepare_schema(model, map, {{"db", &engine()}}, kProject);
  side_->run("ALTER TABLE " + quoted() + " RENAME COLUMN label TO other_label");
  const std::string refused = message_of([&] {
    const sde::Session opened(model, map, {{"db", &engine()}}, options());
  });
  EXPECT_TRUE(contains(refused, "different shape")) << refused;
}

TEST_P(Guarded, AnOldSessionsBatchCannotBorrowTheNewGeneration) {
  const sde::Model model = records();
  const sde::PlacementMap first = generation_map(model, GetParam(), table_);
  sde::prepare_schema(model, first, {{"db", &engine()}}, kProject);
  sde::Session old(model, first, {{"db", &engine()}}, options());
  old.save_many("Record", {{{"id", std::int64_t{1}}}, {{"id", std::int64_t{2}}}});
  advance(2);
  const sde::PlacementMap second = generation_map(model, GetParam(), table_, 2, 2);
  sde::Session current(model, second, {{"db", &engine()}}, options());
  EXPECT_THROW(old.save_many("Record", {{{"id", std::int64_t{3}}}, {{"id", std::int64_t{4}}}}),
               sde::EngineError);
  current.save_many("Record", {{{"id", std::int64_t{5}}}, {{"id", std::int64_t{6}}}});
  EXPECT_EQ(count(), 4U);
  EXPECT_EQ(stored_epoch(5), sde::Value(std::int64_t{2}));
}

INSTANTIATE_TEST_SUITE_P(Engines, Guarded, ::testing::ValuesIn(sde::live::dialects()),
                         sde::live::dialect_name);

// --- a copy with a generation -------------------------------------------------------------------

/// The reference's generation-migration fixture: `Event` in `source_events` on engine `a` and its
/// copy in `copy_events` on engine `b`, writes fanned out, in the direction of the parameter.
class GenerationCopies : public ::testing::TestWithParam<Direction> {
 protected:
  void SetUp() override {
    source_ = side_of(GetParam().source);
    SDE_REQUIRE_SIDE(source_, GetParam().source);
    copy_ = side_of(GetParam().target);
    SDE_REQUIRE_SIDE(copy_, GetParam().target);
  }

  sde::PlacementMap events_map(int epoch) const {
    const auto material = [](const std::string& id, const std::string& engine,
                             const std::string& dialect) {
      return R"({"id": ")" + id + R"(", "engine": ")" + engine +
             R"(", "layout": {"tables": {"Event": ")" + id +
             R"(_events"}, "columns": {"Event": {"id": ")" + sde::column_type("int64", dialect) +
             R"(", "value": ")" + sde::column_type("int32", dialect) + R"("}}}})";
    };
    std::string copy = material("copy", "b", GetParam().target);
    copy.insert(copy.size() - 1, R"(, "lag_budget_ms": 60000)");
    sde::LoadOptions options;
    options.model = &model_;
    return sde::load_map(R"({"contract": 4, "project_id": ")" + kProject +
                             R"(", "model_version": ")" + model_.version() +
                             R"(", "map_version": )" + std::to_string(epoch) +
                             R"(, "groups": {"Event": {"write_epoch": )" + std::to_string(epoch) +
                             R"(, "source": )" + material("source", "a", GetParam().source) +
                             R"(, "derived": [)" + copy + R"(], "also_write": ["copy"]}}})",
                         options);
  }

  std::map<std::string, sde::Engine*> engines() {
    return {{"a", &source_->engine()}, {"b", &copy_->engine()}};
  }
  Side& side(const std::string& name) { return name == "a" ? *source_ : *copy_; }
  sde::WriteFence fence_of(const std::string& name, const std::string& table) {
    return side(name).engine().capabilities().fences->write_fence(table, kProject);
  }

  /// Every table of the group frozen, advanced and released, as a maintenance does.
  void advance(const sde::PlacementMap& map, std::int64_t epoch) {
    for (const sde::Materialization* material : map.placement_of("Event").all()) {
      sde::WriteFence table = fence_of(material->engine, material->layout.table_for("Event"));
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

  std::unique_ptr<Side> source_;
  std::unique_ptr<Side> copy_;
  sde::Model model_ = sde::load_neutral_model(R"({"entities": [{"name": "Event", "fields": [
      {"name": "id", "type": "int64"}, {"name": "value", "type": "int32"}], "key": ["id"]}]})");
};

TEST_P(GenerationCopies, ACopyIsStampedWithTheGenerationInForceAndHistoryIsNotCompared) {
  const sde::PlacementMap first = events_map(1);
  sde::prepare_schema(model_, first, engines(), kProject);
  source_->engine().insert("source_events", {{"id", std::int64_t{1}}, {"value", std::int64_t{11}},
                                             {kEpoch, std::int64_t{1}}});
  advance(first, 2);
  const sde::PlacementMap current = events_map(2);
  sde::Session session(model_, current, engines(), options());
  (void)sde::backfill(session, "Event");
  EXPECT_EQ(copy_->engine().get("copy_events", {{"id", std::int64_t{1}}})->at(kEpoch),
            sde::Value(std::int64_t{2}));
  EXPECT_EQ(source_->engine().get("source_events", {{"id", std::int64_t{1}}})->at(kEpoch),
            sde::Value(std::int64_t{1}));
  EXPECT_TRUE(sde::verify(session, "Event").matched());
  session.save("Event", {{"id", std::int64_t{2}}, {"value", std::int64_t{22}}});
  EXPECT_EQ(copy_->engine().get("copy_events", {{"id", std::int64_t{2}}})->at(kEpoch),
            sde::Value(std::int64_t{2}));
  EXPECT_EQ(session.get("Event", {{"id", std::int64_t{2}}}),
            (sde::Row{{"id", std::int64_t{2}}, {"value", std::int64_t{22}}}));
}

std::vector<Direction> with_two_adapters() {
  std::vector<Direction> out;
  for (const Direction& direction : sde::live::directions()) {
    if (direction.two) out.push_back(direction);
  }
  return out;
}

INSTANTIATE_TEST_SUITE_P(Directions, GenerationCopies, ::testing::ValuesIn(with_two_adapters()),
                         sde::live::direction_name);

/// The source adapter with something happening right after each transaction commits.
class AfterCommit final : public sde::Engine {
 public:
  AfterCommit(sde::Engine& inner, std::function<void()> then) : inner_(inner), then_(std::move(then)) {}
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
  sde::Engine& inner_;
  std::function<void()> then_;
};

/// A copy whose source has transactions: PostgreSQL's, to either engine.
class DeferredCopies : public GenerationCopies {};

TEST_P(DeferredCopies, ADeferredCopyKeepsTheOldGenerationAfterCommitAndANewSession) {
  const sde::PlacementMap first = events_map(1);
  sde::prepare_schema(model_, first, engines(), kProject);
  const sde::PlacementMap current = events_map(2);
  AfterCommit source(source_->engine(), [&] {
    // Between the source's commit and the copy's deferred write: a maintenance moves both tables
    // to generation 2, a new session opens on it, and the copy gets a row of its own.
    advance(first, 2);
    const sde::Session opened(model_, current, engines(), options());
    copy_->engine().insert("copy_events", {{"id", std::int64_t{1}}, {"value", std::int64_t{99}},
                                           {kEpoch, std::int64_t{2}}});
  });
  sde::Session old(model_, first, {{"a", &source}, {"b", &copy_->engine()}}, options());
  old.transaction({"Event"}, [&] {
    old.save("Event", {{"id", std::int64_t{1}}, {"value", std::int64_t{11}}});
  });
  EXPECT_EQ(source_->engine().get("source_events", {{"id", std::int64_t{1}}})->at("value"),
            sde::Value(std::int64_t{11}));
  EXPECT_EQ(copy_->engine().get("copy_events", {{"id", std::int64_t{1}}})->at("value"),
            sde::Value(std::int64_t{99}))
      << "the old session's deferred write reached the copy under the new generation";
}

std::vector<Direction> from_postgres() {
  std::vector<Direction> out;
  for (const Direction& direction : sde::live::directions()) {
    if (direction.source == "postgres" && direction.two) out.push_back(direction);
  }
  return out;
}

INSTANTIATE_TEST_SUITE_P(FromPostgres, DeferredCopies, ::testing::ValuesIn(from_postgres()),
                         sde::live::direction_name);
GTEST_ALLOW_UNINSTANTIATED_PARAMETERIZED_TEST(DeferredCopies);

// --- the comparison under named barriers --------------------------------------------------------

/// The copy's adapter, with something happening after its first keyset read: a comparison in
/// progress.
class DuringComparison final : public sde::Engine, public sde::Migratable {
 public:
  DuringComparison(sde::Engine& inner, std::function<void()> then)
      : inner_(inner), migration_(*inner.capabilities().migration), then_(std::move(then)) {}
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
    std::vector<sde::Row> rows = migration_.key_range(table, order, after, upto, limit);
    if (then_) std::exchange(then_, nullptr)();
    return rows;
  }
  std::optional<std::vector<sde::Value>> nth_key(const std::string& table,
                                                 const std::vector<std::string>& order,
                                                 std::int64_t position) override {
    return migration_.nth_key(table, order, position);
  }
  void copy_in(const std::string& table, const std::vector<sde::Row>& rows) override {
    migration_.copy_in(table, rows);
  }
  std::uint64_t count(const std::string& table) override { return migration_.count(table); }
  std::int64_t backfill_marker(const std::string& materialization,
                               const std::string& entity) override {
    return migration_.backfill_marker(materialization, entity);
  }
  void record_backfill_marker(const std::string& materialization, const std::string& entity,
                              std::int64_t rows) override {
    migration_.record_backfill_marker(materialization, entity, rows);
  }

 private:
  sde::Engine& inner_;
  sde::Migratable& migration_;
  std::function<void()> then_;
};

class Frozen : public GenerationCopies {
 protected:
  const std::string kHold = std::string(32, '6');

  void SetUp() override {
    GenerationCopies::SetUp();
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

  std::vector<std::string> holds(const std::string& name, const std::string& table) {
    return fence_of(name, table).state().holds;
  }

  /// The copy's row 1 written again, as `value` at `epoch`: removed first, so each engine holds
  /// one row whatever its own semantics for a second write of a key.
  void rewrite_copy(std::int64_t value, std::int64_t epoch) {
    copy_->remove("copy_events", "id = 1");
    copy_->engine().insert("copy_events",
                           {{"id", std::int64_t{1}}, {"value", value}, {kEpoch, epoch}});
  }

  std::unique_ptr<sde::PlacementMap> map_;
  std::unique_ptr<sde::Session> session_;
  std::unique_ptr<sde::VerificationRequest> request_;
};

TEST_P(Frozen, ARowOnlyTheCopyHoldsCannotPassTheFrozenGate) {
  copy_->engine().insert("copy_events", {{"id", std::int64_t{99}}, {"value", std::int64_t{99}},
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
    EXPECT_THROW(side(name).engine().insert(table, {{"id", std::int64_t{2}},
                                                    {"value", std::int64_t{22}},
                                                    {kEpoch, std::int64_t{1}}}),
                 sde::EngineError)
        << table;
  }
}

TEST_P(Frozen, AnOperatorComparesDifferentGenerationsWithoutAdoptingAMap) {
  sde::WriteFence target = fence_of("b", "copy_events");
  (void)target.freeze(std::string(32, '8'));
  (void)target.advance(2);
  (void)target.release(std::string(32, '8'));
  rewrite_copy(11, 2);
  EXPECT_TRUE(contains(message_of([&] {
                         const sde::Session opened(model_, *map_, engines(), options());
                       }),
                       "write generation"));
  const sde::FrozenVerifyReport report = frozen({{"source", 1}, {"copy", 2}});
  EXPECT_TRUE(report.matched());
  std::map<std::string, std::int64_t> epochs;
  for (const sde::FrozenTable& barrier : report.barriers) epochs[barrier.materialization] = barrier.epoch;
  EXPECT_EQ(epochs, (std::map<std::string, std::int64_t>{{"source", 1}, {"copy", 2}}));
  EXPECT_FALSE(source_->engine().capabilities().watermark->map_watermark().has_value());
  EXPECT_FALSE(copy_->engine().capabilities().watermark->map_watermark().has_value());
}

TEST_P(Frozen, ReleasingABarrierDuringTheComparisonInvalidatesIt) {
  DuringComparison copy(copy_->engine(), [&] {
    (void)fence_of("a", "source_events").release(kHold);
  });
  const std::string refused = message_of(
      [&] { (void)frozen({{"source", 1}, {"copy", 1}}, {{"a", &source_->engine()}, {"b", &copy}}); });
  EXPECT_TRUE(contains(refused, "lost its named barrier")) << refused;
}

TEST_P(Frozen, AWrongGenerationIsRefusedBeforeAnyTableIsClosed) {
  const std::string refused = message_of([&] { (void)frozen({{"source", 1}, {"copy", 2}}); });
  EXPECT_TRUE(contains(refused, "expected write generation")) << refused;
  EXPECT_TRUE(holds("a", "source_events").empty());
  EXPECT_TRUE(holds("b", "copy_events").empty());
}

TEST_P(Frozen, EqualCountsCannotHideAnotherValueInTheCopy) {
  rewrite_copy(99, 1);
  const sde::FrozenVerifyReport report = frozen({{"source", 1}, {"copy", 1}});
  EXPECT_EQ(report.comparison.rows_source, 1);
  EXPECT_EQ(report.comparison.rows_target, 1);
  EXPECT_FALSE(report.comparison.matched());
  EXPECT_FALSE(report.matched());
}

TEST_P(Frozen, ARetiredBarrierIdIsRefusedBeforeAnotherTableIsClosed) {
  sde::WriteFence source = fence_of("a", "source_events");
  (void)source.freeze(kHold);
  (void)source.release(kHold);
  const std::string refused = message_of([&] { (void)frozen({{"source", 1}, {"copy", 1}}); });
  EXPECT_TRUE(contains(refused, "retired barrier id")) << refused;
  EXPECT_TRUE(source.state().holds.empty());
  EXPECT_TRUE(holds("b", "copy_events").empty());
}

INSTANTIATE_TEST_SUITE_P(Directions, Frozen, ::testing::ValuesIn(with_two_adapters()),
                         sde::live::direction_name);

}  // namespace
