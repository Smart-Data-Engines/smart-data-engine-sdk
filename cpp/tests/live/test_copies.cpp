/// Backfill and verification against real engines, in every direction a group can move: within
/// PostgreSQL and within ClickHouse - through one adapter, as the reference does, and through two
/// in two namespaces, so the marker lives where the source's connection cannot see it - and from
/// each engine to the other. Ported from the reference's `test_migration_live.py`, parametrised as
/// it is, from its temporal-key backfill and from `test_engine_agreement.py`, the one test that
/// cannot be written against a single engine. A direction whose adapter is not built is not
/// instantiated.
///
/// Three claims a fake would agree with while being wrong: keyset pagination over a composite key
/// (a row comparison, written differently in each engine); a copy that is idempotent through each
/// engine's own key semantics (PostgreSQL skips a duplicate through the primary key, ClickHouse
/// collapses it under FINAL); and two adapters agreeing on a value.
///
///     export SDE_POSTGRES_DSN=postgresql://postgres:sde@127.0.0.1:55432/sde
///     export SDE_CLICKHOUSE_DSN=clickhouse://default:sde@127.0.0.1:58123/sde
///     ctest -L live

#include <cstdint>
#include <map>
#include <memory>
#include <optional>
#include <ostream>
#include <string>
#include <utility>
#include <variant>
#include <vector>

#include <gtest/gtest.h>

#include "live/live.hpp"
#include "sde/errors.hpp"
#include "sde/json.hpp"
#include "sde/migration.hpp"
#include "sde/model.hpp"
#include "sde/placement.hpp"
#include "sde/provisioning.hpp"
#include "sde/session.hpp"
#include "sde/telemetry.hpp"
#include "sde/verification.hpp"
#include "sde/write_fence.hpp"

#ifdef SDE_LIVE_POSTGRES
#include "live/postgres.hpp"
#include "sde/postgres.hpp"
#endif
#ifdef SDE_LIVE_CLICKHOUSE
#include "live/clickhouse.hpp"
#include "sde/clickhouse.hpp"
#endif

namespace {

const std::string kSource = "mig_src";
const std::string kTarget = "mig_dst";
const std::string kProject(32, '1');

/// One engine of a pair, in a namespace of its own - a schema or a database - dropped with it.
class Side {
 public:
  virtual ~Side() = default;
  [[nodiscard]] virtual std::string dialect() const = 0;
  [[nodiscard]] virtual sde::Engine& engine() = 0;
  /// A fresh adapter on the same namespace, as an operator re-running a job has.
  virtual void reconnect() = 0;
  /// Rows out of a table by hand: deliberate damage is the only way to test a gate.
  virtual void remove(const std::string& table, const std::string& where) = 0;
  virtual void drop(const std::string& table) = 0;
  /// The engine's type for `int32`, `string` and `timestamptz`.
  [[nodiscard]] virtual std::string type_of(const std::string& neutral) const = 0;

  sde::Migratable& migration() { return *engine().capabilities().migration; }
};

#ifdef SDE_LIVE_POSTGRES
class PostgresSide final : public Side {
 public:
  explicit PostgresSide(const std::string& dsn) : scope_(dsn) { reconnect(); }
  [[nodiscard]] std::string dialect() const override { return "postgres"; }
  sde::Engine& engine() override { return *engine_; }
  void reconnect() override {
    engine_ = std::make_unique<sde::PostgresEngine>(scope_.dsn());
    engine_->connect();
  }
  void remove(const std::string& table, const std::string& where) override {
    (void)sde::live::Admin(scope_.dsn()).run("DELETE FROM \"" + table + "\" WHERE " + where);
  }
  void drop(const std::string& table) override {
    (void)sde::live::Admin(scope_.dsn()).run("DROP TABLE \"" + table + "\"");
  }
  [[nodiscard]] std::string type_of(const std::string& neutral) const override {
    if (neutral == "int32") return "integer";
    if (neutral == "string") return "text";
    return "timestamptz";
  }

 private:
  sde::live::Scope scope_;
  std::unique_ptr<sde::PostgresEngine> engine_;
};
#endif

#ifdef SDE_LIVE_CLICKHOUSE
class ClickHouseSide final : public Side {
 public:
  explicit ClickHouseSide(const std::string& dsn) : scope_(dsn) { reconnect(); }
  [[nodiscard]] std::string dialect() const override { return "clickhouse"; }
  sde::Engine& engine() override { return *engine_; }
  void reconnect() override {
    engine_ = std::make_unique<sde::ClickHouseEngine>(scope_.dsn());
    engine_->connect();
  }
  void remove(const std::string& table, const std::string& where) override {
    // A lightweight delete, waited for, as the reference's test deletes.
    scope_.admin().run("DELETE FROM `" + table + "` WHERE " + where + " SETTINGS mutations_sync = 2");
  }
  void drop(const std::string& table) override {
    scope_.admin().run("DROP TABLE `" + table + "` SYNC");
  }
  [[nodiscard]] std::string type_of(const std::string& neutral) const override {
    if (neutral == "int32") return "Int32";
    if (neutral == "string") return "String";
    return "DateTime64(6, 'UTC')";
  }

 private:
  sde::live::ClickHouseScope scope_;
  std::unique_ptr<sde::ClickHouseEngine> engine_;
};
#endif

std::unique_ptr<Side> side_of(const std::string& dialect) {
#ifdef SDE_LIVE_POSTGRES
  if (dialect == "postgres") {
    std::string dsn;
    if (const auto found = sde::live::dsn_from("SDE_POSTGRES_DSN")) dsn = *found;
    return dsn.empty() ? nullptr : std::make_unique<PostgresSide>(dsn);
  }
#endif
#ifdef SDE_LIVE_CLICKHOUSE
  if (dialect == "clickhouse") {
    std::string dsn;
    if (const auto found = sde::live::dsn_from("SDE_CLICKHOUSE_DSN")) dsn = *found;
    return dsn.empty() ? nullptr : std::make_unique<ClickHouseSide>(dsn);
  }
#endif
  return nullptr;
}

/// Where a group goes: from `source` to `target`, through one adapter or two.
struct Direction {
  std::string source;
  std::string target;
  bool two = false;
};

void PrintTo(const Direction& direction, std::ostream* out) {
  *out << direction.source << " to " << direction.target
       << (direction.two ? ", two adapters" : ", one adapter");
}

std::string name_of(const ::testing::TestParamInfo<Direction>& info) {
  const auto short_name = [](const std::string& dialect) {
    return dialect == "postgres" ? std::string("Postgres") : std::string("ClickHouse");
  };
  std::string name = short_name(info.param.source) + "To" + short_name(info.param.target);
  if (info.param.source == info.param.target) name += info.param.two ? "TwoAdapters" : "OneAdapter";
  return name;
}

std::vector<Direction> directions() {
  std::vector<Direction> out;
#ifdef SDE_LIVE_POSTGRES
  out.push_back({"postgres", "postgres", false});
  out.push_back({"postgres", "postgres", true});
#endif
#ifdef SDE_LIVE_CLICKHOUSE
  out.push_back({"clickhouse", "clickhouse", false});
  out.push_back({"clickhouse", "clickhouse", true});
#endif
#if defined(SDE_LIVE_POSTGRES) && defined(SDE_LIVE_CLICKHOUSE)
  out.push_back({"postgres", "clickhouse", true});
  out.push_back({"clickhouse", "postgres", true});
#endif
  return out;
}

/// The skip, or in CI the failure, for an engine whose DSN is not set.
#define SDE_REQUIRE_SIDE(side, dialect)                                                      \
  do {                                                                                       \
    if (!(side)) {                                                                           \
      if (::sde::live::in_ci()) FAIL() << "the " << (dialect) << " DSN is not set in CI";   \
      GTEST_SKIP() << "set the " << (dialect) << " DSN to run this direction";             \
    }                                                                                        \
  } while (false)

// --- copies of a group --------------------------------------------------------------------------

/// No timestamp: what a moment keyed copy needs is its own test below. The subject here is the copy.
sde::Model readings() {
  return sde::load_neutral_model(R"({"entities": [{"name": "Reading", "fields": [
      {"name": "tenant", "type": "int32"}, {"name": "seq", "type": "int32"},
      {"name": "station", "type": "string"}], "key": ["tenant", "seq"]}]})");
}

class Copies : public ::testing::TestWithParam<Direction> {
 protected:
  void SetUp() override {
    source_ = side_of(GetParam().source);
    SDE_REQUIRE_SIDE(source_, GetParam().source);
    if (GetParam().two) {
      target_ = side_of(GetParam().target);
      SDE_REQUIRE_SIDE(target_, GetParam().target);
    }
    model_ = std::make_unique<sde::Model>(readings());
    map_ = std::make_unique<sde::PlacementMap>(copying());
    open();
  }

  Side& target() { return target_ ? *target_ : *source_; }

  sde::PlacementMap copying() {
    const auto columns = [&](const Side& side) {
      return R"({"Reading": {"tenant": ")" + side.type_of("int32") + R"(", "seq": ")" +
             side.type_of("int32") + R"(", "station": ")" + side.type_of("string") + R"("}})";
    };
    sde::LoadOptions options;
    options.model = model_.get();
    return sde::load_map(
        R"({"contract": 3, "model_version": ")" + model_->version() + R"(", "map_version": 1,
            "groups": {"Reading": {
              "source": {"id": "src", "engine": "source", "layout": {
                "tables": {"Reading": ")" + kSource + R"("}, "columns": )" + columns(*source_) + R"(}},
              "derived": [{"id": "dst", "engine": "target", "lag_budget_ms": 30000, "layout": {
                "tables": {"Reading": ")" + kTarget + R"("}, "columns": )" + columns(target()) +
            R"(}}],
              "also_write": ["dst"]}}})",
        options);
  }

  /// A fresh session on the adapters as they are, schema ensured.
  void open(sde::Recorder* recorder = nullptr) {
    sde::SessionOptions options;
    options.recorder = recorder;
    session_.reset();
    session_ = std::make_unique<sde::Session>(
        *model_, *map_,
        std::map<std::string, sde::Engine*>{{"source", &source_->engine()},
                                            {"target", &target().engine()}},
        options);
    session_->ensure_schema();
  }

  /// Rows straight into the source, past the fan-out: the rows that existed when dual write began,
  /// which the backfill's whole job is to move.
  void fill(int rows) {
    for (int n = 1; n <= rows; ++n) {
      source_->engine().insert(kSource, {{"tenant", std::int64_t{1 + n % 3}},
                                         {"seq", std::int64_t{n}},
                                         {"station", "s" + std::to_string(n)}});
    }
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

  std::unique_ptr<Side> source_;
  std::unique_ptr<Side> target_;
  std::unique_ptr<sde::Model> model_;
  std::unique_ptr<sde::PlacementMap> map_;
  std::unique_ptr<sde::Recorder> recorder_;  ///< declared before the session, so it outlives it
  std::unique_ptr<sde::Session> session_;
};

TEST_P(Copies, AGroupIsCopiedInChunksAndTheCopyVerifies) {
  fill(25);
  const sde::BackfillProgress progress = copy(7);
  EXPECT_TRUE(progress.complete());
  EXPECT_EQ(progress.rows_this_run(), 25);
  EXPECT_EQ(target().migration().count(kTarget), 25U);
  const sde::VerifyReport report = compare(7);
  EXPECT_TRUE(report.matched()) << report.for_a_human();
  EXPECT_EQ(report.chunks_compared, 4);
  EXPECT_EQ(report.rows_source, 25);
  EXPECT_EQ(report.rows_target, 25);
}

TEST_P(Copies, TheMarkerSurvivesANewAdapterAndTheBackfillResumes) {
  // A fresh adapter and a fresh session, which is what an operator re-running an interrupted job
  // has. Anything kept in the process would have looked the same up to here.
  fill(20);
  const sde::BackfillProgress first = copy(5, 2);
  EXPECT_FALSE(first.complete());
  EXPECT_EQ(first.rows_this_run(), 10);
  session_.reset();
  source_->reconnect();
  if (target_) target_->reconnect();
  open();
  const sde::BackfillProgress second = copy(5);
  EXPECT_TRUE(second.complete());
  EXPECT_EQ(second.rows_this_run(), 10);
  EXPECT_EQ(target().migration().count(kTarget), 20U);
  EXPECT_TRUE(compare(5).matched());
}

TEST_P(Copies, ARecopiedChunkLeavesOneRowInEachEnginesOwnWay) {
  // The idempotence the write-then-marker order depends on: PostgreSQL skips the duplicate through
  // the primary key the layout created, ClickHouse collapses it and counts it once under FINAL.
  fill(12);
  EXPECT_TRUE(copy(12).complete());
  EXPECT_EQ(target().migration().backfill_marker("dst", "Reading"), 12);
  // The crash window, reproduced: the chunk landed and the marker did not move.
  target().drop(std::string(sde::BACKFILL_TABLE));
  EXPECT_EQ(target().migration().backfill_marker("dst", "Reading"), 0);
  EXPECT_TRUE(copy(12).complete());
  EXPECT_EQ(target().migration().count(kTarget), 12U);
  EXPECT_TRUE(compare(12).matched());
}

TEST_P(Copies, ARowDeletedFromTheCopyStopsTheMigrationAndNamesTheRow) {
  fill(15);
  EXPECT_TRUE(copy(5).complete());
  target().remove(kTarget, "seq = 7");
  const sde::VerifyReport report = compare(5);
  EXPECT_FALSE(report.matched());
  EXPECT_EQ(report.chunks_mismatched, 1);
  EXPECT_EQ(report.tail_rows_missing_in_target, 0);
  ASSERT_FALSE(report.differences.empty());
  EXPECT_EQ(report.differences[0].key.at("seq"), sde::Value(std::int64_t{7}));
  EXPECT_TRUE(report.differences[0].absent());
}

TEST_P(Copies, AWriteTheFanOutLostIsCaughtAboveTheMarker) {
  // Tenant 4 sorts above every key the fill wrote, so the row lands above the marker.
  fill(10);
  EXPECT_TRUE(copy(10).complete());
  session_->save("Reading", {{"tenant", std::int64_t{4}},
                             {"seq", std::int64_t{999}},
                             {"station", std::string("arrived-by-fan-out")}});
  EXPECT_EQ(target().migration().count(kTarget), 11U);
  target().remove(kTarget, "seq = 999");
  const sde::VerifyReport report = compare(10);
  EXPECT_FALSE(report.matched());
  EXPECT_EQ(report.chunks_mismatched, 0);
  EXPECT_EQ(report.tail_rows_read, 1);
  EXPECT_EQ(report.tail_rows_missing_in_target, 1);
}

TEST_P(Copies, AValueChangedInTheCopyIsFoundAndTheColumnNamed) {
  fill(6);
  EXPECT_TRUE(copy(6).complete());
  target().remove(kTarget, "seq = 3");
  target().engine().insert(kTarget, {{"tenant", std::int64_t{1}},
                                     {"seq", std::int64_t{3}},
                                     {"station", std::string("not-what-was-written")}});
  const sde::VerifyReport report = compare(6);
  EXPECT_FALSE(report.matched());
  ASSERT_FALSE(report.differences.empty());
  EXPECT_EQ(report.differences[0].columns, std::vector<std::string>{"station"});
}

TEST_P(Copies, AnUntouchedEngineReportsNoMarkerAndTheMarkerIsTheHighest) {
  // max() over nothing: null in PostgreSQL, 0 in ClickHouse - and either means nothing is copied.
  EXPECT_EQ(target().migration().backfill_marker("dst", "Reading"), 0);
  EXPECT_EQ(target().migration().backfill_marker("dst", "Reading"), 0);
  target().migration().record_backfill_marker("dst", "Reading", 3);
  target().migration().record_backfill_marker("dst", "Reading", 1);
  EXPECT_EQ(target().migration().backfill_marker("dst", "Reading"), 3)
      << "append-only, and the answer is max() - a stale row cannot lower the marker";
}

TEST_P(Copies, TheMeasuredLagOfACopyIsARealServersWrite) {
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
  EXPECT_EQ(target().migration().count(kTarget), 10U);
}

INSTANTIATE_TEST_SUITE_P(Directions, Copies, ::testing::ValuesIn(directions()), name_of);

// --- a moment in the key ------------------------------------------------------------------------

class MomentKeyedCopies : public ::testing::TestWithParam<Direction> {};

TEST_P(MomentKeyedCopies, ABackfillResumesAndVerifiesExactMoments) {
  // The reference's temporal backfill: readings keyed by a hostile station and a moment four
  // microseconds wide, copied a row a chunk, stopped after two and resumed, then compared - each
  // copy read back to the microsecond, its generation column aside.
  const std::unique_ptr<Side> source = side_of(GetParam().source);
  SDE_REQUIRE_SIDE(source, GetParam().source);
  const std::unique_ptr<Side> target = side_of(GetParam().target);
  SDE_REQUIRE_SIDE(target, GetParam().target);
  const sde::Model model = sde::load_neutral_model(R"({"entities": [{"name": "Reading", "fields": [
      {"name": "station", "type": "string"}, {"name": "at", "type": "timestamptz"},
      {"name": "value", "type": "int32"}], "key": ["station", "at"]}]})");
  const auto material = [&](const Side& side, const std::string& name) {
    return R"({"id": ")" + name + R"(", "engine": ")" + name + R"(", "layout": {"tables": {"Reading": ")" +
           name + R"(_readings"}, "columns": {"Reading": {"station": ")" + side.type_of("string") +
           R"(", "at": ")" + side.type_of("timestamptz") + R"(", "value": ")" + side.type_of("int32") +
           R"("}}}})";
  };
  std::string copy = material(*target, "copy");
  copy.pop_back();
  copy += R"(, "lag_budget_ms": 1000})";
  sde::LoadOptions options;
  options.model = &model;
  const sde::PlacementMap map = sde::load_map(
      R"({"contract": 4, "project_id": ")" + kProject + R"(", "model_version": ")" + model.version() +
          R"(", "map_version": 1, "groups": {"Reading": {"source": )" + material(*source, "source") +
          R"(, "write_epoch": 1, "derived": [)" + copy + R"(], "also_write": ["copy"]}}})",
      options);
  const std::map<std::string, sde::Engine*> engines{{"source", &source->engine()},
                                                    {"copy", &target->engine()}};
  sde::prepare_schema(model, map, engines, kProject);
  std::vector<sde::Row> rows;
  for (std::int64_t n = 0; n < 4; ++n) {
    rows.push_back({{"station", std::string("station'\\\\\xC3\xA9")},
                    {"at", *sde::TimestampTz::from_micros(1789387200000000 + 123456 + n)},
                    {"value", n}});
  }
  for (sde::Row row : rows) {
    row[std::string(sde::EPOCH_COLUMN)] = std::int64_t{1};
    source->engine().insert("source_readings", row);
  }
  sde::SessionOptions session_options;
  session_options.project_id = kProject;
  sde::Session session(model, map, engines, session_options);
  sde::BackfillOptions twice;
  twice.chunk_rows = 1;
  twice.stop_after = 2;
  (void)sde::backfill(session, "Reading", twice);
  EXPECT_EQ(target->migration().count("copy_readings"), 2U);
  sde::BackfillOptions rest;
  rest.chunk_rows = 1;
  rest.stop_after = 5;
  (void)sde::backfill(session, "Reading", rest);
  EXPECT_EQ(target->migration().count("copy_readings"), rows.size());
  sde::VerifyOptions compared;
  compared.chunk_rows = 1;
  EXPECT_TRUE(sde::verify(session, "Reading", compared).matched());
  for (const sde::Row& expected : rows) {
    std::optional<sde::Row> observed = target->engine().get(
        "copy_readings", {{"station", expected.at("station")}, {"at", expected.at("at")}});
    ASSERT_TRUE(observed.has_value());
    observed->erase(std::string(sde::EPOCH_COLUMN));
    EXPECT_EQ(*observed, expected);
  }
}

std::vector<Direction> across() {
  std::vector<Direction> out;
#if defined(SDE_LIVE_POSTGRES) && defined(SDE_LIVE_CLICKHOUSE)
  out.push_back({"postgres", "clickhouse", true});
  out.push_back({"clickhouse", "postgres", true});
#endif
  return out;
}

INSTANTIATE_TEST_SUITE_P(Across, MomentKeyedCopies, ::testing::ValuesIn(across()), name_of);
GTEST_ALLOW_UNINSTANTIATED_PARAMETERIZED_TEST(MomentKeyedCopies);

}  // namespace
