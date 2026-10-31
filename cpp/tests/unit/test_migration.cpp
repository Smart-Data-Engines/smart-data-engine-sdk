/// Backfill and verify where the shared vectors do not reach, ported from the reference's own
/// tests: the boundary between a record and a row, every refusal before a row moves, and the three
/// properties a copy rests on - the marker is a row count, the chunk is written before the marker,
/// and a recopy is idempotent - each with a test that fails when it is removed.
///
/// The engine here is the reference's test fake rather than the library's in-memory engine: it can
/// hide a row from a window, refuse to record progress, and count the size of each read, which is
/// what these properties are observed through.

#include <algorithm>
#include <functional>
#include <map>
#include <memory>
#include <optional>
#include <set>
#include <stdexcept>
#include <string>
#include <utility>
#include <variant>
#include <vector>

#include <gtest/gtest.h>

#include "sde/canonical.hpp"
#include "sde/errors.hpp"
#include "sde/migration.hpp"
#include "sde/model.hpp"
#include "sde/placement.hpp"
#include "sde/session.hpp"
#include "sde/testing/memory.hpp"
#include "sde/write_fence.hpp"
#include "sde/verification.hpp"

namespace {

using Key = std::vector<sde::Value>;

/// Keys here are integers and text; the order of either is its natural one.
bool key_less(const Key& left, const Key& right) {
  return std::lexicographical_compare(
      left.begin(), left.end(), right.begin(), right.end(), [](const auto& a, const auto& b) {
        if (a.index() != b.index()) return a.index() < b.index();
        if (const auto* number = std::get_if<std::int64_t>(&a)) {
          return *number < std::get<std::int64_t>(b);
        }
        if (const auto* text = std::get_if<std::string>(&a)) {
          return *text < std::get<std::string>(b);
        }
        throw std::logic_error("the test engine orders integers and text only");
      });
}

/// An engine that keeps rows in memory: an `Engine` that is also `Migratable`. `copy_in` skips a
/// row whose key is present, which both real adapters do by different means; a fake that appended
/// blindly would let an idempotence bug pass.
class Store final : public sde::Engine, public sde::Migratable {
 public:
  Store(std::string name, std::map<std::string, std::vector<std::string>> keys,
        std::vector<std::string>* calls)
      : name_(std::move(name)), keys_(std::move(keys)), calls_(calls) {}

  std::map<std::string, std::vector<sde::Row>> tables;
  std::map<std::pair<std::string, std::string>, std::vector<std::int64_t>> markers;
  std::vector<std::size_t> range_sizes;
  std::set<Key, decltype(&key_less)> hide_from_range{&key_less};
  bool fail_marker_writes = false;

  [[nodiscard]] std::string_view dialect() const noexcept override { return "postgres"; }
  std::vector<sde::PhysicalFinding> ensure_schema(const sde::PhysicalLayout&,
                                                  const sde::Keys&) override {
    return {};
  }
  void insert(const std::string& table, const sde::Row& values) override {
    tables[table].push_back(values);
  }
  std::optional<sde::Row> get(const std::string& table, const sde::Row& key) override {
    note("get");
    for (const sde::Row& row : tables[table]) {
      if (std::all_of(key.begin(), key.end(), [&](const auto& item) {
            const auto found = row.find(item.first);
            return found != row.end() && found->second == item.second;
          })) {
        return row;
      }
    }
    return std::nullopt;
  }
  void transaction(const std::function<void()>& body) override { body(); }
  [[nodiscard]] sde::Capabilities capabilities() noexcept override {
    sde::Capabilities offered;
    offered.migration = this;
    return offered;
  }

  std::vector<sde::Row> key_range(const std::string& table, const std::vector<std::string>& order,
                                  const std::optional<Key>& after, const std::optional<Key>& upto,
                                  std::optional<std::size_t> limit) override {
    note("key_range");
    const std::vector<std::string> columns = sde::key_columns(order, table);
    if (after) sde::same_width(*after, columns, "after");
    if (upto) sde::same_width(*upto, columns, "upto");
    std::vector<sde::Row> out;
    for (const sde::Row& row : sorted(table, columns)) {
      const Key key = key_of(row, columns);
      if (after && !key_less(*after, key)) continue;
      if (upto && key_less(*upto, key)) continue;
      if (hide_from_range.contains(key)) continue;
      out.push_back(row);
      if (limit && out.size() >= *limit) break;
    }
    range_sizes.push_back(out.size());
    return out;
  }
  std::optional<Key> nth_key(const std::string& table, const std::vector<std::string>& order,
                             std::int64_t position) override {
    const std::vector<std::string> columns = sde::key_columns(order, table);
    const std::vector<sde::Row> rows = sorted(table, columns);
    if (position < 1 || position > static_cast<std::int64_t>(rows.size())) return std::nullopt;
    return key_of(rows[static_cast<std::size_t>(position - 1)], columns);
  }
  void copy_in(const std::string& table, const std::vector<sde::Row>& rows) override {
    note("copy_in");
    const std::vector<std::string>& key = keys_.at(table);
    std::set<Key, decltype(&key_less)> present(&key_less);
    for (const sde::Row& row : tables[table]) present.insert(key_of(row, key));
    for (const sde::Row& row : rows) {
      if (present.insert(key_of(row, key)).second) tables[table].push_back(row);
    }
  }
  std::uint64_t count(const std::string& table) override { return tables[table].size(); }
  std::int64_t backfill_marker(const std::string& materialization,
                               const std::string& entity) override {
    const auto found = markers.find({materialization, entity});
    if (found == markers.end() || found->second.empty()) return 0;
    return *std::max_element(found->second.begin(), found->second.end());
  }
  void record_backfill_marker(const std::string& materialization, const std::string& entity,
                              std::int64_t rows) override {
    if (fail_marker_writes) throw sde::EngineError(name_ + " will not record progress");
    markers[{materialization, entity}].push_back(rows);
  }

 private:
  void note(std::string_view method) {
    if (calls_ != nullptr) calls_->push_back(name_ + "." + std::string(method));
  }
  static Key key_of(const sde::Row& row, const std::vector<std::string>& columns) {
    Key key;
    for (const std::string& column : columns) key.push_back(row.at(column));
    return key;
  }
  std::vector<sde::Row> sorted(const std::string& table, const std::vector<std::string>& columns) {
    std::vector<sde::Row> rows = tables[table];
    std::stable_sort(rows.begin(), rows.end(), [&](const sde::Row& a, const sde::Row& b) {
      return key_less(key_of(a, columns), key_of(b, columns));
    });
    return rows;
  }

  std::string name_;
  std::map<std::string, std::vector<std::string>> keys_;
  std::vector<std::string>* calls_;
};

/// An engine with no row-level operations at all - our orderbook engine, in miniature.
class Plain final : public sde::Engine {
 public:
  [[nodiscard]] std::string_view dialect() const noexcept override { return "postgres"; }
  std::vector<sde::PhysicalFinding> ensure_schema(const sde::PhysicalLayout&,
                                                  const sde::Keys&) override {
    return {};
  }
  void insert(const std::string&, const sde::Row&) override {}
  std::optional<sde::Row> get(const std::string&, const sde::Row&) override { return std::nullopt; }
  void transaction(const std::function<void()>& body) override { body(); }
};

const char* const kColumns = R"({"Reading": {"id": "integer", "station": "text"}})";

sde::Model readings() {
  return sde::load_neutral_model(R"({"entities": [{"name": "Reading", "fields": [
      {"name": "id", "type": "int32"}, {"name": "station", "type": "string"}], "key": ["id"]}]})");
}

sde::PlacementMap placed(const sde::Model& model, bool fan_out = true,
                         const std::string& target_columns = kColumns) {
  const std::string group = model.groups().at(0).name;
  const std::string text =
      R"({"contract": 3, "model_version": ")" + model.version() +
      R"(", "map_version": 4, "groups": {")" + group + R"(": {
        "source": {"id": "src@pg", "engine": "pg",
                   "layout": {"tables": {"Reading": "reading"}, "columns": )" +
      kColumns + R"(}},
        "derived": [{"id": "copy@ch", "engine": "ch", "lag_budget_ms": 30000,
                     "layout": {"tables": {"Reading": "reading_copy"}, "columns": )" +
      target_columns + "}}]" + (fan_out ? R"(, "also_write": ["copy@ch"])" : "") + "}}}";
  sde::LoadOptions options;
  options.model = &model;
  return sde::load_map(std::string_view(text), options);
}

/// The reference's `_session`: a source with `rows` readings, an empty copy, and the map that
/// fans writes out to it.
struct Fixture {
  explicit Fixture(int rows = 0, bool fan_out = true, const std::string& target_columns = kColumns)
      : model(readings()),
        map(placed(model, fan_out, target_columns)),
        source("pg", {{"reading", {"id"}}}, &calls),
        target("ch", {{"reading_copy", {"id"}}}, &calls) {
    for (int n = 1; n <= rows; ++n) source.insert("reading", row(n));
    session.emplace(model, map,
                    std::map<std::string, sde::Engine*>{{"pg", &source}, {"ch", &target}});
  }

  static sde::Row row(std::int64_t id) {
    return sde::Row{{"id", id}, {"station", "s" + std::to_string(id)}};
  }
  [[nodiscard]] std::string group() const { return model.groups().at(0).name; }
  sde::BackfillProgress backfill(std::int64_t chunk_rows,
                                 std::optional<std::int64_t> stop_after = std::nullopt) {
    return sde::backfill(*session, group(), {chunk_rows, stop_after});
  }
  sde::VerifyReport verify(std::int64_t chunk_rows = sde::CHUNK_ROWS) {
    sde::VerifyOptions options;
    options.chunk_rows = chunk_rows;
    return sde::verify(*session, group(), options);
  }
  void drop_from_copy(std::int64_t id) {
    auto& rows = target.tables["reading_copy"];
    rows.erase(std::remove_if(rows.begin(), rows.end(),
                              [&](const sde::Row& row) { return row.at("id") == sde::Value(id); }),
               rows.end());
  }
  std::vector<std::int64_t> copied_ids() {
    std::vector<std::int64_t> ids;
    for (const sde::Row& row : target.tables["reading_copy"]) {
      ids.push_back(std::get<std::int64_t>(row.at("id")));
    }
    std::sort(ids.begin(), ids.end());
    return ids;
  }

  std::vector<std::string> calls;
  sde::Model model;
  sde::PlacementMap map;
  Store source;
  Store target;
  std::optional<sde::Session> session;
};

std::string refusal(const std::function<void()>& body) {
  try {
    body();
  } catch (const sde::MigrationRefused& error) {
    return error.what();
  }
  ADD_FAILURE() << "nothing was refused";
  return "";
}

bool contains(const std::string& text, std::string_view fragment) {
  return text.find(fragment) != std::string::npos;
}

// ── The boundary: numbers cross, rows do not ────────────────────────────────────────────────────

TEST(VerifyRecord, CarriesCountsWhileTheDifferenceCarriesTheRow) {
  // A model keyed by text, so a key value is unmistakable in a serialised record.
  const std::string secret = "ORD-88213-would-be-a-leak";
  const sde::Model model = sde::load_neutral_model(R"({"entities": [{"name": "Ticket", "fields": [
      {"name": "reference", "type": "string"}, {"name": "note", "type": "string"}],
      "key": ["reference"]}]})");
  const std::string columns = R"({"Ticket": {"reference": "text", "note": "text"}})";
  const std::string text = R"({"contract": 3, "model_version": ")" + model.version() +
                           R"(", "map_version": 1, "groups": {"Ticket": {
      "source": {"id": "src@pg", "engine": "pg",
                 "layout": {"tables": {"Ticket": "ticket"}, "columns": )" + columns + R"(}},
      "derived": [{"id": "copy@ch", "engine": "ch", "lag_budget_ms": 1000,
                   "layout": {"tables": {"Ticket": "ticket_copy"}, "columns": )" + columns + R"(}}],
      "also_write": ["copy@ch"]}}})";
  sde::LoadOptions options;
  options.model = &model;
  const sde::PlacementMap map = sde::load_map(std::string_view(text), options);
  Store source("pg", {{"ticket", {"reference"}}}, nullptr);
  Store target("ch", {{"ticket_copy", {"reference"}}}, nullptr);
  sde::Session session(model, map, {{"pg", &source}, {"ch", &target}});
  source.insert("ticket", sde::Row{{"reference", secret}, {"note", "n"}});

  const sde::VerifyReport report = sde::verify(session, "Ticket");
  ASSERT_FALSE(report.matched());
  EXPECT_EQ(report.differences.at(0).key, (sde::Row{{"reference", secret}}));
  EXPECT_TRUE(contains(report.differences[0].for_a_human(), secret));
  EXPECT_FALSE(contains(sde::canonical_bytes(report.as_record()), secret));
  std::string human = report.for_a_human();
  const std::string detail = report.differences[0].for_a_human();
  human.erase(human.find(detail), detail.size());
  EXPECT_FALSE(contains(human, secret)) << human;
}

TEST(VerifyRecord, HasExactlyTheFieldsTheGateReads) {
  Fixture fixture(1);
  const sde::Json record = fixture.verify().as_record();
  std::set<std::string> names;
  for (const auto& member : record.as_object()) names.insert(member.first);
  EXPECT_EQ(names, (std::set<std::string>{"at", "chunks_compared", "chunks_mismatched",
                                          "tail_rows_read", "tail_rows_missing_in_target",
                                          "rows_source", "rows_target"}));
}

TEST(VerifyRecord, SaysTheDetailIsTheClientsOwnData) {
  Fixture fixture(1);
  fixture.source.insert("reading", Fixture::row(9));
  const std::string text = fixture.verify().for_a_human();
  EXPECT_TRUE(contains(text, "your own data")) << text;
  EXPECT_TRUE(contains(text, "not part of what is reported")) << text;
}

// ── Refusals, all of them before a row moves ────────────────────────────────────────────────────

TEST(MigrationRefusals, AGroupTheModelDoesNotHaveIsNamed) {
  Fixture fixture(1);
  EXPECT_TRUE(contains(refusal([&] { (void)sde::backfill(*fixture.session, "Nonexistent"); }),
                       "not a colocation group"));
}

TEST(MigrationRefusals, AnEngineWithNoRowOperationsSaysWhichRole) {
  const sde::Model model = readings();
  const sde::PlacementMap map = placed(model);
  Store store("pg", {{"reading", {"id"}}}, nullptr);
  Plain plain;
  sde::Session as_target(model, map, {{"pg", &store}, {"ch", &plain}});
  EXPECT_TRUE(contains(refusal([&] { (void)sde::backfill(as_target, "Reading"); }),
                       "cannot act as the target"));
  Store copy("ch", {{"reading_copy", {"id"}}}, nullptr);
  sde::Session as_source(model, map, {{"pg", &plain}, {"ch", &copy}});
  EXPECT_TRUE(contains(refusal([&] { (void)sde::backfill(as_source, "Reading"); }),
                       "cannot act as the source"));
}

TEST(MigrationRefusals, ATargetWithOtherColumnsIsAReshapeAndNotAMove) {
  // A wide denormalised copy is a legitimate materialisation, and not a migration target.
  Fixture fixture(1, true,
                  R"({"Reading": {"id": "integer", "station": "text",)"
                  R"( "station_country": "text"}})");
  const std::string message = refusal([&] { (void)fixture.backfill(10); });
  EXPECT_TRUE(contains(message, "only in the source: []; only in the target: ['station_country']"))
      << message;
  EXPECT_TRUE(fixture.target.tables["reading_copy"].empty());
}

TEST(MigrationRefusals, ALayoutWithTablesAndNoColumnsCannotBeCheckedForShape) {
  Fixture fixture(1, true, R"({"Reading": {}})");
  EXPECT_TRUE(
      contains(refusal([&] { (void)fixture.backfill(10); }), "does not describe the columns"));
}

TEST(MigrationRefusals, VerifyMakesEveryOneOfThemToo) {
  Fixture unmigrated(1, false);
  EXPECT_TRUE(contains(refusal([&] { (void)unmigrated.verify(); }), "no fan-out target"));
  Fixture reshaped(1, true, R"({"Reading": {"id": "integer"}})");
  EXPECT_TRUE(
      contains(refusal([&] { (void)reshaped.verify(); }), "only in the source: ['station']"));
}

TEST(MigrationRefusals, AChunkOfNoRowsIsNotAChunk) {
  Fixture fixture(1);
  EXPECT_TRUE(contains(refusal([&] { (void)fixture.backfill(0); }), "is not a chunk"));
  EXPECT_TRUE(contains(refusal([&] { (void)fixture.verify(0); }), "is not a chunk"));
}

// ── Backfill: a row count, the chunk before the marker, and idempotence ──────────────────────────

TEST(Backfill, CopiesEveryRowAndTheMarkerIsTheRowCount) {
  Fixture fixture(25);
  const sde::BackfillProgress progress = fixture.backfill(10);
  EXPECT_TRUE(progress.complete());
  EXPECT_EQ(progress.rows_this_run(), 25);
  EXPECT_EQ(fixture.target.count("reading_copy"), 25U);
  EXPECT_EQ(fixture.target.backfill_marker("copy@ch", "Reading"), 25);
  EXPECT_EQ(progress.entities.at(0).chunks, 3);
  // Three reads and not four: a short chunk is the end of the table, so there is no confirming
  // empty read against the client's own engine.
  EXPECT_EQ(fixture.source.range_sizes.size(), 3U);
}

TEST(Backfill, AnEarlyStopResumesFromTheMarkerAndCopiesEachRowOnce) {
  Fixture fixture(25);
  const sde::BackfillProgress first = fixture.backfill(10, 1);
  EXPECT_FALSE(first.complete());
  EXPECT_EQ(first.rows_this_run(), 10);
  const sde::BackfillProgress second = fixture.backfill(10);
  EXPECT_TRUE(second.complete());
  EXPECT_EQ(second.rows_this_run(), 15);
  std::vector<std::int64_t> all(25);
  for (int n = 0; n < 25; ++n) all[static_cast<std::size_t>(n)] = n + 1;
  EXPECT_EQ(fixture.copied_ids(), all);
}

TEST(Backfill, AFinishedBackfillDoesNothingWhenCalledAgain) {
  Fixture fixture(7);
  EXPECT_TRUE(fixture.backfill(3).complete());
  const sde::BackfillProgress again = fixture.backfill(3);
  EXPECT_TRUE(again.complete());
  EXPECT_EQ(again.rows_this_run(), 0);
  EXPECT_EQ(fixture.target.count("reading_copy"), 7U);
}

TEST(Backfill, TheChunkLandsBeforeTheMarkerSoACrashBetweenThemCostsARecopy) {
  Fixture fixture(5);
  fixture.target.fail_marker_writes = true;
  EXPECT_THROW((void)fixture.backfill(2), sde::EngineError);
  EXPECT_EQ(fixture.target.count("reading_copy"), 2U) << "the chunk landed";
  EXPECT_EQ(fixture.target.backfill_marker("copy@ch", "Reading"), 0);
  fixture.target.fail_marker_writes = false;
  EXPECT_TRUE(fixture.backfill(2).complete());
  EXPECT_EQ(fixture.target.count("reading_copy"), 5U) << "recopied, not duplicated, nothing lost";
}

TEST(Backfill, ARecopiedChunkLeavesOneRowAndNotTwo) {
  Fixture fixture(4);
  (void)fixture.backfill(4);
  fixture.target.markers.clear();
  (void)fixture.backfill(4);
  EXPECT_EQ(fixture.target.count("reading_copy"), 4U);
}

TEST(Backfill, ASourceThatLostRowsRefusesRatherThanResumingFromAGuess) {
  Fixture fixture(10);
  (void)fixture.backfill(10);
  fixture.source.tables["reading"].resize(4);
  EXPECT_TRUE(contains(refusal([&] { (void)fixture.backfill(sde::CHUNK_ROWS); }),
                       "does not have that many"));
}

TEST(Backfill, NoRowThatPredatesTheBackfillIsSteppedOver) {
  // Rows written during the migration land below the resume key on purpose: a key need not grow
  // with time. They reach the copy through the fan-out; every row that existed when the backfill
  // began is copied by it.
  Fixture fixture(10);
  (void)fixture.backfill(5, 1);
  EXPECT_EQ(fixture.target.backfill_marker("copy@ch", "Reading"), 5);
  for (std::int64_t n : {-3, -2, -1}) fixture.session->save("Reading", Fixture::row(n));
  EXPECT_TRUE(fixture.backfill(5).complete());
  EXPECT_EQ(fixture.copied_ids(),
            (std::vector<std::int64_t>{-3, -2, -1, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10}));
  EXPECT_TRUE(fixture.verify(5).matched());
}

TEST(Backfill, ACompositeKeyPaginatesAsOneOrdering) {
  const sde::Model model = sde::load_neutral_model(R"({"entities": [{"name": "Slot", "fields": [
      {"name": "tenant", "type": "int32"}, {"name": "seq", "type": "int32"}],
      "key": ["tenant", "seq"]}]})");
  const std::string columns = R"({"Slot": {"tenant": "integer", "seq": "integer"}})";
  const std::string text = R"({"contract": 3, "model_version": ")" + model.version() +
                           R"(", "map_version": 1, "groups": {"Slot": {
      "source": {"id": "src@pg", "engine": "pg",
                 "layout": {"tables": {"Slot": "slot"}, "columns": )" + columns + R"(}},
      "derived": [{"id": "copy@ch", "engine": "ch", "lag_budget_ms": 1000,
                   "layout": {"tables": {"Slot": "slot_copy"}, "columns": )" + columns + R"(}}],
      "also_write": ["copy@ch"]}}})";
  sde::LoadOptions options;
  options.model = &model;
  const sde::PlacementMap map = sde::load_map(std::string_view(text), options);
  Store source("pg", {{"slot", {"tenant", "seq"}}}, nullptr);
  Store target("ch", {{"slot_copy", {"tenant", "seq"}}}, nullptr);
  for (std::int64_t tenant : {1, 2, 3}) {
    for (std::int64_t seq = 1; seq <= 4; ++seq) {
      source.insert("slot", sde::Row{{"tenant", tenant}, {"seq", seq}});
    }
  }
  sde::Session session(model, map, {{"pg", &source}, {"ch", &target}});
  EXPECT_TRUE(sde::backfill(session, "Slot", {5, std::nullopt}).complete());
  EXPECT_EQ(target.count("slot_copy"), 12U);
  EXPECT_TRUE(sde::verify(session, "Slot").matched());
}

TEST(Backfill, ItsProgressLogFiresPerChunkAndCarriesNoValue) {
  const sde::Model model = readings();
  const sde::PlacementMap map = placed(model);
  Store source("pg", {{"reading", {"id"}}}, nullptr);
  Store target("ch", {{"reading_copy", {"id"}}}, nullptr);
  for (int n = 1; n <= 6; ++n) source.insert("reading", Fixture::row(n));
  std::vector<sde::Json> events;
  sde::SessionOptions options;
  options.log = [&](std::string_view event, const sde::Json& fields) {
    if (event == "sde.migration.backfill_progress") events.push_back(fields);
  };
  sde::Session session(model, map, {{"pg", &source}, {"ch", &target}}, options);
  (void)sde::backfill(session, "Reading", {2, std::nullopt});
  ASSERT_EQ(events.size(), 3U);
  EXPECT_EQ(*events.back().find("chunk"), sde::Json(3));
  EXPECT_EQ(*events.back().find("rows_copied"), sde::Json(6));
  std::set<std::string> names;
  for (const auto& member : events.back().as_object()) names.insert(member.first);
  EXPECT_EQ(names, (std::set<std::string>{"group", "entity", "engine", "table", "chunk", "rows",
                                          "rows_copied"}));
}

// ── Verify: which mechanism failed, and the reads in the order that makes it decidable ───────────

TEST(Verify, ACompleteCopyMatchesAndCountsTheChunksBelowTheMarker) {
  Fixture fixture(25);
  (void)fixture.backfill(10);
  const sde::VerifyReport report = fixture.verify(10);
  EXPECT_TRUE(report.matched());
  EXPECT_EQ(report.chunks_compared, 3);
  EXPECT_EQ(report.chunks_mismatched, 0);
  EXPECT_EQ(report.tail_rows_read, 0);
  EXPECT_EQ(report.rows_source, 25);
  EXPECT_EQ(report.rows_target, 25);
}

TEST(Verify, ARowMissingBelowTheMarkerSaysTheBackfillDidNotCopyIt) {
  Fixture fixture(20);
  (void)fixture.backfill(10);
  fixture.drop_from_copy(3);
  const sde::VerifyReport report = fixture.verify(10);
  EXPECT_FALSE(report.matched());
  EXPECT_EQ(report.chunks_mismatched, 1);
  EXPECT_EQ(report.tail_rows_missing_in_target, 0);
  EXPECT_TRUE(contains(report.for_a_human(), "the backfill did not copy these"));
}

TEST(Verify, AWriteTheFanOutLostIsCaughtAboveTheMarker) {
  Fixture fixture(10);
  (void)fixture.backfill(10);
  fixture.session->save("Reading", Fixture::row(11));
  EXPECT_EQ(fixture.target.count("reading_copy"), 11U);
  fixture.drop_from_copy(11);
  const sde::VerifyReport report = fixture.verify(10);
  EXPECT_FALSE(report.matched());
  EXPECT_EQ(report.chunks_mismatched, 0);
  EXPECT_EQ(report.tail_rows_read, 1);
  EXPECT_EQ(report.tail_rows_missing_in_target, 1);
  EXPECT_TRUE(report.differences.at(0).absent());
  EXPECT_TRUE(contains(report.for_a_human(), "the dual-write fan-out did not reach these"));
}

TEST(Verify, ALostFanOutBelowTheFinalMarkerIsStillCaughtOneMechanismOver) {
  Fixture fixture(10);
  (void)fixture.backfill(5, 1);
  fixture.session->save("Reading", Fixture::row(-1));
  fixture.drop_from_copy(-1);
  EXPECT_TRUE(fixture.backfill(5).complete());
  const sde::VerifyReport report = fixture.verify(5);
  EXPECT_FALSE(report.matched());
  EXPECT_EQ(report.chunks_mismatched, 1);
  EXPECT_EQ(report.tail_rows_missing_in_target, 0);
  EXPECT_EQ(report.differences.at(0).key, (sde::Row{{"id", std::int64_t{-1}}}));
}

TEST(Verify, ARowPresentWithAnotherValueNamesTheColumn) {
  Fixture fixture(3);
  (void)fixture.backfill(3);
  for (sde::Row& row : fixture.target.tables["reading_copy"]) {
    if (row.at("id") == sde::Value(std::int64_t{2})) row["station"] = std::string("somewhere-else");
  }
  const sde::VerifyReport report = fixture.verify(3);
  EXPECT_FALSE(report.matched());
  EXPECT_EQ(report.differences.at(0).columns, std::vector<std::string>{"station"});
  EXPECT_FALSE(report.differences[0].absent());
}

TEST(Verify, ReadsTheSourceWindowBeforeTheTargetWindowAndBoundsBoth) {
  // A write is in the source before it is in the copy, so reading the copy first would find rows
  // legitimately in flight and stop a healthy migration.
  Fixture fixture(4);
  (void)fixture.backfill(2);
  fixture.calls.clear();
  fixture.target.range_sizes.clear();
  (void)fixture.verify(2);
  std::vector<std::string> windows;
  for (const std::string& call : fixture.calls) {
    if (call.ends_with(".key_range")) windows.push_back(call);
  }
  EXPECT_EQ(windows, (std::vector<std::string>{"pg.key_range", "ch.key_range", "pg.key_range",
                                               "ch.key_range", "pg.key_range"}));
  EXPECT_LE(*std::max_element(fixture.target.range_sizes.begin(), fixture.target.range_sizes.end()),
            2U);
}

TEST(Verify, ARowTheWindowMissedIsLookedUpOnceMoreBeforeItIsCalledLost) {
  Fixture fixture(4);
  (void)fixture.backfill(4);
  fixture.target.hide_from_range.insert(Key{std::int64_t{2}});
  const sde::VerifyReport report = fixture.verify(4);
  EXPECT_TRUE(report.matched()) << "present, just not in the window read";
  EXPECT_EQ(report.chunks_mismatched, 0);
}

TEST(Verify, ExtraRowsInTheTargetAreReportedAndNotGatedOn) {
  Fixture fixture(5);
  (void)fixture.backfill(5);
  fixture.target.insert("reading_copy",
                        sde::Row{{"id", std::int64_t{99}}, {"station", "left over"}});
  const sde::VerifyReport report = fixture.verify(5);
  EXPECT_TRUE(report.matched());
  EXPECT_EQ(report.rows_target, 6);
  EXPECT_EQ(report.rows_source, 5);
  EXPECT_TRUE(contains(report.for_a_human(), "reported, not gated on"));
}

TEST(Verify, KeepsTwentyDifferencesAndCountsTheRest) {
  Fixture fixture(0);
  for (int n = 1; n < 40; ++n) fixture.source.insert("reading", Fixture::row(n));
  const sde::VerifyReport report = fixture.verify(50);
  EXPECT_EQ(report.differences.size(), 20U);
  EXPECT_EQ(report.differences_suppressed, 19);
  EXPECT_TRUE(contains(report.for_a_human(), "and 19 more"));
}

TEST(Verify, AMarkerIsTheTargetsAndTheEntitys) {
  Fixture fixture(4);
  (void)fixture.backfill(4);
  EXPECT_EQ(fixture.target.backfill_marker("copy@ch", "Reading"), 4);
  EXPECT_EQ(fixture.target.backfill_marker("copy@ch", "Other"), 0);
  EXPECT_EQ(fixture.target.backfill_marker("elsewhere", "Reading"), 0);
}

TEST(Verify, TheChunkBoundaryLandsOnTheMarkerAndNotPastIt) {
  // With the marker at 5 and chunks of 10, the first read stops at 5; otherwise the three rows
  // above the marker count against the chunks, which reverses the attribution.
  Fixture fixture(8);
  (void)fixture.backfill(5, 1);
  const sde::VerifyReport report = fixture.verify(10);
  EXPECT_EQ(report.chunks_compared, 1);
  EXPECT_EQ(report.chunks_mismatched, 0);
  EXPECT_EQ(report.tail_rows_read, 3);
  EXPECT_EQ(report.tail_rows_missing_in_target, 3) << "not copied and not fanned out - both true";
}

TEST(Verify, AnExtraColumnInTheCopyDoesNotMakeEveryRowDiffer) {
  Fixture fixture(3);
  (void)fixture.backfill(3);
  for (sde::Row& row : fixture.target.tables["reading_copy"]) {
    row["added_outside_sde"] = std::string("whatever");
  }
  const sde::VerifyReport report = fixture.verify(3);
  EXPECT_TRUE(report.matched()) << report.for_a_human();
}

TEST(Verify, AColumnAbsentFromTheCopyIsNotReadAsAStoredNull) {
  Fixture fixture(0);
  fixture.source.insert("reading", sde::Row{{"id", std::int64_t{1}}, {"station", sde::Null{}}});
  (void)fixture.backfill(2);
  for (sde::Row& row : fixture.target.tables["reading_copy"]) row.erase("station");
  const sde::VerifyReport report = fixture.verify(2);
  EXPECT_FALSE(report.matched());
  EXPECT_EQ(report.differences.at(0).columns, std::vector<std::string>{"station"});
}

// ── A verification request ──────────────────────────────────────────────────────────────────────

constexpr const char* kProject = "11111111111111111111111111111111";
constexpr const char* kRequestId = "22222222222222222222222222222222";

TEST(VerifyRequest, NamesTheSessionsModelAndNotOnlyTheMaps) {
  // A map loaded without a model is checked against nothing, and below contract 4 a session does
  // not compare the two versions: the request agrees with the map, and the model is another one.
  const sde::Model model = readings();
  const std::string text = R"({"contract": 3, "model_version": "0123456789abcdef", "map_version": 1,
      "groups": {"Reading": {
        "source": {"id": "src@pg", "engine": "pg",
                   "layout": {"tables": {"Reading": "reading"}, "columns": )" +
                           std::string(kColumns) + R"(}},
        "derived": [{"id": "copy@ch", "engine": "ch", "lag_budget_ms": 1000,
                     "layout": {"tables": {"Reading": "reading_copy"}, "columns": )" +
                           std::string(kColumns) + R"(}}],
        "also_write": ["copy@ch"]}}})";
  const sde::PlacementMap map = sde::load_map(std::string_view(text));
  ASSERT_NE(map.model_version(), model.version());
  Store source("pg", {{"reading", {"id"}}}, nullptr);
  Store target("ch", {{"reading_copy", {"id"}}}, nullptr);
  sde::SessionOptions options;
  options.project_id = kProject;
  sde::Session session(model, map, {{"pg", &source}, {"ch", &target}}, options);
  sde::VerifyOptions verify;
  verify.request = sde::verification_request(map, "Reading", kProject, kRequestId,
                                             "2026-10-07T00:00:00+00:00");
  EXPECT_EQ(refusal([&] { (void)sde::verify(session, "Reading", verify); }),
            "verification request names another session model");
}

TEST(VerifyRequest, AComparisonWithoutATimeIsStampedAndStillNotBeforeItsRequest) {
  // With no time given, the report is stamped when it is made; a request from the future - a
  // control plane whose clock is ahead - is refused then, after the comparison, not before it.
  sde::SessionOptions options;
  options.project_id = kProject;
  const sde::Model model = readings();
  const sde::PlacementMap map = placed(model);
  Store source("pg", {{"reading", {"id"}}}, nullptr);
  Store target("ch", {{"reading_copy", {"id"}}}, nullptr);
  sde::Session session(model, map, {{"pg", &source}, {"ch", &target}}, options);
  sde::VerifyOptions verify;
  verify.request = sde::verification_request(map, "Reading", kProject, kRequestId,
                                             "2999-01-01T00:00:00+00:00");
  EXPECT_TRUE(contains(refusal([&] { (void)sde::verify(session, "Reading", verify); }),
                       "verification predates its request"));
  verify.request = sde::verification_request(map, "Reading", kProject, kRequestId,
                                             "2026-01-01T00:00:00+00:00");
  const sde::VerifyReport report = sde::verify(session, "Reading", verify);
  EXPECT_TRUE(report.matched());
  EXPECT_TRUE(report.at.ends_with("+00:00")) << report.at;
  EXPECT_EQ(report.request, verify.request);
}

// ── The comparison under barriers ───────────────────────────────────────────────────────────────

constexpr const char* kHold = "66666666666666666666666666666666";

/// An in-memory engine whose first window read runs a hook: what an operator's comparison has to
/// survive is something happening to a table while its rows are being read.
class Interrupted final : public sde::Engine,
                          public sde::Migratable,
                          public sde::Fencable,
                          public sde::SchemaValidator {
 public:
  explicit Interrupted(sde::testing::MemoryEngine& inner) : inner_(inner) {}
  std::function<void()> on_first_read;
  /// The keys each schema check was asked with; a refusal to give when set.
  std::vector<sde::Keys> checked;
  std::optional<std::string> schema_differs;

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
    sde::Capabilities offered;
    offered.migration = this;
    offered.fences = this;
    offered.schema = this;
    return offered;
  }
  std::vector<sde::Row> key_range(const std::string& table, const std::vector<std::string>& order,
                                  const std::optional<Key>& after, const std::optional<Key>& upto,
                                  std::optional<std::size_t> limit) override {
    if (on_first_read) std::exchange(on_first_read, nullptr)();
    return inner_.key_range(table, order, after, upto, limit);
  }
  std::optional<Key> nth_key(const std::string& table, const std::vector<std::string>& order,
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
  sde::WriteFence write_fence(const std::string& table, const std::string& project_id) override {
    return inner_.write_fence(table, project_id);
  }
  std::vector<sde::PhysicalFinding> validate_schema(const sde::PhysicalLayout& layout,
                                                    const sde::Keys& keys) override {
    checked.push_back(keys);
    if (schema_differs) throw sde::EngineError(*schema_differs);
    return inner_.validate_schema(layout, keys);
  }

 private:
  sde::testing::MemoryEngine& inner_;
};

sde::testing::MemoryEngineOptions named(std::string name) {
  sde::testing::MemoryEngineOptions options;
  options.name = std::move(name);
  return options;
}

/// A contract-4 map moving Event from `pg` to a copy in `ch`, both at generation 1, each table
/// fenced in memory, and one row in each.
struct Frozen {
  Frozen()
      : model(sde::load_neutral_model(R"({"entities": [{"name": "Event", "fields": [
          {"name": "id", "type": "int64"}, {"name": "value", "type": "int64"}],
          "key": ["id"]}]})")),
        map([this] {
          const std::string columns = R"({"Event": {"id": "bigint", "value": "bigint"}})";
          const std::string text = R"({"contract": 4, "project_id": ")" + std::string(kProject) +
                                   R"(", "model_version": ")" + model.version() +
                                   R"(", "map_version": 1, "groups": {"Event": {"write_epoch": 1,
              "source": {"id": "source", "engine": "pg",
                         "layout": {"tables": {"Event": "source_events"}, "columns": )" +
                                   columns + R"(}},
              "derived": [{"id": "copy", "engine": "ch", "lag_budget_ms": 1000,
                           "layout": {"tables": {"Event": "copy_events"}, "columns": )" +
                                   columns + R"(}}],
              "also_write": ["copy"]}}})";
          sde::LoadOptions options;
          options.model = &model;
          return sde::load_map(std::string_view(text), options);
        }()),
        source_watch(pg),
        copy_watch(ch) {
    const sde::Row row{{"id", std::int64_t{1}},
                       {"value", std::int64_t{11}},
                       {std::string(sde::EPOCH_COLUMN), std::int64_t{1}}};
    pg.tables["source_events"].push_back(row);
    ch.tables["copy_events"].push_back(row);
    fence(pg, "source_events");
    fence(ch, "copy_events");
  }

  static void fence(sde::testing::MemoryEngine& engine, const std::string& table) {
    auto backend = std::make_shared<sde::testing::MemoryFences>();
    backend->identity = table;
    backend->column = sde::ColumnState::valid;
    const std::string prefix(sde::FENCE_PREFIX);
    const std::string column(sde::EPOCH_COLUMN);
    backend->constraints[prefix + "owner_" + kProject] = "1";
    backend->constraints[prefix + "min_1"] = column + " >= 1";
    backend->constraints[prefix + "max_1"] = column + " <= 1";
    engine.bind_fences({{table, backend}});
  }

  sde::FrozenVerifyReport compare(const std::string& hold = kHold) {
    const sde::InspectionContext context(model, map, {{"pg", &source_watch}, {"ch", &copy_watch}},
                                         kProject);
    const sde::VerificationRequest request = sde::verification_request(
        map, "Event", kProject, kRequestId, "2026-09-12T12:00:00Z");
    sde::FrozenOptions options;
    options.at = "2026-09-12T12:00:01Z";
    return sde::verify_frozen(context, "Event", request, hold, {{"source", 1}, {"copy", 1}},
                              options);
  }
  std::vector<std::string> holds(sde::testing::MemoryEngine& engine, const std::string& table) {
    return engine.write_fence(table, kProject).state().holds;
  }

  sde::Model model;
  sde::PlacementMap map;
  sde::testing::MemoryEngine pg{named("pg")};
  sde::testing::MemoryEngine ch{named("ch")};
  Interrupted source_watch;
  Interrupted copy_watch;
};

TEST(FrozenComparison, HoldsEveryTableAndMatchesAnEqualCopy) {
  Frozen frozen;
  const sde::FrozenVerifyReport report = frozen.compare();
  EXPECT_TRUE(report.matched());
  EXPECT_EQ(frozen.holds(frozen.pg, "source_events"), std::vector<std::string>{kHold});
  EXPECT_EQ(frozen.holds(frozen.ch, "copy_events"), std::vector<std::string>{kHold});
}

TEST(FrozenComparison, ABarrierIdIsThirtyTwoLowercaseHexadecimalDigits) {
  Frozen frozen;
  for (const std::string& hold : {std::string(31, '6'), std::string(32, 'A'), std::string()}) {
    EXPECT_EQ(refusal([&] { (void)frozen.compare(hold); }),
              "a frozen comparison hold id must be 32 lowercase hexadecimal digits")
        << hold;
  }
  EXPECT_TRUE(frozen.holds(frozen.pg, "source_events").empty());
}

TEST(FrozenComparison, ARetiredBarrierIdIsRefusedBeforeAnyTableIsClosed) {
  Frozen frozen;
  sde::WriteFence source = frozen.pg.write_fence("source_events", kProject);
  (void)source.freeze(kHold);
  (void)source.release(kHold);
  EXPECT_EQ(refusal([&] { (void)frozen.compare(); }),
            "a frozen comparison cannot reuse a retired barrier id");
  EXPECT_TRUE(frozen.holds(frozen.pg, "source_events").empty());
  EXPECT_TRUE(frozen.holds(frozen.ch, "copy_events").empty());
}

TEST(FrozenComparison, ABarrierReleasedDuringTheComparisonInvalidatesIt) {
  // The rows were compared while a writer could reach the source again: whatever they said, they
  // are not evidence that the two tables were equal under the barrier.
  Frozen frozen;
  frozen.source_watch.on_first_read = [&] {
    (void)frozen.pg.write_fence("source_events", kProject).release(kHold);
  };
  EXPECT_EQ(refusal([&] { (void)frozen.compare(); }),
            "a frozen comparison lost its named barrier or write generation");
}

TEST(FrozenComparison, AnOperatorInspectsOnlyAGenerationBearingMap) {
  // Below contract 4 there is no generation to hold a table at, so there is nothing a barrier could
  // prove about its writers.
  const sde::Model model = readings();
  const sde::PlacementMap map = placed(model);
  Store source("pg", {{"reading", {"id"}}}, nullptr);
  Store target("ch", {{"reading_copy", {"id"}}}, nullptr);
  EXPECT_EQ(refusal([&] {
              (void)sde::InspectionContext(model, map, {{"pg", &source}, {"ch", &target}},
                                           kProject);
            }),
            "operator inspection requires a generation-bearing placement map");
}

TEST(FrozenComparison, AnOperatorNamesTheEnginesItWasNotGiven) {
  Frozen frozen;
  EXPECT_EQ(refusal([&] {
              (void)sde::InspectionContext(frozen.model, frozen.map, {{"pg", &frozen.source_watch}},
                                           kProject);
            }),
            "operator inspection is missing engines ['ch']");
}

TEST(FrozenComparison, AnOperatorInspectsWithTheModelItsMapNames) {
  Frozen frozen;
  const sde::Model other = readings();
  EXPECT_EQ(refusal([&] {
              (void)sde::InspectionContext(
                  other, frozen.map, {{"pg", &frozen.source_watch}, {"ch", &frozen.copy_watch}},
                  kProject);
            }),
            "operator inspection needs the model named by its map");
}

TEST(FrozenComparison, AGroupWithoutAGenerationIsOutOfAnOperatorsReach) {
  // Contract 6: the orderbook group has no generation, so no operator acts on it and none needs its
  // engine - only the fenced group's engines are asked for.
  const sde::Model model = sde::load_neutral_model(R"({"entities": [
      {"name": "Event", "fields": [{"name": "id", "type": "int64"}], "key": ["id"]},
      {"name": "Book", "fields": [{"name": "symbol", "type": "string"}], "key": ["symbol"]}]})");
  const std::string text = R"({"contract": 6, "project_id": ")" + std::string(kProject) +
                           R"(", "model_version": ")" + model.version() + R"(", "map_version": 1,
      "groups": {
        "Event": {"write_epoch": 1, "source": {"id": "source", "engine": "pg",
                  "layout": {"tables": {"Event": "events"},
                             "columns": {"Event": {"id": "bigint"}}}}},
        "Book": {"source": {"id": "book", "engine": "ob",
                 "layout": {"tables": {"Book": "book"},
                            "columns": {"Book": {"symbol": "text"}}}}}}})";
  sde::LoadOptions options;
  options.model = &model;
  const sde::PlacementMap map = sde::load_map(std::string_view(text), options);
  sde::testing::MemoryEngine pg;
  const sde::InspectionContext context(model, map, {{"pg", &pg}}, kProject);
  EXPECT_EQ(context.engines().size(), 1U);
}

TEST(FrozenComparison, ATableThatIsNotTheMapsIsRefusedBeforeAnyIsClosed) {
  // The columns are checked, and only the columns: a comparison under barriers needs the tables
  // the map describes, and reads nothing of their physical design, as in the reference.
  Frozen frozen;
  frozen.copy_watch.schema_differs = "copy_events already existed with a different shape";
  EXPECT_THROW((void)frozen.compare(), sde::EngineError);
  EXPECT_TRUE(frozen.holds(frozen.pg, "source_events").empty());
  EXPECT_TRUE(frozen.holds(frozen.ch, "copy_events").empty());
  frozen.copy_watch.schema_differs.reset();
  (void)frozen.compare();
  ASSERT_FALSE(frozen.source_watch.checked.empty());
  EXPECT_TRUE(frozen.source_watch.checked.back().empty());
  EXPECT_TRUE(frozen.copy_watch.checked.back().empty());
}

}  // namespace
