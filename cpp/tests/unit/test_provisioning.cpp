/// Schema preparation (`sde::prepare_schema`), which no vector reaches: it runs before any session
/// exists, through the client's provisioning connections. The first three tests are the reference's
/// own (`test_unfenced.py`). The rest pin what it refuses before creating anything, what a table of
/// another design costs here and in a running session, and the bookkeeping of a signed map.

#include <functional>
#include <map>
#include <memory>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

#include <gtest/gtest.h>

#include "sde/canonical.hpp"
#include "sde/engine.hpp"
#include "sde/errors.hpp"
#include "sde/json.hpp"
#include "sde/model.hpp"
#include "sde/placement.hpp"
#include "sde/provisioning.hpp"
#include "sde/session.hpp"
#include "sde/testing/memory.hpp"
#include "sde/write_fence.hpp"
#include "support/signer.hpp"

namespace {

using sde::testing::MemoryEngine;
using sde::testing::MemoryEngineOptions;
using sde::testing::MemoryFences;
using sde::testing::Recorded;
using sde::testing_support::Signer;

const std::string kProject = "11111111111111111111111111111111";

sde::Model unfenced_model() {
  return sde::load_neutral_model(R"({"entities": [
    {"name": "Reading", "fields": [{"name": "celsius", "type": "int32"},
                                   {"name": "id", "type": "int64"}], "key": ["id"]},
    {"name": "Tick", "fields": [{"name": "at", "type": "int64"}, {"name": "price", "type": "int64"},
                                {"name": "venue", "type": "string"}], "key": ["venue", "at"]}]})");
}

sde::PlacementMap loaded(const sde::Model& model, const sde::Json& document,
                         const Signer* signer = nullptr) {
  sde::LoadOptions options;
  options.model = &model;
  if (signer != nullptr) options.public_keys = signer->keys();
  return sde::load_map(signer != nullptr ? signer->signed_document(document) : document, options);
}

/// The reference's contract-6 map: Reading carries generation 2 in `pg-main`, Tick none in `book-1`.
sde::Json unfenced_document(const sde::Model& model) {
  return sde::parse_json(R"({"contract": 6, "project_id": ")" + kProject +
                         R"(", "model_version": ")" + model.version() +
                         R"(", "map_version": 1, "groups": {
      "Reading": {"write_epoch": 2, "source": {"id": "Reading@pg", "engine": "pg-main",
        "layout": {"tables": {"Reading": "reading"},
                   "columns": {"Reading": {"id": "bigint", "celsius": "integer"}}}}},
      "Tick": {"source": {"id": "Tick@book", "engine": "book-1",
        "layout": {"tables": {"Tick": "tick"},
                   "columns": {"Tick": {"venue": "text", "at": "bigint", "price": "bigint"}}}}}}})");
}

/// The same two groups in one engine, `db`, with no generation: contract 3.
sde::Json plain_document(const sde::Model& model) {
  return sde::parse_json(R"({"contract": 3, "model_version": ")" + model.version() +
                         R"(", "map_version": 4, "groups": {
      "Reading": {"source": {"id": "Reading@db", "engine": "db",
        "layout": {"tables": {"Reading": "reading"},
                   "columns": {"Reading": {"id": "bigint", "celsius": "integer"}}}}},
      "Tick": {"source": {"id": "Tick@db", "engine": "db",
        "layout": {"tables": {"Tick": "tick"},
                   "columns": {"Tick": {"venue": "text", "at": "bigint", "price": "bigint"}}}}}}})");
}

MemoryEngineOptions named(std::string dialect, std::string name,
                          const std::shared_ptr<Recorded>& journal, bool bookkeeping = true) {
  MemoryEngineOptions options;
  options.dialect = std::move(dialect);
  options.name = std::move(name);
  options.journal = journal;
  options.can_keep_bookkeeping = bookkeeping;
  return options;
}

/// The reference's engine set: `pg-main` with a fence for `reading`, and `book-1`, which keeps no
/// bookkeeping and, unless told, fences nothing.
struct Unfenced {
  std::shared_ptr<Recorded> journal = std::make_shared<Recorded>();
  std::shared_ptr<MemoryFences> reading = std::make_shared<MemoryFences>();
  std::shared_ptr<MemoryFences> tick = std::make_shared<MemoryFences>();
  MemoryEngine pg{named("postgres", "pg-main", journal)};
  MemoryEngine book{named("orderbook", "book-1", journal, false)};

  explicit Unfenced(bool book_fences, bool pg_fences = true) {
    if (pg_fences) pg.bind_fences({{"reading", reading}});
    if (book_fences) book.bind_fences({{"tick", tick}});
  }

  [[nodiscard]] std::map<std::string, sde::Engine*> engines() {
    return {{"pg-main", &pg}, {"book-1", &book}};
  }

  /// Nothing reached an engine or a fence: what "refused before creating anything" means.
  void untouched() const {
    EXPECT_TRUE(journal->calls().empty());
    EXPECT_TRUE(reading->calls.empty());
    EXPECT_TRUE(tick->calls.empty());
    EXPECT_TRUE(pg.tables.empty());
    EXPECT_TRUE(book.tables.empty());
  }
};

/// Each call as `engine.call`, in the order the engines received them.
std::vector<std::string> calls(const Recorded& journal) {
  std::vector<std::string> out;
  for (const sde::Json& call : journal.calls()) {
    out.push_back(call.find("engine")->as_string() + "." + call.find("call")->as_string());
  }
  return out;
}

template <typename Error>
std::string refusal(const std::function<void()>& body) {
  try {
    body();
  } catch (const Error& error) {
    return error.what();
  }
  ADD_FAILURE() << "nothing was refused";
  return "";
}

TEST(PrepareSchema, PreparesAGenerationOnlyWhereTheMapCarriesOne) {
  const sde::Model model = unfenced_model();
  const sde::PlacementMap map = loaded(model, unfenced_document(model));
  Unfenced set(false);
  sde::prepare_schema(model, map, set.engines(), kProject);
  EXPECT_EQ(calls(*set.journal),
            (std::vector<std::string>{"pg-main.ensure_schema", "book-1.ensure_schema"}));
  EXPECT_EQ(set.reading->column, sde::ColumnState::valid);
  EXPECT_FALSE(set.reading->constraints.empty());
  EXPECT_EQ(set.book.tables, (std::map<std::string, std::vector<sde::Row>>{{"tick", {}}}));
}

TEST(PrepareSchema, RefusesAnEngineThatFencesBeforeCreatingAnything) {
  const sde::Model model = unfenced_model();
  const sde::PlacementMap map = loaded(model, unfenced_document(model));
  Unfenced set(true);
  EXPECT_EQ(refusal<sde::MigrationRefused>(
                [&] { sde::prepare_schema(model, map, set.engines(), kProject); }),
            "group Tick carries no write generation on book-1, which fences writes; a map we issue "
            "gives every group on such an engine a generation, so this one was built for another "
            "engine");
  set.untouched();
}

TEST(PrepareSchema, StillNeedsEveryEngineTheMapNames) {
  const sde::Model model = unfenced_model();
  const sde::PlacementMap map = loaded(model, unfenced_document(model));
  Unfenced set(false);
  std::map<std::string, sde::Engine*> engines = set.engines();
  engines.erase("book-1");
  EXPECT_EQ(refusal<sde::MigrationRefused>(
                [&] { sde::prepare_schema(model, map, engines, kProject); }),
            "schema preparation is missing an engine named by the map");
  set.untouched();
}

TEST(PrepareSchema, RefusesAMapForAnotherModel) {
  const sde::Model model = unfenced_model();
  const sde::PlacementMap map = loaded(model, unfenced_document(model));
  const sde::Model other = sde::load_neutral_model(
      R"({"entities": [{"name": "Reading", "fields": [{"name": "id", "type": "int64"}],
                        "key": ["id"]}]})");
  Unfenced set(false);
  EXPECT_EQ(refusal<sde::MigrationRefused>(
                [&] { sde::prepare_schema(other, map, set.engines(), kProject); }),
            "schema preparation needs the model named by the placement map");
  set.untouched();
}

TEST(PrepareSchema, AGenerationBearingMapNeedsTheLocallyConfiguredProject) {
  const sde::Model model = unfenced_model();
  const sde::PlacementMap map = loaded(model, unfenced_document(model));
  for (const std::optional<std::string>& project :
       {std::optional<std::string>{}, std::optional<std::string>{std::string(32, '2')}}) {
    Unfenced set(false);
    EXPECT_EQ(refusal<sde::MigrationRefused>(
                  [&] { sde::prepare_schema(model, map, set.engines(), project); }),
              "this generation-bearing map needs its locally configured project_id; do not learn "
              "that identity from the supplied map");
    set.untouched();
  }
}

TEST(PrepareSchema, AGroupCarryingAGenerationNeedsAnEngineThatFences) {
  const sde::Model model = unfenced_model();
  const sde::PlacementMap map = loaded(model, unfenced_document(model));
  Unfenced set(false, false);
  EXPECT_EQ(refusal<sde::MigrationRefused>(
                [&] { sde::prepare_schema(model, map, set.engines(), kProject); }),
            "schema preparation needs native write generations on every engine of a group that "
            "carries one");
  set.untouched();
  // Both wrong at once: the group that needs a fence is named first, as the reference orders them.
  Unfenced both(true, false);
  EXPECT_EQ(refusal<sde::MigrationRefused>(
                [&] { sde::prepare_schema(model, map, both.engines(), kProject); }),
            "schema preparation needs native write generations on every engine of a group that "
            "carries one");
  both.untouched();
}

TEST(PrepareSchema, EachTableOfAGroupIsPreparedInNameOrder) {
  // Two entities in one group, whose tables sort the other way from their entities: the order is
  // the tables', as the reference's `sorted(layout.tables.values())`.
  const sde::Model model = sde::load_neutral_model(R"({"atomic": [["Order", "Payment"]],
    "entities": [{"name": "Order", "fields": [{"name": "id", "type": "int64"}], "key": ["id"]},
                 {"name": "Payment", "fields": [{"name": "id", "type": "int64"}], "key": ["id"]}]})");
  const sde::PlacementMap map = loaded(
      model, sde::parse_json(R"({"contract": 6, "project_id": ")" + kProject +
                             R"(", "model_version": ")" + model.version() +
                             R"(", "map_version": 1, "groups": {"Order": {"write_epoch": 3,
        "source": {"id": "Order@pg", "engine": "pg", "layout": {
          "tables": {"Order": "zeta_orders", "Payment": "alpha_payments"},
          "columns": {"Order": {"id": "bigint"}, "Payment": {"id": "bigint"}}}}}}})"));
  std::vector<std::string> order;
  std::map<std::string, std::shared_ptr<MemoryFences>> backends;
  for (const std::string& table : std::vector<std::string>{"zeta_orders", "alpha_payments"}) {
    auto backend = std::make_shared<MemoryFences>();
    backend->on_call = [&order, table](const sde::Json&) {
      if (order.empty() || order.back() != table) order.push_back(table);
    };
    backends[table] = backend;
  }
  MemoryEngine pg{named("postgres", "pg", std::make_shared<Recorded>())};
  pg.bind_fences(backends);
  sde::prepare_schema(model, map, {{"pg", &pg}}, kProject);
  EXPECT_EQ(order, (std::vector<std::string>{"alpha_payments", "zeta_orders"}));
  for (const auto& [table, backend] : backends) {
    EXPECT_EQ(backend->column, sde::ColumnState::valid) << table;
  }
}

/// An engine whose tables already exist with another design: it reports `findings` from both of
/// its schema calls, and offers write fences when it is given a backend.
class Designed final : public sde::Engine, public sde::Fencable, public sde::SchemaValidator {
 public:
  std::vector<sde::PhysicalFinding> findings;
  std::shared_ptr<MemoryFences> fences;
  int ensured = 0;

  [[nodiscard]] std::string_view dialect() const noexcept override { return "postgres"; }
  std::vector<sde::PhysicalFinding> ensure_schema(const sde::PhysicalLayout&,
                                                  const sde::Keys&) override {
    ++ensured;
    return findings;
  }
  void insert(const std::string&, const sde::Row&) override {}
  std::optional<sde::Row> get(const std::string&, const sde::Row&) override { return {}; }
  void transaction(const std::function<void()>& body) override { body(); }
  [[nodiscard]] sde::Capabilities capabilities() noexcept override {
    sde::Capabilities offered;
    if (fences) {
      offered.fences = this;
      offered.schema = this;
    }
    return offered;
  }
  sde::WriteFence write_fence(const std::string& table, const std::string& project_id) override {
    return sde::WriteFence(*fences, table, project_id);
  }
  std::vector<sde::PhysicalFinding> validate_schema(const sde::PhysicalLayout&,
                                                    const sde::Keys&) override {
    return findings;
  }
};

const sde::PhysicalFinding kKey{"reading", "primary key", "['id']", "['celsius']"};
const sde::PhysicalFinding kIndex{"reading", "index reading_celsius", "btree on ['celsius']",
                                  "absent"};

TEST(PrepareSchema, ATableOfAnotherDesignIsRefusedNamingEveryDifference) {
  const sde::Model model = unfenced_model();
  const sde::PlacementMap map = loaded(model, plain_document(model));
  Designed db;
  db.findings = {kKey, kIndex};
  EXPECT_EQ(refusal<sde::EngineError>([&] { sde::prepare_schema(model, map, {{"db", &db}}); }),
            "existing tables differ from the physical design this map declares: reading: primary "
            "key is ['celsius'] and the map declares ['id']; reading: index reading_celsius is "
            "absent and the map declares btree on ['celsius']. `CREATE ... IF NOT EXISTS` keeps "
            "whatever table or index already has the name, so this came from an earlier map or "
            "from outside SDE. A new layout needs fresh tables (staging), not the old ones under "
            "the new declaration.");
  EXPECT_EQ(db.ensured, 1) << "the second group was prepared after the first was refused";
  // Nothing found is nothing refused.
  db.findings.clear();
  sde::prepare_schema(model, map, {{"db", &db}});
  EXPECT_EQ(db.ensured, 3);
}

TEST(PrepareSchema, ATableOfAnotherDesignGetsNoGeneration) {
  // Refused before its fence is touched: a generation installed on the wrong table is a table the
  // runtime would then accept.
  const sde::Model model = unfenced_model();
  sde::Json document = unfenced_document(model);
  document.set("groups", sde::parse_json(R"({"Reading": {"write_epoch": 2, "source": {
      "id": "Reading@pg", "engine": "pg-main", "layout": {"tables": {"Reading": "reading"},
      "columns": {"Reading": {"id": "bigint", "celsius": "integer"}}}}}})"));
  const sde::Model reading_only = sde::load_neutral_model(
      R"({"entities": [{"name": "Reading", "fields": [{"name": "celsius", "type": "int32"},
                        {"name": "id", "type": "int64"}], "key": ["id"]}]})");
  document.set("model_version", reading_only.version());
  const sde::PlacementMap map = loaded(reading_only, document);
  Designed pg;
  pg.fences = std::make_shared<MemoryFences>();
  pg.findings = {kKey};
  EXPECT_THROW(sde::prepare_schema(reading_only, map, {{"pg-main", &pg}}, kProject),
               sde::EngineError);
  EXPECT_TRUE(pg.fences->calls.empty());
}

TEST(PrepareSchema, ASignedMapPreparesTheBookkeepingWithoutRecordingItsVersion) {
  const sde::Model model = unfenced_model();
  const Signer signer("schema preparation");
  const sde::PlacementMap map = loaded(model, plain_document(model), &signer);
  ASSERT_TRUE(map.is_signed());
  const auto journal = std::make_shared<Recorded>();
  MemoryEngine db{named("postgres", "db", journal)};
  MemoryEngine spare{named("postgres", "spare", journal)};  // supplied, named by no group
  MemoryEngine book{named("orderbook", "book", journal, false)};
  sde::prepare_schema(model, map, {{"spare", &spare}, {"db", &db}, {"book", &book}});
  EXPECT_EQ(calls(*journal),
            (std::vector<std::string>{"db.ensure_schema", "db.ensure_schema", "db.map_watermark",
                                      "spare.map_watermark"}));
  EXPECT_FALSE(db.map_watermark().has_value());

  // An unsigned map costs no bookkeeping at all.
  const auto quiet = std::make_shared<Recorded>();
  MemoryEngine plain{named("postgres", "db", quiet)};
  sde::prepare_schema(model, loaded(model, plain_document(model)), {{"db", &plain}});
  EXPECT_EQ(calls(*quiet), (std::vector<std::string>{"db.ensure_schema", "db.ensure_schema"}));
}

struct Captured {
  std::vector<std::pair<std::string, sde::Json>> events;
  [[nodiscard]] sde::LogSink sink() {
    return [this](std::string_view event, const sde::Json& fields) {
      events.emplace_back(std::string(event), fields);
    };
  }
  [[nodiscard]] std::vector<std::string> names() const {
    std::vector<std::string> out;
    for (const auto& [event, fields] : events) out.push_back(event);
    return out;
  }
};

TEST(SessionSchema, EnsureSchemaReportsTheDifferencesItFindsAsOpeningDoes) {
  const sde::Model model = unfenced_model();
  const sde::PlacementMap map = loaded(model, plain_document(model));
  Designed db;
  db.findings = {kKey};
  Captured log;
  sde::SessionOptions options;
  options.log = log.sink();
  sde::Session session(model, map, {{"db", &db}}, options);
  session.ensure_schema();
  EXPECT_EQ(session.physical(), (std::vector<sde::PhysicalFinding>{kKey, kKey}));
  ASSERT_EQ(log.names(), (std::vector<std::string>{"sde.schema.physical_mismatch",
                                                   "sde.schema.applied"}));
  EXPECT_EQ(sde::canonical_bytes(log.events[0].second), R"({"findings":2,"tables":["reading"]})");
}

TEST(SessionSchema, AGenerationBearingSessionReportsOnOpeningAndOnEnsureSchema) {
  const sde::Model model = unfenced_model();
  const sde::PlacementMap map = loaded(
      model, sde::parse_json(R"({"contract": 6, "project_id": ")" + kProject +
                             R"(", "model_version": ")" + model.version() +
                             R"(", "map_version": 1, "groups": {
      "Reading": {"write_epoch": 2, "source": {"id": "Reading@db", "engine": "db",
        "layout": {"tables": {"Reading": "reading"},
                   "columns": {"Reading": {"id": "bigint", "celsius": "integer"}}}}},
      "Tick": {"write_epoch": 2, "source": {"id": "Tick@db", "engine": "db",
        "layout": {"tables": {"Tick": "tick"},
                   "columns": {"Tick": {"venue": "text", "at": "bigint", "price": "bigint"}}}}}}})"));
  Designed db;
  db.fences = std::make_shared<MemoryFences>();
  sde::prepare_schema(model, map, {{"db", &db}}, kProject);
  db.findings = {kIndex};  // the table changed after provisioning
  Captured log;
  sde::SessionOptions options;
  options.log = log.sink();
  options.project_id = kProject;
  sde::Session session(model, map, {{"db", &db}}, options);
  EXPECT_EQ(log.names(), (std::vector<std::string>{"sde.schema.physical_mismatch"}));
  session.ensure_schema();
  EXPECT_EQ(log.names(), (std::vector<std::string>{"sde.schema.physical_mismatch",
                                                   "sde.schema.physical_mismatch"}));
  EXPECT_EQ(session.physical(), (std::vector<sde::PhysicalFinding>{kIndex, kIndex}));
}

}  // namespace
