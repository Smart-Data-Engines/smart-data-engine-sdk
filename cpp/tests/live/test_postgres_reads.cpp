/// Logical reads against a real PostgreSQL: pages in one portable order that keep every tie across
/// page boundaries, counts and exact summaries over the values the server holds, and the plans of
/// the statements - the primary key and a declared index on text serve the reads, which compare
/// text in the reads' collation. Ported from the reference's `test_query_live.py` and the PostgreSQL
/// half of `test_index_use_live.py`.
///
///     SDE_POSTGRES_DSN=postgresql://postgres:sde@127.0.0.1:55432/sde ctest -L live

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <functional>
#include <limits>
#include <map>
#include <memory>
#include <optional>
#include <set>
#include <string>
#include <utility>
#include <variant>
#include <vector>

#include <gtest/gtest.h>

#include "engines/postgres/values.hpp"
#include "live/live.hpp"
#include "live/postgres.hpp"
#include "sde/canonical.hpp"
#include "sde/errors.hpp"
#include "sde/hashing.hpp"
#include "sde/json.hpp"
#include "sde/model.hpp"
#include "sde/placement.hpp"
#include "sde/postgres.hpp"
#include "sde/provisioning.hpp"
#include "sde/query.hpp"
#include "sde/session.hpp"
#include "sde/telemetry.hpp"

namespace {

using sde::live::Admin;
using sde::live::Roles;
using sde::live::Scope;

const std::string kProject = "11111111111111111111111111111111";

/// A contract-3 map of every group of `model` in engine `db`, each in its auto layout: the
/// reference's `default_layout`, written out by the loader. With `indexes`, the layouts the loader
/// wrote out are loaded again with each index in its entity's group, since an auto layout carries
/// no design.
sde::PlacementMap auto_map(const sde::Model& model, const std::string& indexes = "") {
  const std::string head = R"({"contract": 3, "model_version": ")" + model.version() +
                           R"(", "map_version": 1, "groups": {)";
  std::string groups;
  for (const sde::Group& group : model.groups()) {
    groups += (groups.empty() ? "\"" : ", \"") + group.name + R"(": {"source": {"id": ")" +
              group.name + R"(@db", "engine": "db", "layout": {"auto": true}}})";
  }
  sde::LoadOptions options;
  options.model = &model;
  sde::PlacementMap loaded = sde::load_map(head + groups + "}}", options);
  if (indexes.empty()) return loaded;
  const sde::Json declared = sde::parse_json(indexes);
  sde::Json written = sde::Json::object();
  for (const auto& [name, spot] : loaded.groups()) {
    const sde::PhysicalLayout& layout = spot.source.layout;
    sde::Json raw = sde::Json::object();
    sde::Json tables = sde::Json::object();
    for (const auto& [entity, table] : layout.tables) tables.set(entity, table);
    raw.set("tables", std::move(tables));
    sde::Json columns = sde::Json::object();
    for (const auto& [entity, types] : layout.columns) {
      sde::Json typed = sde::Json::object();
      for (const auto& [column, type] : types) typed.set(column, type);
      columns.set(entity, std::move(typed));
    }
    raw.set("columns", std::move(columns));
    sde::Json mine = sde::Json::array();
    for (const sde::Json& index : declared.as_array()) {
      if (layout.tables.count(index.find("entity")->as_string()) != 0) {
        mine.as_array().push_back(index);
      }
    }
    if (!mine.as_array().empty()) raw.set("indexes", std::move(mine));
    sde::Json source = sde::Json::object();
    source.set("id", name + "@db");
    source.set("engine", "db");
    source.set("layout", std::move(raw));
    sde::Json group = sde::Json::object();
    group.set("source", std::move(source));
    written.set(name, std::move(group));
  }
  sde::Json document = sde::parse_json(head + "}}");
  document.set("groups", std::move(written));
  return sde::load_map(document, options);
}

/// The PostgreSQL engine with every read plan it is handed recorded, so that a test renders the
/// very statement the adapter ran and asks the planner about it: the reference's spy on `read_sql`.
class Spy final : public sde::Engine, public sde::Queryable, public sde::Countable {
 public:
  struct Read {
    std::string table;
    sde::ReadPlan plan;
    bool count;
  };

  explicit Spy(sde::PostgresEngine& inner) : inner_(inner) {}
  std::vector<Read> reads;

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
    offered.query = this;
    offered.count = this;
    return offered;
  }
  std::vector<sde::Row> select_rows(const std::string& table, const sde::ReadPlan& plan) override {
    reads.push_back({table, plan, false});
    return inner_.select_rows(table, plan);
  }
  std::uint64_t count_rows(const std::string& table, const sde::ReadPlan& plan) override {
    reads.push_back({table, plan, true});
    return inner_.count_rows(table, plan);
  }

 private:
  sde::PostgresEngine& inner_;
};

/// The plan's scans, `Node Type[:index][ on condition]`, with sequential scans off. A planner that
/// cannot use an index for the predicate still reads one then - whole, filtering every entry - so an
/// index's name proves nothing; the predicate as its `Index Cond` does.
std::vector<std::string> scans(Admin& admin, const Spy::Read& read) {
  std::vector<std::optional<std::string>> values;
  const std::string statement = sde::read_sql(
      read.table, read.plan, "postgres",
      [&](const sde::Value& value) {
        values.push_back(sde::detail::postgres::parameter_text(value));
        return "$" + std::to_string(values.size());
      },
      read.count);
  (void)admin.run("SET enable_seqscan = off");
  const Admin::Rows plan = admin.rows("EXPLAIN (FORMAT JSON) " + statement, values);
  (void)admin.run("RESET enable_seqscan");
  std::vector<std::string> out;
  const std::function<void(const sde::Json&)> walk = [&](const sde::Json& node) {
    const std::string& type = node.find("Node Type")->as_string();
    if (type.find("Scan") != std::string::npos) {
      std::string label = type;
      if (const sde::Json* index = node.find("Index Name")) label += ":" + index->as_string();
      if (const sde::Json* condition = node.find("Index Cond");
          condition != nullptr && !condition->as_string().empty()) {
        label += " on condition";
      }
      out.push_back(label);
    }
    if (const sde::Json* children = node.find("Plans")) {
      for (const sde::Json& child : children->as_array()) walk(child);
    }
  };
  walk(*sde::parse_json(*plan.at(0).at(0)).as_array().at(0).find("Plan"));
  return out;
}

bool has(const std::vector<std::string>& scans, const std::string& suffix) {
  return std::any_of(scans.begin(), scans.end(),
                     [&](const std::string& scan) { return scan.ends_with(suffix); });
}

std::string listed(const std::vector<std::string>& scans) {
  std::string out;
  for (const std::string& scan : scans) out += (out.empty() ? "" : ", ") + scan;
  return out;
}

class PostgresReads : public ::testing::Test {
 protected:
  void SetUp() override { SDE_REQUIRE_DSN("SDE_POSTGRES_DSN", dsn_); }

  static std::unique_ptr<sde::PostgresEngine> engine(const std::string& dsn) {
    auto made = std::make_unique<sde::PostgresEngine>(dsn);
    made->connect();
    return made;
  }

  std::string dsn_;
};

// --- pages, counts and summaries ----------------------------------------------------------------

const std::vector<std::string> kIds = {
    "00000000-0000-0001-0000-000000000000", "00000000-0000-0000-ffff-ffffffffffff",
    "ffffffff-ffff-ffff-0000-000000000000", "00000000-0000-0000-0000-000000000001",
    "00000000-0000-0000-0000-000000000002", "00000000-0000-0000-0000-000000000003"};

sde::Value at(int micros_after_base) {
  return *sde::TimestampTz::parse("2026-09-14T00:00:00." + std::to_string(123456 + micros_after_base) +
                                  "Z");
}

/// The reference's fixture: six events whose ids sort differently as text and as numbers, labels
/// with two nulls and a tie, times in microsecond ties, amounts at scale 2 - saved through a
/// session on the runtime login, telemetry on, the saving window rolled away.
struct Events {
  Roles roles;
  sde::Model declared;
  /// The model the map and the engine speak, and the names the application keeps: the declared
  /// model and no names, or its hashed form and the map between the two.
  std::pair<sde::Model, std::optional<sde::NameMap>> spoken;
  sde::PlacementMap map;
  std::unique_ptr<sde::PostgresEngine> provisioning;
  std::unique_ptr<sde::PostgresEngine> runtime;
  std::unique_ptr<sde::Recorder> recorder;
  std::unique_ptr<sde::Session> session;
  std::vector<sde::Row> rows;

  static std::pair<sde::Model, std::optional<sde::NameMap>> speak(const sde::Model& model,
                                                                 bool hash) {
    if (!hash) return {model, std::nullopt};
    auto [hashed, names] = sde::hash_identifiers(model, std::string(32, 'q'));
    return {std::move(hashed), std::move(names)};
  }

  Events(const std::string& dsn, bool hash)
      : roles(dsn),
        declared(sde::load_neutral_model(R"json({"entities": [{"name": "Event", "fields": [
            {"name": "id", "type": "uuid"}, {"name": "label", "type": "string", "nullable": true},
            {"name": "at", "type": "timestamptz"}, {"name": "amount", "type": "decimal(12,2)"}],
            "key": ["id"]}]})json")),
        spoken(speak(declared, hash)),
        map(auto_map(spoken.first)) {
    const sde::Model& model = spoken.first;
    const std::string entity = model.entities().at(0).name;
    const sde::PhysicalLayout& layout = map.placement_of(entity).source.layout;
    provisioning = std::make_unique<sde::PostgresEngine>(roles.operator_dsn());
    provisioning->connect();
    (void)provisioning->ensure_schema(layout, {{entity, model.entity(entity).key}});
    roles.grant(layout.table_for(entity));
    runtime = std::make_unique<sde::PostgresEngine>(roles.runtime_dsn());
    runtime->connect();
    recorder = std::make_unique<sde::Recorder>(model);
    sde::SessionOptions options;
    options.recorder = recorder.get();
    if (spoken.second) options.names = &*spoken.second;
    session = std::make_unique<sde::Session>(
        model, map, std::map<std::string, sde::Engine*>{{"db", runtime.get()}}, options);
    const std::vector<std::optional<std::string>> labels = {"z", std::nullopt, "é", "a",
                                                            std::nullopt, "a"};
    for (std::size_t i = 0; i < kIds.size(); ++i) {
      rows.push_back(sde::Row{
          {"id", *sde::Uuid::parse(kIds[i])},
          {"label", labels[i] ? sde::Value(*labels[i]) : sde::Value(sde::Null{})},
          {"at", at(static_cast<int>(i / 2))},
          {"amount", sde::Decimal(std::to_string(i) + ".25")}});
    }
    session->save_many("Event", rows);
    (void)recorder->roll();
  }

  [[nodiscard]] const sde::Model& model() const noexcept { return spoken.first; }

  /// Every page of a scan, following each page's position; at most twenty of them.
  std::vector<sde::Row> pages(sde::ScanOptions options) {
    std::vector<sde::Row> out;
    for (int page = 0; page < 20; ++page) {
      const sde::ScanPage found = session->scan("Event", options);
      EXPECT_LE(static_cast<std::int64_t>(found.rows.size()), options.limit.rows());
      out.insert(out.end(), found.rows.begin(), found.rows.end());
      if (!found.next_after) return out;
      options.after = found.next_after;
    }
    ADD_FAILURE() << "pagination did not end";
    return out;
  }
};

/// Whether `left` sorts before `right` in the order the reads promise: text by code point (UTF-8
/// bytes), a UUID by its bytes, an instant in time, a decimal by value.
bool before(const sde::Value& left, const sde::Value& right) {
  if (const auto* text = std::get_if<std::string>(&left)) return *text < std::get<std::string>(right);
  if (const auto* id = std::get_if<sde::Uuid>(&left)) {
    return id->to_string() < std::get<sde::Uuid>(right).to_string();
  }
  if (const auto* instant = std::get_if<sde::TimestampTz>(&left)) {
    return instant->micros() < std::get<sde::TimestampTz>(right).micros();
  }
  if (const auto* amount = std::get_if<sde::Decimal>(&left)) {
    return *amount < std::get<sde::Decimal>(right);
  }
  ADD_FAILURE() << "no order for this value here";
  return false;
}

/// Sorted by these fields, in the order the reads promise.
std::vector<sde::Row> sorted(std::vector<sde::Row> rows, const std::vector<std::string>& fields,
                             bool descending = false) {
  std::stable_sort(rows.begin(), rows.end(), [&](const sde::Row& left, const sde::Row& right) {
    for (const std::string& field : fields) {
      if (left.at(field) == right.at(field)) continue;
      return descending ? before(right.at(field), left.at(field))
                        : before(left.at(field), right.at(field));
    }
    return false;
  });
  return rows;
}

TEST_F(PostgresReads, UuidPagesUseOnePortableOrderAndNeverDropTheSentinel) {
  Events events(dsn_, false);
  for (const int size : {1, 2, 3, 6}) {
    SCOPED_TRACE("pages of " + std::to_string(size));
    sde::ScanOptions options;
    options.limit = size;
    EXPECT_EQ(events.pages(options), sorted(events.rows, {"id"}));
    options.descending = true;
    EXPECT_EQ(events.pages(options), sorted(events.rows, {"id"}, true));
  }
}

TEST_F(PostgresReads, NullableSortTiesKeepNullsLastInBothDirections) {
  for (const bool hashed : {false, true}) {
    SCOPED_TRACE(hashed ? "hashed names" : "the client's names");
    Events events(dsn_, hashed);
    for (const bool descending : {false, true}) {
      std::vector<sde::Row> present;
      std::vector<sde::Row> absent;
      for (const sde::Row& row : events.rows) {
        (std::holds_alternative<sde::Null>(row.at("label")) ? absent : present).push_back(row);
      }
      std::vector<sde::Row> expected = sorted(present, {"label", "id"}, descending);
      for (const sde::Row& row : sorted(absent, {"id"}, descending)) expected.push_back(row);
      sde::ScanOptions options;
      options.order_by = "label";
      options.descending = descending;
      options.limit = 1;
      EXPECT_EQ(events.pages(options), expected) << (descending ? "descending" : "ascending");
    }
    sde::CountOptions nulls;
    nulls.where = sde::Row{{"label", sde::Null{}}};
    EXPECT_EQ(events.session->count("Event", nulls), 2U);
  }
}

TEST_F(PostgresReads, AHalfOpenTimeRangeWithEqualityAndMicrosecondTies) {
  Events events(dsn_, false);
  const sde::Range bounds{"at", at(0), at(3)};
  std::vector<sde::Row> wanted;
  for (const sde::Row& row : events.rows) {
    if (row.at("label") == sde::Value(std::string("a")) && !before(row.at("at"), at(0)) &&
        before(row.at("at"), at(3))) {
      wanted.push_back(row);
    }
  }
  ASSERT_EQ(wanted.size(), 2U);
  sde::ScanOptions options;
  options.where = sde::Row{{"label", std::string("a")}};
  options.bounds = bounds;
  options.order_by = "at";
  options.limit = 1;
  EXPECT_EQ(events.pages(options), sorted(wanted, {"at", "id"}));
  sde::CountOptions counted;
  counted.where = options.where;
  counted.bounds = bounds;
  EXPECT_EQ(events.session->count("Event", counted), wanted.size());

  const std::optional<sde::Window> window = events.recorder->roll();
  ASSERT_TRUE(window.has_value());
  std::set<std::string> kinds;
  for (const sde::ShapeStats& stats : window->shapes) kinds.insert(stats.kind);
  EXPECT_EQ(kinds, (std::set<std::string>{"range_read", "aggregate"}));
  for (const sde::ShapeStats& stats : window->shapes) {
    // What each read filtered on, by name: the equality field and the bounded one.
    const std::map<sde::Predicates, std::uint64_t> each{{sde::Predicates{{"label"}, "at"},
                                                         stats.calls}};
    EXPECT_EQ(stats.filtered, each) << stats.kind;
    if (stats.kind == "aggregate") {
      EXPECT_EQ(stats.rows, 1U);
    }
  }
}

TEST_F(PostgresReads, FiltersAreRecordedInTheModelsVocabularyWhenNamesAreHashed) {
  Events events(dsn_, true);
  sde::CountOptions counted;
  counted.where = sde::Row{{"label", std::string("a")}};
  counted.bounds = sde::Range{"at", at(0), at(3)};
  (void)events.session->count("Event", counted);
  sde::ScanOptions scanned;
  scanned.where = counted.where;
  scanned.limit = 2;
  (void)events.session->scan("Event", scanned);
  const std::optional<sde::Window> window = events.recorder->roll();
  ASSERT_TRUE(window.has_value());
  std::set<std::string> declared;
  for (const sde::Field& field : events.model().entities().at(0).fields) declared.insert(field.name);
  std::set<std::string> recorded;
  for (const sde::ShapeStats& stats : window->shapes) {
    for (const auto& [predicates, calls] : stats.filtered) {
      recorded.insert(predicates.equal.begin(), predicates.equal.end());
      if (!predicates.range.empty()) recorded.insert(predicates.range);
    }
  }
  EXPECT_EQ(recorded.size(), 2U);
  EXPECT_TRUE(std::includes(declared.begin(), declared.end(), recorded.begin(), recorded.end()));
  EXPECT_EQ(recorded.count("label") + recorded.count("at"), 0U);
}

TEST_F(PostgresReads, DecimalBoundsAreNotRoundedToTheStoredScale) {
  Events events(dsn_, false);
  const sde::Range bounds{"amount", sde::Decimal("1.249"), sde::Decimal("3.251")};
  std::vector<sde::Row> expected;
  for (const sde::Row& row : events.rows) {
    const auto& amount = std::get<sde::Decimal>(row.at("amount"));
    if (!(amount < sde::Decimal("1.249")) && amount < sde::Decimal("3.251")) expected.push_back(row);
  }
  sde::ScanOptions options;
  options.bounds = bounds;
  options.order_by = "amount";
  options.limit = 1;
  EXPECT_EQ(events.pages(options), sorted(expected, {"amount", "id"}));
  sde::CountOptions counted;
  counted.bounds = bounds;
  EXPECT_EQ(events.session->count("Event", counted), 3U);
}

TEST_F(PostgresReads, ANumericSummaryIsExactOnEmptyAndFilteredDecimalData) {
  Events events(dsn_, false);
  sde::SummaryOptions filtered;
  filtered.where = sde::Row{{"label", std::string("a")}};
  filtered.mean_scale = 3;
  const sde::NumericSummary result = events.session->summarize("Event", "amount", filtered);
  EXPECT_EQ(result.count, 2U);
  EXPECT_EQ(result.non_null_count, 2U);
  EXPECT_EQ(result.minimum, sde::Decimal("3.25"));
  EXPECT_EQ(result.maximum, sde::Decimal("5.25"));
  EXPECT_EQ(result.total->to_string(), "8.50");
  EXPECT_EQ(result.mean->to_string(), "4.250");
  sde::SummaryOptions nothing;
  nothing.where = sde::Row{{"label", std::string("absent")}};
  const sde::NumericSummary empty = events.session->summarize("Event", "amount", nothing);
  EXPECT_EQ(empty.count, 0U);
  EXPECT_EQ(empty.non_null_count, 0U);
  EXPECT_FALSE(empty.minimum || empty.maximum || empty.total || empty.mean);
  const std::optional<sde::Window> window = events.recorder->roll();
  ASSERT_TRUE(window.has_value());
  EXPECT_EQ(window->shapes.at(0).rows, 2U);
  EXPECT_EQ(sde::dump_json(window->as_record(events.model())).find("8.50"), std::string::npos)
      << "a value reached the telemetry window";
}

TEST_F(PostgresReads, ASummaryCastsInt64BeforeSummingAndKeepsLargeResults) {
  Roles roles(dsn_);
  const sde::Model model = sde::load_neutral_model(R"({"entities": [{"name": "Number", "fields": [
      {"name": "id", "type": "int64"}, {"name": "value", "type": "int64", "nullable": true}],
      "key": ["id"]}]})");
  const sde::PlacementMap map = auto_map(model);
  const auto provisioning = engine(roles.operator_dsn());
  (void)provisioning->ensure_schema(map.placement_of("Number").source.layout, {{"Number", {"id"}}});
  roles.grant("number");
  const auto runtime = engine(roles.runtime_dsn());
  sde::Session session(model, map, {{"db", runtime.get()}});
  constexpr std::int64_t kLargest = std::numeric_limits<std::int64_t>::max();
  session.save_many("Number", {{{"id", std::int64_t{1}}, {"value", kLargest}},
                               {{"id", std::int64_t{2}}, {"value", kLargest}},
                               {{"id", std::int64_t{3}}, {"value", sde::Null{}}}});
  const sde::NumericSummary result = session.summarize("Number", "value");
  EXPECT_EQ(result.total->to_string(), "18446744073709551614");
  EXPECT_EQ(result.mean->to_string(), "9223372036854775807.000000");
  EXPECT_EQ(result.count, 3U);
  EXPECT_EQ(result.non_null_count, 2U);
  sde::SummaryOptions third;
  third.where = sde::Row{{"id", std::int64_t{3}}};
  const sde::NumericSummary empty = session.summarize("Number", "value", third);
  EXPECT_EQ(empty.count, 1U);
  EXPECT_EQ(empty.non_null_count, 0U);
  EXPECT_FALSE(empty.minimum || empty.maximum || empty.total || empty.mean);
}

TEST_F(PostgresReads, AScanProjectsTheModelsFieldsAndARevokedGrantRefusesEveryRead) {
  Events events(dsn_, false);
  Admin(events.roles.operator_dsn()).run("ALTER TABLE \"event\" ADD COLUMN extra text");
  sde::ScanOptions one;
  one.limit = 1;
  const sde::ScanPage page = events.session->scan("Event", one);
  std::set<std::string> fields;
  for (const auto& [name, value] : page.rows.at(0)) fields.insert(name);
  EXPECT_EQ(fields, (std::set<std::string>{"amount", "at", "id", "label"}));
  events.roles.grant("event", true);
  EXPECT_THROW((void)events.session->scan("Event"), sde::EngineError);
  EXPECT_THROW((void)events.session->count("Event"), sde::EngineError);
  EXPECT_THROW((void)events.session->summarize("Event", "amount"), sde::EngineError);
}

TEST_F(PostgresReads, AGenerationColumnIsNotALogicalProjection) {
  Roles roles(dsn_);
  Admin(roles.operator_dsn()).run("CREATE TABLE generations (id bigint PRIMARY KEY)");
  const sde::Model model = sde::load_neutral_model(
      R"({"entities": [{"name": "Record", "fields": [{"name": "id", "type": "int64"}],
                        "key": ["id"]}]})");
  sde::LoadOptions options;
  options.model = &model;
  const sde::PlacementMap map = sde::load_map(
      R"({"contract": 4, "project_id": ")" + kProject + R"(", "model_version": ")" +
          model.version() + R"(", "map_version": 1, "groups": {"Record": {"write_epoch": 1,
          "source": {"id": "Record@db", "engine": "db", "layout": {
          "tables": {"Record": "generations"}, "columns": {"Record": {"id": "bigint"}}}}}}})",
      options);
  const auto provisioning = engine(roles.operator_dsn());
  sde::prepare_schema(model, map, {{"db", provisioning.get()}}, kProject);
  roles.grant("generations");
  const auto runtime = engine(roles.runtime_dsn());
  sde::SessionOptions session_options;
  session_options.project_id = kProject;
  sde::Session session(model, map, {{"db", runtime.get()}}, session_options);
  session.save_many("Record", {{{"id", std::int64_t{1}}}, {{"id", std::int64_t{2}}}});
  sde::ScanOptions one;
  one.limit = 1;
  const sde::ScanPage page = session.scan("Record", one);
  EXPECT_EQ(page.rows, (std::vector<sde::Row>{{{"id", std::int64_t{1}}}}));
  EXPECT_EQ(page.next_after, (sde::Row{{"id", std::int64_t{1}}}));
  EXPECT_EQ(session.count("Record"), 2U);
  EXPECT_EQ(session.summarize("Record", "id").total->to_string(), "3");
}

TEST_F(PostgresReads, TimestampOrderAndBoundsDoNotDependOnTheConnectionsTimeZone) {
  Roles roles(dsn_);
  const sde::Model model = sde::load_neutral_model(R"({"entities": [{"name": "Event", "fields": [
      {"name": "id", "type": "int64"}, {"name": "at", "type": "timestamp"}], "key": ["id"]}]})");
  const sde::PlacementMap map = auto_map(model);
  const auto provisioning = engine(roles.operator_dsn());
  (void)provisioning->ensure_schema(map.placement_of("Event").source.layout, {{"Event", {"id"}}});
  roles.grant("event");
  const sde::Value first_at = *sde::Timestamp::parse("2026-09-14T10:00:00.123456");
  const sde::Value second_at = *sde::Timestamp::parse("2026-09-14T10:00:00.123457");
  provisioning->insert_many("event", {{{"id", std::int64_t{1}}, {"at", first_at}},
                                      {{"id", std::int64_t{2}}, {"at", second_at}}});
  // The runtime's session is in New York; the reads' bounds are instants in other zones.
  const std::string new_york = sde::live::conninfo(
      roles.runtime_dsn(),
      {{"options", "-csearch_path=" + roles.schema() + " -ctimezone=America/New_York"}});
  ASSERT_EQ(Admin(new_york).rows("SHOW TimeZone"), (Admin::Rows{{std::string("America/New_York")}}))
      << "the session is not in the zone this test is about";
  const auto runtime = engine(new_york);
  sde::Session session(model, map, {{"db", runtime.get()}});
  sde::ScanOptions options;
  options.bounds = sde::Range{"at", std::string("2026-09-14T12:00:00.123456+02:00"),
                              std::string("2026-09-14T10:00:00.123458Z")};
  options.order_by = "at";
  options.limit = 1;
  const sde::ScanPage first = session.scan("Event", options);
  EXPECT_EQ(first.rows, (std::vector<sde::Row>{{{"id", std::int64_t{1}}, {"at", first_at}}}));
  sde::ScanOptions next;
  next.order_by = "at";
  next.after = first.next_after;
  EXPECT_EQ(session.scan("Event", next).rows,
            (std::vector<sde::Row>{{{"id", std::int64_t{2}}, {"at", second_at}}}));
}

TEST_F(PostgresReads, AFloatsNonFiniteValuesAreReadAndARangeExcludesNaN) {
  Roles roles(dsn_);
  const sde::Model model = sde::load_neutral_model(R"({"entities": [{"name": "FloatValue",
      "fields": [{"name": "id", "type": "int64"}, {"name": "value", "type": "float64"}],
      "key": ["id"]}]})");
  const sde::PlacementMap map = auto_map(model);
  const auto provisioning = engine(roles.operator_dsn());
  const sde::PhysicalLayout& layout = map.placement_of("FloatValue").source.layout;
  (void)provisioning->ensure_schema(layout, {{"FloatValue", {"id"}}});
  ASSERT_EQ(layout.table_for("FloatValue"), "float_value");
  roles.grant("float_value");
  Admin(roles.operator_dsn())
      .run("INSERT INTO float_value VALUES (1,0.25),(2,'NaN'),(3,'Infinity'),(4,'-Infinity')");
  const auto runtime = engine(roles.runtime_dsn());
  sde::Session session(model, map, {{"db", runtime.get()}});
  const sde::ScanPage page = session.scan("FloatValue");
  ASSERT_EQ(page.rows.size(), 4U);
  std::vector<std::int64_t> ids;
  for (const sde::Row& row : page.rows) ids.push_back(std::get<std::int64_t>(row.at("id")));
  EXPECT_EQ(ids, (std::vector<std::int64_t>{1, 2, 3, 4}));
  EXPECT_EQ(std::get<double>(page.rows[0].at("value")), 0.25);
  EXPECT_TRUE(std::isnan(std::get<double>(page.rows[1].at("value"))));
  EXPECT_EQ(std::get<double>(page.rows[2].at("value")), std::numeric_limits<double>::infinity());
  EXPECT_EQ(std::get<double>(page.rows[3].at("value")), -std::numeric_limits<double>::infinity());
  sde::ScanOptions positive;
  positive.bounds = sde::Range{"value", std::int64_t{0}};
  ids.clear();
  for (const sde::Row& row : session.scan("FloatValue", positive).rows) {
    ids.push_back(std::get<std::int64_t>(row.at("id")));
  }
  EXPECT_EQ(ids, (std::vector<std::int64_t>{1, 3}));
  sde::CountOptions counted;
  counted.bounds = positive.bounds;
  EXPECT_EQ(session.count("FloatValue", counted), 2U);
}

TEST_F(PostgresReads, ADecimalIsReadAtItsDeclaredScale) {
  Roles roles(dsn_);
  const sde::Model model = sde::load_neutral_model(R"json({"entities": [{"name": "Price", "fields": [
      {"name": "id", "type": "int64"}, {"name": "value", "type": "decimal(38,18)"}],
      "key": ["id"]}]})json");
  const sde::PlacementMap map = auto_map(model);
  const auto provisioning = engine(roles.operator_dsn());
  (void)provisioning->ensure_schema(map.placement_of("Price").source.layout, {{"Price", {"id"}}});
  roles.grant("price");
  const std::string literal = "99999999999999999999.123456789012345678";
  Admin(roles.operator_dsn()).run("INSERT INTO price VALUES (1," + literal + "),(2,7)");
  const auto runtime = engine(roles.runtime_dsn());
  sde::Session session(model, map, {{"db", runtime.get()}});
  const std::vector<sde::Row> rows = session.scan("Price").rows;
  ASSERT_EQ(rows.size(), 2U);
  EXPECT_EQ(std::get<sde::Decimal>(rows[0].at("value")).to_string(), literal);
  EXPECT_EQ(std::get<sde::Decimal>(rows[1].at("value")).to_string(), "7.000000000000000000");
}

// --- the plans ----------------------------------------------------------------------------------

/// The reference's index-use model: readings keyed by station and time, with a sensor to index.
sde::Model readings_and_orders() {
  return sde::load_neutral_model(R"({"entities": [
      {"name": "Reading", "fields": [{"name": "at", "type": "timestamptz"},
                                     {"name": "celsius", "type": "int32"},
                                     {"name": "sensor", "type": "string"},
                                     {"name": "station", "type": "string"}],
       "key": ["station", "at"]},
      {"name": "Order", "fields": [{"name": "account", "type": "string"},
                                   {"name": "id", "type": "uuid"}], "key": ["id"]}]})");
}

/// A thousand readings: 50 stations, 400 sensors, so each sensor three times.
void fill(sde::Session& session) {
  std::vector<sde::Row> rows;
  for (int n = 0; n < 1000; ++n) {
    char station[8];
    char sensor[8];
    std::snprintf(station, sizeof station, "st-%02d", n % 50);
    std::snprintf(sensor, sizeof sensor, "sn-%03d", n % 400);
    rows.push_back({{"station", std::string(station)},
                    {"at", *sde::TimestampTz::from_micros(1767225600000000LL + n * 1000000LL)},
                    {"sensor", std::string(sensor)},
                    {"celsius", std::int64_t{n % 40}}});
  }
  session.save_many("Reading", rows);
}

TEST_F(PostgresReads, AScanByATextKeyPrefixUsesThePrimaryKey) {
  const Scope scope(dsn_);
  const sde::Model model = readings_and_orders();
  const sde::PlacementMap map = auto_map(model);
  const auto db = engine(scope.dsn());
  Spy spy(*db);
  sde::Session session(model, map, {{"db", &spy}});
  session.ensure_schema();
  fill(session);
  sde::ScanOptions options;
  options.where = sde::Row{{"station", std::string("st-07")}};
  options.limit = 5;
  const sde::ScanPage page = session.scan("Reading", options);
  ASSERT_EQ(page.rows.size(), 5U);
  for (const sde::Row& row : page.rows) EXPECT_EQ(row.at("station"), sde::Value(std::string("st-07")));
  Admin admin(scope.dsn());
  const std::vector<std::string> found = scans(admin, spy.reads.back());
  // An index or a bitmap index scan is the planner's choice; the primary key with the predicate as
  // its condition, and no sequential scan, is the claim.
  EXPECT_TRUE(has(found, ":reading_pkey on condition")) << listed(found);
  EXPECT_FALSE(has(found, "Seq Scan")) << listed(found);
}

TEST_F(PostgresReads, AScanByADesignedIndexOnATextColumnUsesIt) {
  // A table this library created: its key and its index on text are in the reads' collation, so
  // nothing is named - the event is for a table from before.
  const Scope scope(dsn_);
  const sde::Model model = readings_and_orders();
  const sde::PlacementMap map = auto_map(
      model, R"([{"entity": "Reading", "name": "reading_sensor_btree", "columns": ["sensor"]}])");
  std::vector<std::string> events;
  sde::PostgresOptions options;
  options.log = [&events](std::string_view event, const sde::Json&) {
    events.emplace_back(event);
  };
  sde::PostgresEngine db(scope.dsn(), options);
  db.connect();
  Spy spy(db);
  sde::Session session(model, map, {{"db", &spy}});
  session.ensure_schema();
  EXPECT_EQ(std::count(events.begin(), events.end(), "sde.schema.text_collation"), 0);
  EXPECT_GT(std::count(events.begin(), events.end(), "sde.schema.applied"), 0)
      << "the capture saw none of the adapter's events";
  fill(session);
  sde::CountOptions counted;
  counted.where = sde::Row{{"sensor", std::string("sn-123")}};
  EXPECT_EQ(session.count("Reading", counted), 3U);
  Admin admin(scope.dsn());
  const std::vector<std::string> found = scans(admin, spy.reads.back());
  EXPECT_TRUE(has(found, ":reading_sensor_btree on condition")) << listed(found);
}

}  // namespace
