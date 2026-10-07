/// The session where the shared vectors do not reach: threads, the application's log sink, hashed
/// names, logical reads, storage sizes, a fan-out that fails, nested transactions and batches.

#include <chrono>
#include <future>
#include <map>
#include <memory>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

#include <gtest/gtest.h>

#include "sde/errors.hpp"
#include "sde/hashing.hpp"
#include "sde/model.hpp"
#include "sde/placement.hpp"
#include "sde/session.hpp"
#include "sde/telemetry.hpp"
#include "sde/testing/memory.hpp"
#include "storage_internal.hpp"

namespace {

using sde::testing::MemoryEngine;
using sde::testing::MemoryEngineOptions;

sde::Model readings() {
  return sde::load_neutral_model(R"({"entities": [
    {"name": "Reading", "fields": [{"name": "id", "type": "int64"},
                                   {"name": "station", "type": "string"},
                                   {"name": "celsius", "type": "int32", "nullable": true},
                                   {"name": "at", "type": "timestamptz"}], "key": ["id"]},
    {"name": "Station", "fields": [{"name": "code", "type": "string"}], "key": ["code"]}]})");
}

/// A contract-3 map: both groups in `pg`, and with `copy` a derived copy of Reading in `copy`
/// that writes fan out to.
sde::PlacementMap placed(const sde::Model& model, bool copy = false) {
  std::string reading = R"("Reading": {"source": {"id": "Reading@pg", "engine": "pg",
      "layout": {"auto": true}})";
  if (copy) {
    reading += R"(, "derived": [{"id": "Reading@copy", "engine": "copy", "lag_budget_ms": 1000,
        "layout": {"tables": {"Reading": "reading"}, "columns": {"Reading": {"id": "bigint",
        "station": "text", "celsius": "integer", "at": "timestamptz"}}}}],
        "also_write": ["Reading@copy"])";
  }
  reading += "}";
  const std::string text = R"({"contract": 3, "model_version": ")" + model.version() +
                           R"(", "map_version": 1, "groups": {)" + reading +
                           R"(, "Station": {"source": {"id": "Station@pg", "engine": "pg",
      "layout": {"auto": true}}}}})";
  sde::LoadOptions options;
  options.model = &model;
  return sde::load_map(std::string_view(text), options);
}

sde::Row reading(std::int64_t id) {
  return sde::Row{{"id", id}, {"station", "alpha"}, {"celsius", 20}, {"at", "2026-10-06T10:00:00Z"}};
}

std::string refusal(const std::function<void()>& body) {
  try {
    body();
  } catch (const std::exception& error) {
    return error.what();
  }
  return "";
}

TEST(Session, ASecondThreadIsRefusedWhileTheFirstIsInside) {
  const sde::Model model = readings();
  const sde::PlacementMap map = placed(model);
  MemoryEngine pg;
  sde::Session session(model, map, {{"pg", &pg}});
  std::promise<void> inside;
  std::promise<void> release;
  std::shared_future<void> released = release.get_future().share();
  std::thread first([&] {
    session.transaction({"Reading"}, [&] {
      session.save("Reading", reading(1));  // the owner may re-enter
      inside.set_value();
      released.wait();
    });
  });
  inside.get_future().wait();
  EXPECT_THROW(session.save("Reading", reading(2)), sde::ResourceBusy);
  release.set_value();
  first.join();
  // Free again once the first thread is out.
  session.save("Reading", reading(3));
  EXPECT_EQ(pg.tables.at("reading").size(), 2U);
}

TEST(Session, ASinkThatThrowsCannotFailAnOperation) {
  const sde::Model model = readings();
  const auto throwing = [](std::string_view, const sde::Json&) {
    throw std::runtime_error("the application's logger is broken");
  };
  sde::LoadOptions options;
  options.model = &model;
  options.log = throwing;
  // A valid map still loads, and a refused one still raises the map's refusal, not the sink's.
  const std::string text = R"({"contract": 3, "model_version": ")" + model.version() +
                           R"(", "map_version": 1, "groups": {"Reading": {"source": {"id": "r",
      "engine": "pg", "layout": {"auto": true}}}, "Station": {"source": {"id": "s",
      "engine": "pg", "layout": {"auto": true}}}}})";
  const sde::PlacementMap map = sde::load_map(std::string_view(text), options);
  EXPECT_THROW((void)sde::load_map(std::string_view("{"), options), sde::MapError);
  // A divergence logged through the same sink does not fail the write either.
  const sde::PlacementMap fanning = placed(model, true);
  MemoryEngine pg;
  MemoryEngineOptions failing;
  failing.name = "copy";
  failing.fail_inserts = {{"reading", 1}};
  MemoryEngine copy(failing);
  sde::SessionOptions session_options;
  session_options.log = throwing;
  sde::Session session(model, fanning, {{"pg", &pg}, {"copy", &copy}}, session_options);
  EXPECT_NO_THROW(session.save("Reading", reading(1)));
  EXPECT_EQ(pg.tables.at("reading").size(), 1U);
}

TEST(Session, AFanOutThatFailsIsLoggedCountedAndNotTheCallersFailure) {
  const sde::Model model = readings();
  const sde::PlacementMap map = placed(model, true);
  MemoryEngine pg;
  MemoryEngineOptions failing;
  failing.name = "copy";
  failing.fail_inserts = {{"reading", 1}};
  MemoryEngine copy(failing);
  sde::Recorder recorder(model);
  std::vector<std::pair<std::string, sde::Json>> events;
  sde::SessionOptions options;
  options.recorder = &recorder;
  options.log = [&](std::string_view event, const sde::Json& fields) {
    events.emplace_back(std::string(event), fields);
  };
  sde::Session session(model, map, {{"pg", &pg}, {"copy", &copy}}, options);
  session.save("Reading", reading(1));
  session.save("Reading", reading(2));
  ASSERT_EQ(events.size(), 1U);
  EXPECT_EQ(events[0].first, "sde.migration.divergence");
  EXPECT_EQ(*events[0].second.find("error"), sde::Json("EngineError"));
  EXPECT_EQ(*events[0].second.find("engine"), sde::Json("copy"));
  EXPECT_EQ(pg.tables.at("reading").size(), 2U);
  EXPECT_EQ(copy.tables.at("reading").size(), 1U);
  const auto window = recorder.roll();
  ASSERT_TRUE(window.has_value());
  ASSERT_EQ(window->fanned.size(), 1U);
  EXPECT_EQ(window->fanned[0].writes, 2U);
  EXPECT_EQ(window->fanned[0].failures, 1U);
}

TEST(Session, ANestedTransactionReachesTheCopyOnlyWithTheOutermostCommit) {
  const sde::Model model = readings();
  const sde::PlacementMap map = placed(model, true);
  MemoryEngine pg;
  MemoryEngineOptions named;
  named.name = "copy";
  MemoryEngine copy(named);
  sde::Session session(model, map, {{"pg", &pg}, {"copy", &copy}});
  session.transaction({"Reading"}, [&] {
    session.transaction({"Reading"}, [&] { session.save("Reading", reading(1)); });
    // The inner block has returned and committed nothing yet: the copy has no row.
    EXPECT_EQ(copy.tables.count("reading"), 0U);
  });
  EXPECT_EQ(copy.tables.at("reading").size(), 1U);
  // An outer rollback takes the inner rows with it, from the source and from the queue.
  struct Abandon {};
  EXPECT_THROW(session.transaction({"Reading"}, [&] {
    session.transaction({"Reading"}, [&] { session.save("Reading", reading(2)); });
    throw Abandon{};
  }), Abandon);
  EXPECT_EQ(pg.tables.at("reading").size(), 1U);
  EXPECT_EQ(copy.tables.at("reading").size(), 1U);
  // And the session is not left believing it is inside a transaction.
  session.save("Reading", reading(3));
  EXPECT_EQ(copy.tables.at("reading").size(), 2U);
}

TEST(Session, ATransactionAcrossGroupsIsRefusedBeforeAnythingOpens) {
  const sde::Model model = readings();
  const sde::PlacementMap map = placed(model);
  MemoryEngine pg;
  sde::Session session(model, map, {{"pg", &pg}});
  const std::string message = refusal([&] {
    session.transaction({"Station", "Reading"}, [] { ADD_FAILURE() << "the body ran"; });
  });
  EXPECT_NE(message.find("a transaction cannot span colocation groups (Reading: Reading; "
                         "Station: Station)"),
            std::string::npos)
      << message;
  EXPECT_TRUE(pg.recorded().calls().empty());
  // No entities is the whole model, which has two groups here.
  EXPECT_THROW(session.transaction([] {}), sde::ModelPlanningError);
}

TEST(Session, APointReadNeedsExactlyTheKey) {
  const sde::Model model = readings();
  const sde::PlacementMap map = placed(model);
  MemoryEngine pg;
  sde::Session session(model, map, {{"pg", &pg}});
  session.save("Reading", reading(7));
  const auto row = session.get("Reading", sde::Row{{"id", 7}});
  ASSERT_TRUE(row.has_value());
  EXPECT_EQ(std::get<std::string>(row->at("station")), "alpha");
  EXPECT_FALSE(session.get("Reading", sde::Row{{"id", 8}}).has_value());
  EXPECT_EQ(refusal([&] { (void)session.get("Reading", sde::Row{{"id", 7}, {"station", "a"}}); }),
            "a point read of Reading needs exactly its key ['id'], and was given ['id', "
            "'station']. A partial key is a range read, which is a different shape and may well "
            "be routed somewhere else.");
}

TEST(Session, ABatchIsCheckedWholeBeforeAnyEngine) {
  const sde::Model model = readings();
  const sde::PlacementMap map = placed(model);
  MemoryEngine pg;
  sde::Session session(model, map, {{"pg", &pg}});
  std::vector<sde::Row> too_many(1001, reading(1));
  EXPECT_EQ(refusal([&] { session.save_many("Reading", too_many); }),
            "a batch may contain at most 1000 rows");
  EXPECT_EQ(refusal([&] { session.save_many("Reading", {reading(1), sde::Row{{"id", 2}}}); }),
            "all batch rows must have the same fields");
  EXPECT_EQ(refusal([&] { session.save_many("Reading", {reading(1), sde::Row{}}); }),
            "each batch row must be a nonempty mapping with string fields");
  EXPECT_EQ(refusal([&] { session.save_many("Reading", {sde::Row{{"id", 1}}}); }),
            "batch fields must be declared and include the key and all non-nullable fields");
  EXPECT_TRUE(pg.recorded().calls().empty());
  session.save_many("Reading", {});  // nothing to write, and nothing written
  EXPECT_TRUE(pg.recorded().calls().empty());

  MemoryEngineOptions single;
  single.name = "pg";
  single.can_bulk_write = false;
  MemoryEngine plain(single);
  sde::Session other(model, map, {{"pg", &plain}});
  const std::string message = refusal([&] { other.save_many("Reading", {reading(1)}); });
  EXPECT_EQ(message, "this adapter does not support bulk writes (insert_many)");
}

TEST(Session, HashedNamesStayTheApplicationsOnBothSides) {
  const sde::Model model = readings();
  const auto [hashed, names] = sde::hash_identifiers(model, "a salt of sixteen bytes or more");
  const std::string reading_group = names.entity("Reading");
  const std::string station_group = names.entity("Station");
  const std::string text = R"({"contract": 3, "model_version": ")" + hashed.version() +
                           R"(", "map_version": 1, "groups": {")" + reading_group +
                           R"(": {"source": {"id": "r", "engine": "pg", "layout": {"auto": true}}},
      ")" + station_group + R"(": {"source": {"id": "s", "engine": "pg",
      "layout": {"auto": true}}}}})";
  sde::LoadOptions load;
  load.model = &hashed;
  const sde::PlacementMap map = sde::load_map(std::string_view(text), load);
  MemoryEngine pg;
  sde::SessionOptions options;
  options.names = &names;
  sde::Session session(hashed, map, {{"pg", &pg}}, options);
  session.save("Reading", reading(1));
  // The engine holds digests only.
  const sde::Row& stored = pg.tables.begin()->second.front();
  EXPECT_EQ(stored.count("station"), 0U);
  EXPECT_EQ(stored.count(names.field("Reading", "station")), 1U);
  // The application reads its own names back.
  const auto row = session.get("Reading", sde::Row{{"id", 1}});
  ASSERT_TRUE(row.has_value());
  EXPECT_EQ(std::get<std::string>(row->at("station")), "alpha");
  // And is refused in them.
  EXPECT_NE(refusal([&] { session.save("Reading", sde::Row{{"id", 2}, {"colour", "red"}}); })
                .find("Reading declares no field colour. With hashed identifiers"),
            std::string::npos);
  EXPECT_EQ(refusal([&] {
              session.save("Reading", sde::Row{{"id", 2}, {"at", "2026-10-06T10:00:00Z"}});
            }),
            "Reading.station is required and this row leaves it out");
}

/// An engine that answers reads with canned rows and records the plan it was given.
class ReadingEngine final : public sde::Engine,
                            public sde::Queryable,
                            public sde::Countable,
                            public sde::Summarizable,
                            public sde::StorageMeasurable {
 public:
  bool offers_query = true;
  bool offers_count = true;
  std::string count_refusal;
  bool offers_summary = true;
  bool offers_storage = true;
  std::vector<sde::Row> rows;
  std::optional<sde::ReadPlan> last;
  std::function<std::map<std::string, std::pair<std::int64_t, std::int64_t>>(
      const std::vector<std::string>&)>
      sizes;

  std::string_view dialect() const noexcept override { return "postgres"; }
  std::vector<sde::PhysicalFinding> ensure_schema(const sde::PhysicalLayout&,
                                                  const sde::Keys&) override {
    return {};
  }
  void insert(const std::string&, const sde::Row&) override {}
  std::optional<sde::Row> get(const std::string&, const sde::Row&) override { return std::nullopt; }
  void transaction(const std::function<void()>& body) override { body(); }
  sde::Capabilities capabilities() noexcept override {
    sde::Capabilities offered;
    if (offers_query) offered.query = this;
    if (offers_count) offered.count = this;
    offered.count_refusal = count_refusal;
    if (offers_summary) offered.summary = this;
    if (offers_storage) offered.storage = this;
    return offered;
  }
  std::vector<sde::Row> select_rows(const std::string&, const sde::ReadPlan& plan) override {
    last = plan;
    return rows;
  }
  std::uint64_t count_rows(const std::string&, const sde::ReadPlan& plan) override {
    last = plan;
    return 42;
  }
  sde::SummaryRecord summarize_rows(const std::string&, const sde::ReadPlan& plan,
                                    const sde::ReadColumn&) override {
    last = plan;
    return sde::SummaryRecord{"3", "2", "10", "30", "40"};
  }
  std::map<std::string, std::pair<std::int64_t, std::int64_t>> storage_sizes(
      const std::vector<std::string>& tables) override {
    return sizes(tables);
  }
};

TEST(Session, AScanIsAPageAndThePositionOfTheNext) {
  const sde::Model model = readings();
  const sde::PlacementMap map = placed(model);
  ReadingEngine pg;
  pg.rows = {reading(1), reading(2), reading(3)};
  sde::Recorder recorder(model);
  sde::SessionOptions options;
  options.recorder = &recorder;
  sde::Session session(model, map, {{"pg", &pg}}, options);
  sde::ScanOptions scan;
  scan.where = sde::Row{{"station", "alpha"}};
  scan.limit = 2;
  const sde::ScanPage page = session.scan("Reading", scan);
  ASSERT_EQ(page.rows.size(), 2U);
  ASSERT_TRUE(page.next_after.has_value());
  EXPECT_EQ(std::get<std::int64_t>(page.next_after->at("id")), 2);
  ASSERT_TRUE(pg.last.has_value());
  EXPECT_EQ(pg.last->limit, 2);
  // Telemetry has the shape and the field it filtered on, never the value.
  const auto window = recorder.roll();
  ASSERT_TRUE(window.has_value());
  bool found = false;
  for (const sde::ShapeStats& stats : window->shapes) {
    if (stats.kind != "full_scan") continue;
    found = true;
    EXPECT_EQ(stats.rows, 2U);
    ASSERT_EQ(stats.filtered.size(), 1U);
    EXPECT_EQ(stats.filtered.begin()->first.equal, std::vector<std::string>{"station"});
  }
  EXPECT_TRUE(found);
}

TEST(Session, ReadsAnEngineCannotAnswerAreRefusedByName) {
  const sde::Model model = readings();
  const sde::PlacementMap map = placed(model);
  ReadingEngine pg;
  pg.offers_query = false;
  pg.offers_count = false;
  pg.count_refusal = "this engine keeps no row count; it answers by key only";
  pg.offers_summary = false;
  sde::Session session(model, map, {{"pg", &pg}});
  EXPECT_EQ(refusal([&] { (void)session.scan("Reading"); }),
            "this adapter does not support logical reads (select_rows)");
  EXPECT_EQ(refusal([&] { (void)session.count("Reading"); }),
            "this engine keeps no row count; it answers by key only");
  EXPECT_EQ(refusal([&] { (void)session.summarize("Reading", "celsius"); }),
            "this adapter does not support numeric summaries (summarize_rows)");
  EXPECT_EQ(refusal([&] { (void)session.scan("Nothing"); }),
            "query refers to an entity this model does not declare");
  EXPECT_EQ(refusal([&] { (void)session.summarize("Reading", "nope"); }),
            "summary refers to a field this entity does not declare");
}

TEST(Session, CountsAndSummariesAreTheEnginesExactAnswers) {
  const sde::Model model = readings();
  const sde::PlacementMap map = placed(model);
  ReadingEngine pg;
  sde::Session session(model, map, {{"pg", &pg}});
  sde::CountOptions count;
  count.bounds = sde::Range{"celsius", 10, sde::Null{}};
  EXPECT_EQ(session.count("Reading", count), 42U);
  ASSERT_TRUE(pg.last.has_value());
  EXPECT_TRUE(pg.last->order.empty());
  const sde::NumericSummary summary = session.summarize("Reading", "celsius");
  EXPECT_EQ(summary.count, 3U);
  EXPECT_EQ(summary.mean->to_string(), "20.000000");
}

TEST(Session, StorageIsMeasuredOrSaysWhyNot) {
  const sde::Model model = readings();
  const sde::PlacementMap map = placed(model);
  ReadingEngine pg;
  sde::Recorder recorder(model);
  sde::SessionOptions options;
  options.recorder = &recorder;
  sde::Session session(model, map, {{"pg", &pg}}, options);

  pg.sizes = [](const std::vector<std::string>& tables) {
    std::map<std::string, std::pair<std::int64_t, std::int64_t>> out;
    for (const std::string& table : tables) {
      if (table == "reading") out[table] = {8192, 2048};
    }
    return out;
  };
  sde::StorageMeasurement measured = session.measure_storage();
  ASSERT_EQ(measured.sizes.size(), 1U);
  EXPECT_EQ(measured.sizes[0].group, "Reading");
  EXPECT_EQ(measured.sizes[0].total_bytes, 8192);
  EXPECT_EQ(measured.unavailable.at("Station"), "missing_table");

  pg.sizes = [](const std::vector<std::string>&) -> std::map<std::string, std::pair<std::int64_t, std::int64_t>> {
    throw sde::detail::CatalogueRefused("storage sizes could not be read: permission denied");
  };
  measured = session.measure_storage();
  EXPECT_EQ(measured.unavailable.at("Reading"), "refused");
  // The adapter that read the server's code says so by the type; a message's words decide nothing.
  pg.sizes = [](const std::vector<std::string>&) -> std::map<std::string, std::pair<std::int64_t, std::int64_t>> {
    throw sde::EngineError("permission denied for table parts (SQLSTATE 42501, Code: 497)");
  };
  EXPECT_EQ(session.measure_storage().unavailable.at("Reading"), "failed");
  pg.sizes = [](const std::vector<std::string>&) -> std::map<std::string, std::pair<std::int64_t, std::int64_t>> {
    throw sde::EngineError("connection reset");
  };
  EXPECT_EQ(session.measure_storage().unavailable.at("Reading"), "failed");
  pg.sizes = [](const std::vector<std::string>&) -> std::map<std::string, std::pair<std::int64_t, std::int64_t>> {
    throw sde::ResourceBusy("in use");
  };
  EXPECT_THROW((void)session.measure_storage(), sde::ResourceBusy);
  pg.offers_storage = false;
  EXPECT_EQ(session.measure_storage().unavailable.at("Reading"), "unsupported");
  // A sample alone makes no window, as in the reference: it waits for the next one with traffic.
  EXPECT_FALSE(recorder.roll().has_value());
  (void)session.count("Reading");
  const auto window = recorder.roll();
  ASSERT_TRUE(window.has_value());
  ASSERT_EQ(window->storage.size(), 1U);
  EXPECT_EQ(window->storage[0].group, "Reading");
  EXPECT_EQ(window->storage[0].secondary_index_bytes, 2048);
}

TEST(Session, AMapNamingAnEngineNobodySuppliedIsRefused) {
  const sde::Model model = readings();
  const sde::PlacementMap map = placed(model, true);
  MemoryEngine pg;
  EXPECT_EQ(refusal([&] { const sde::Session session(model, map, {{"pg", &pg}}); }),
            "the placement map refers to engines that were not supplied: ['copy']. A session "
            "cannot route an operation to an engine it has no adapter for, and guessing at a "
            "connection is not something a library should do.");
  // A project id is the local configuration's, checked before anything else.
  sde::SessionOptions options;
  options.project_id = "not hex";
  EXPECT_EQ(refusal([&] { const sde::Session session(model, map, {{"pg", &pg}}, options); }),
            "verification project_id must be 32 lowercase hexadecimal digits");
}

/// Where a point read goes, through the session rather than `resolve`: to the copy the map routes it
/// to, and to the source when it asks to be fresh or runs in a transaction that has written. The
/// session decides those places once, when it opens, and a mutation run found no test reaching any
/// of the three - each decision could be swapped and the suite stayed green.
TEST(Session, APointReadGoesToItsRoutedCopyAndToTheSourceWhenFreshOrAfterAWrite) {
  const sde::Model model = readings();
  const sde::OperationShape* point = nullptr;
  for (const sde::OperationShape& shape : model.shapes()) {
    if (shape.entity == "Reading" && shape.kind == "point_read") point = &shape;
  }
  ASSERT_NE(point, nullptr);
  const std::string text = R"({"contract": 3, "model_version": ")" + model.version() +
                           R"(", "map_version": 1, "groups": {"Reading": {"source": {"id":
      "Reading@pg", "engine": "pg", "layout": {"auto": true}}, "derived": [{"id": "Reading@copy",
      "engine": "copy", "lag_budget_ms": 1000, "layout": {"tables": {"Reading": "reading"},
      "columns": {"Reading": {"id": "bigint", "station": "text", "celsius": "integer",
      "at": "timestamptz"}}}}], "also_write": ["Reading@copy"]}, "Station": {"source": {"id":
      "Station@pg", "engine": "pg", "layout": {"auto": true}}}}, "routing": {")" +
                           point->id + R"(": "Reading@copy"}})";
  sde::LoadOptions options;
  options.model = &model;
  const sde::PlacementMap map = sde::load_map(std::string_view(text), options);
  const auto journal = std::make_shared<sde::testing::Recorded>();
  MemoryEngineOptions source_options;
  source_options.name = "pg";
  source_options.journal = journal;
  MemoryEngineOptions copy_options;
  copy_options.name = "copy";
  copy_options.journal = journal;
  MemoryEngine pg(source_options);
  MemoryEngine copy(copy_options);
  sde::Session session(model, map, {{"pg", &pg}, {"copy", &copy}});
  session.save("Reading", reading(1));

  // The engines whose `get` a read called.
  const auto read_by = [&](const std::function<void()>& read) {
    const std::size_t before = journal->calls().size();
    read();
    std::vector<std::string> engines;
    for (std::size_t call = before; call < journal->calls().size(); ++call) {
      const sde::Json& entry = journal->calls()[call];
      if (entry.find("call")->as_string() == "get") {
        engines.push_back(entry.find("engine")->as_string());
      }
    }
    return engines;
  };
  const sde::Row first{{"id", std::int64_t{1}}};
  EXPECT_EQ(read_by([&] { (void)session.get("Reading", first); }),
            std::vector<std::string>{"copy"});
  EXPECT_EQ(read_by([&] { (void)session.get("Reading", first, true); }),
            std::vector<std::string>{"pg"});
  EXPECT_EQ(read_by([&] {
              session.transaction({"Reading"}, [&] {
                session.save("Reading", reading(2));
                (void)session.get("Reading", {{"id", std::int64_t{2}}});
              });
            }),
            std::vector<std::string>{"pg"});
  // And outside the transaction again, routed as before.
  EXPECT_EQ(read_by([&] { (void)session.get("Reading", first); }),
            std::vector<std::string>{"copy"});
}

}  // namespace
