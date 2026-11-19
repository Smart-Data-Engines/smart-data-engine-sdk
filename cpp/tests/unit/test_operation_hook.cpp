/// The session's operation hook: a record a completed operation, nothing the operation carried - the
/// counterpart of python/tests/test_operation_hook.py. In the sentinel test the control is the
/// sentinel coming back in the rows the operations return, so the test cannot pass by looking at
/// nothing.

#include <cstdint>
#include <functional>
#include <map>
#include <optional>
#include <stdexcept>
#include <string>
#include <vector>

#include <gtest/gtest.h>

#include "sde/errors.hpp"
#include "sde/hook.hpp"
#include "sde/model.hpp"
#include "sde/placement.hpp"
#include "sde/session.hpp"

namespace {

const std::string SENTINEL = "sentinel-7f3a9c41-in-every-value";

/// An engine that keeps rows by table and answers reads from them.
class Reader final : public sde::Engine,
                     public sde::BulkWritable,
                     public sde::Queryable,
                     public sde::Countable,
                     public sde::Summarizable {
 public:
  bool refuse_inserts = false;
  std::map<std::string, std::vector<sde::Row>> tables;

  std::string_view dialect() const noexcept override { return "postgres"; }
  std::vector<sde::PhysicalFinding> ensure_schema(const sde::PhysicalLayout&,
                                                  const sde::Keys&) override {
    return {};
  }
  void insert(const std::string& table, const sde::Row& values) override {
    if (refuse_inserts) {
      throw sde::EngineError("insert into " + table + " failed: Key (id)=(" + SENTINEL +
                             ") already exists");
    }
    tables[table].push_back(values);
  }
  void insert_many(const std::string& table, const std::vector<sde::Row>& rows) override {
    for (const sde::Row& row : rows) insert(table, row);
  }
  std::optional<sde::Row> get(const std::string& table, const sde::Row& key) override {
    for (const sde::Row& row : tables[table]) {
      if (row.at("id") == key.at("id")) return row;
    }
    return std::nullopt;
  }
  void transaction(const std::function<void()>& body) override { body(); }
  sde::Capabilities capabilities() noexcept override {
    sde::Capabilities offered;
    offered.bulk = this;
    offered.query = this;
    offered.count = this;
    offered.summary = this;
    return offered;
  }
  std::vector<sde::Row> select_rows(const std::string& table, const sde::ReadPlan&) override {
    return tables[table];
  }
  std::uint64_t count_rows(const std::string& table, const sde::ReadPlan&) override {
    return tables[table].size();
  }
  sde::SummaryRecord summarize_rows(const std::string&, const sde::ReadPlan&,
                                    const sde::ReadColumn&) override {
    return sde::SummaryRecord{"1", "1", "7", "7", "7"};
  }
};

sde::Model notes() {
  return sde::load_neutral_model(R"({"entities": [
    {"name": "Note", "fields": [{"name": "body", "type": "string"}, {"name": "id", "type": "string"},
                                {"name": "size", "type": "int64"}], "key": ["id"]},
    {"name": "Tag", "fields": [{"name": "id", "type": "string"}, {"name": "note", "type": "string"}],
     "key": ["id"]}], "relations": [], "atomic": [["Note", "Tag"]]})");
}

sde::PlacementMap placed(const sde::Model& model) {
  const std::string text = R"({"contract": 3, "model_version": ")" + model.version() +
                           R"(", "map_version": 1, "groups": {"Note": {"source": {
      "id": "notes@pg", "engine": "pg-main", "layout": {"auto": true}}}}})";
  sde::LoadOptions options;
  options.model = &model;
  return sde::load_map(std::string_view(text), options);
}

struct Fixture {
  sde::Model model = notes();
  sde::PlacementMap map = placed(model);
  Reader pg;
  std::vector<sde::OperationRecord> records;

  sde::Session open() {
    sde::SessionOptions options;
    options.hook = [this](const sde::OperationRecord& record) { records.push_back(record); };
    return sde::Session(model, map, {{"pg-main", &pg}}, options);
  }
};

std::string text(const sde::OperationRecord& r) {
  return r.operation + "|" + r.entity + "|" + r.group + "|" + r.shape + "|" + r.engine + "|" +
         r.dialect + "|" + r.materialization + "|" + std::to_string(r.duration_ns) + "|" +
         std::to_string(r.rows) + "|" + r.outcome;
}

TEST(OperationHook, EachMethodIsOneRecordWithWhereItWent) {
  Fixture fixture;
  sde::Session session = fixture.open();
  session.save("Note", {{"id", "a"}, {"body", "one"}, {"size", std::int64_t{1}}});
  session.save_many("Note", {{{"id", "b"}, {"body", "two"}, {"size", std::int64_t{2}}},
                             {{"id", "c"}, {"body", "three"}, {"size", std::int64_t{3}}}});
  EXPECT_TRUE(session.get("Note", {{"id", "a"}}).has_value());
  sde::ScanOptions scan;
  scan.limit = 10;
  (void)session.scan("Note", scan);
  (void)session.count("Note");
  (void)session.summarize("Note", "size");
  std::vector<std::pair<std::string, std::uint64_t>> seen;
  for (const sde::OperationRecord& record : fixture.records) {
    seen.emplace_back(record.operation, record.rows);
    EXPECT_EQ(record.entity, "Note");
    EXPECT_EQ(record.group, "Note");
    EXPECT_EQ(record.engine, "pg-main");
    EXPECT_EQ(record.dialect, "postgres");
    EXPECT_EQ(record.materialization, "notes@pg");
    EXPECT_EQ(record.outcome, "ok");
    EXPECT_GE(record.duration_ns, 0);
    EXPECT_FALSE(record.shape.empty());
  }
  const std::vector<std::pair<std::string, std::uint64_t>> expected = {
      {"save", 1}, {"save_many", 2}, {"get", 1}, {"scan", 3}, {"count", 1}, {"summarize", 1}};
  EXPECT_EQ(seen, expected);
}

TEST(OperationHook, ATransactionIsARecordOfItsOwnBesideItsOperations) {
  Fixture fixture;
  sde::Session session = fixture.open();
  session.transaction({"Note", "Tag"}, [&] {
    session.save("Note", {{"id", "a"}, {"body", "one"}, {"size", std::int64_t{1}}});
    session.save("Tag", {{"id", "t"}, {"note", "a"}});
  });
  ASSERT_EQ(fixture.records.size(), 3U);
  EXPECT_EQ(fixture.records[0].operation + fixture.records[0].entity, "saveNote");
  EXPECT_EQ(fixture.records[1].operation + fixture.records[1].entity, "saveTag");
  EXPECT_EQ(fixture.records[2].operation, "transaction");
  EXPECT_EQ(fixture.records[2].entity, "Note");
  EXPECT_EQ(fixture.records[2].shape, "");
  EXPECT_EQ(fixture.records[2].outcome, "ok");
}

TEST(OperationHook, ATransactionThatThrowsSaysWhatEndedIt) {
  Fixture fixture;
  sde::Session session = fixture.open();
  EXPECT_THROW(session.transaction({"Note"}, [] { throw sde::ModelPlanningError("gave up"); }),
               sde::ModelPlanningError);
  EXPECT_THROW(session.transaction({"Note"}, [] { throw std::out_of_range("not ours"); }),
               std::out_of_range);
  ASSERT_EQ(fixture.records.size(), 2U);
  EXPECT_EQ(fixture.records[0].outcome, "ModelPlanningError");
  EXPECT_EQ(fixture.records[1].outcome, "unexpected");
}

TEST(OperationHook, AnEngineErrorIsItsClassNeverItsMessage) {
  Fixture fixture;
  fixture.pg.refuse_inserts = true;
  sde::Session session = fixture.open();
  EXPECT_THROW(session.save("Note", {{"id", SENTINEL}, {"body", "x"}, {"size", std::int64_t{1}}}),
               sde::EngineError);
  ASSERT_EQ(fixture.records.size(), 1U);
  EXPECT_EQ(fixture.records[0].operation, "save");
  EXPECT_EQ(fixture.records[0].outcome, "EngineError");
  EXPECT_EQ(text(fixture.records[0]).find(SENTINEL), std::string::npos);
}

TEST(OperationHook, AHookThatThrowsChangesNothing) {
  Fixture fixture;
  sde::SessionOptions options;
  options.hook = [](const sde::OperationRecord&) {
    throw std::runtime_error("the application's exporter is down");
  };
  sde::Session session(fixture.model, fixture.map, {{"pg-main", &fixture.pg}}, options);
  session.save("Note", {{"id", "a"}, {"body", "one"}, {"size", std::int64_t{1}}});
  const std::optional<sde::Row> row = session.get("Note", {{"id", "a"}});
  ASSERT_TRUE(row.has_value());
  EXPECT_EQ(std::get<std::string>(row->at("body")), "one");
}

TEST(OperationHook, NothingAnOperationCarriedReachesTheHook) {
  Fixture fixture;
  sde::Session session = fixture.open();
  session.save("Note", {{"id", SENTINEL}, {"body", SENTINEL}, {"size", std::int64_t{7}}});
  session.save_many("Note",
                    {{{"id", SENTINEL + "-2"}, {"body", SENTINEL}, {"size", std::int64_t{8}}}});
  session.transaction({"Note", "Tag"},
                      [&] { session.save("Tag", {{"id", SENTINEL + "-t"}, {"note", SENTINEL}}); });
  std::vector<sde::Row> returned;
  if (auto row = session.get("Note", {{"id", SENTINEL}})) returned.push_back(*row);
  sde::ScanOptions scan;
  scan.where = sde::Row{{"body", SENTINEL}};
  scan.limit = 10;
  for (sde::Row& row : session.scan("Note", scan).rows) returned.push_back(row);
  sde::CountOptions count;
  count.where = sde::Row{{"body", SENTINEL}};
  (void)session.count("Note", count);
  sde::SummaryOptions summary;
  summary.where = sde::Row{{"body", SENTINEL}};
  (void)session.summarize("Note", "size", summary);
  // The control: the value is there to be found, in what the operations return.
  bool found = false;
  for (const sde::Row& row : returned) {
    for (const auto& [name, value] : row) {
      if (const auto* held = std::get_if<std::string>(&value);
          held != nullptr && held->find(SENTINEL) != std::string::npos) {
        found = true;
      }
    }
  }
  EXPECT_TRUE(found);
  ASSERT_EQ(fixture.records.size(), 8U);
  for (const sde::OperationRecord& record : fixture.records) {
    EXPECT_EQ(text(record).find(SENTINEL), std::string::npos) << text(record);
  }
}

TEST(OperationHook, TheRecordHasTheContractsFieldsInOrder) {
  const std::vector<std::string_view> fields(std::begin(sde::OPERATION_FIELDS),
                                             std::end(sde::OPERATION_FIELDS));
  const std::vector<std::string_view> expected = {
      "operation", "entity", "group", "shape", "engine", "dialect", "materialization",
      "duration_ns", "rows", "outcome"};
  EXPECT_EQ(fields, expected);
  EXPECT_EQ(sde::outcome_of(nullptr), "ok");
  EXPECT_EQ(sde::outcome_of(std::make_exception_ptr(sde::ResourceBusy("x"))), "ResourceBusy");
  EXPECT_EQ(sde::outcome_of(std::make_exception_ptr(std::out_of_range("x"))), "unexpected");
}

}  // namespace
