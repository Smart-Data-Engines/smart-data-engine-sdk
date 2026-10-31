/// The forward-only bookkeeping and native batches against real engines, one test body for each:
/// `max()` over an empty table, which PostgreSQL answers with null and ClickHouse with 0, an
/// append-only watermark that refuses an older map and lets an operator step back, a batch that
/// keeps every row and every microsecond and that a bad row never sends, a batch fanned out from
/// one engine to the other, and a ClickHouse insert whose answer was lost and which is not sent
/// again. Ported from the reference's `test_rollback_protection_live.py` and `test_bulk_live.py`.
///
///     export SDE_POSTGRES_DSN=postgresql://postgres:sde@127.0.0.1:55432/sde
///     export SDE_CLICKHOUSE_DSN=clickhouse://default:sde@127.0.0.1:58123/sde
///     ctest -L live

#include <cstdint>
#include <functional>
#include <map>
#include <memory>
#include <optional>
#include <string>
#include <utility>
#include <vector>

#include <gtest/gtest.h>

#include "live/live.hpp"
#include "live/maps.hpp"
#include "live/sides.hpp"
#include "sde/errors.hpp"
#include "sde/json.hpp"
#include "sde/layout.hpp"
#include "sde/model.hpp"
#include "sde/placement.hpp"
#include "sde/schema.hpp"
#include "sde/session.hpp"
#include "support/signer.hpp"

#ifdef SDE_LIVE_CLICKHOUSE
#include "engines/clickhouse/dsn.hpp"
#include "live/clickhouse.hpp"
#include "live/http_stub.hpp"
#include "sde/clickhouse.hpp"
#endif

namespace {

using sde::live::Direction;
using sde::live::Side;
using sde::live::side_of;

std::string message_of(const std::function<void()>& body) {
  try {
    body();
  } catch (const std::exception& error) {
    return error.what();
  }
  return "accepted";
}

// --- the forward-only bookkeeping ---------------------------------------------------------------

class Watermark : public ::testing::TestWithParam<std::string> {
 protected:
  void SetUp() override {
    side_ = side_of(GetParam());
    SDE_REQUIRE_SIDE(side_, GetParam());
  }

  sde::WatermarkStore& store() { return *side_->engine().capabilities().watermark; }

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
    const sde::Session session(model_, map, {{"e", &side_->engine()}});
    return session.rollback_protection();
  }

  std::int64_t rows() {
    return std::stoll(side_->scalar("SELECT count(*) FROM " +
                                    sde::quote_identifier(GetParam(), sde::WATERMARK_TABLE)));
  }

  std::unique_ptr<Side> side_;
  sde::Model model_ = sde::load_neutral_model(R"({"entities": [{"name": "Reading", "fields": [
      {"name": "id", "type": "uuid"}, {"name": "station", "type": "string"}], "key": ["id"]}]})");
  sde::testing_support::Signer signer_{"the watermark tests"};
};

TEST_P(Watermark, AnEmptyEngineReportsNoWatermarkAndCreatesTheTable) {
  // max() over nothing: null in PostgreSQL, 0 in ClickHouse, and in both "nothing has been
  // applied" - exactly the kind of belief a fake would have agreed with.
  EXPECT_FALSE(store().map_watermark().has_value());
  // The table exists now, and asking again is the ordinary path on every restart.
  EXPECT_FALSE(store().map_watermark().has_value());
  EXPECT_EQ(rows(), 0);
}

TEST_P(Watermark, TheWatermarkAdvancesAndARollbackIsRefused) {
  EXPECT_EQ(open(4).protection, "enforced");
  EXPECT_EQ(store().map_watermark(), 4);
  (void)open(9);
  EXPECT_EQ(store().map_watermark(), 9);
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
  EXPECT_EQ(store().map_watermark(), 9);
}

TEST_P(Watermark, TheDocumentedEscapeActuallyWorks) {
  // The refusal tells an operator to delete the rows above the version they want, and to let the
  // deletion finish - which in ClickHouse means waiting for its mutation. It has to work.
  (void)open(9);
  EXPECT_THROW((void)open(5), sde::MapRolledBack);
  side_->remove(std::string(sde::WATERMARK_TABLE), "map_version > 5");
  EXPECT_EQ(open(5).protection, "enforced");
  EXPECT_EQ(store().map_watermark(), 5);
}

TEST_P(Watermark, TheBookkeepingHoldsOneRowPerVersionAndNotPerStart) {
  for (const int version : {1, 2, 2, 2, 3}) (void)open(version);
  EXPECT_EQ(rows(), 3);
  EXPECT_EQ(store().map_watermark(), 3);
}

INSTANTIATE_TEST_SUITE_P(Engines, Watermark, ::testing::ValuesIn(sde::live::dialects()),
                         sde::live::dialect_name);

// --- native batches -----------------------------------------------------------------------------

sde::Model events(const std::string& type) {
  return sde::load_neutral_model(R"({"entities": [{"name": "Event", "fields": [
      {"name": "id", "type": "int64"}, {"name": "value", "type": ")" + type + R"("}],
      "key": ["id"]}]})");
}

/// A contract-3 map of `Event` in each engine named, in that engine's default layout; with more
/// than one, the second is a copy the first fans out to.
sde::PlacementMap events_map(const sde::Model& model,
                             const std::vector<std::pair<std::string, std::string>>& engines) {
  sde::Json groups = sde::Json::object();
  sde::Json group = sde::Json::object();
  sde::Json copies = sde::Json::array();
  for (std::size_t i = 0; i < engines.size(); ++i) {
    const auto& [name, dialect] = engines[i];
    sde::Json material = sde::Json::object();
    material.set("id", name);
    material.set("engine", name);
    material.set("layout", sde::live::layout_json(
                               sde::default_layout(model, model.groups().front(), dialect)));
    if (i == 0) {
      group.set("source", std::move(material));
    } else {
      material.set("lag_budget_ms", std::int64_t{30000});
      copies.as_array().push_back(std::move(material));
    }
  }
  if (!copies.as_array().empty()) {
    sde::Json also = sde::Json::array();
    for (const sde::Json& copy : copies.as_array()) also.as_array().push_back(*copy.find("id"));
    group.set("derived", std::move(copies));
    group.set("also_write", std::move(also));
  }
  groups.set("Event", std::move(group));
  sde::Json document = sde::parse_json(R"({"contract": 3, "model_version": ")" + model.version() +
                                       R"(", "map_version": 1})");
  document.set("groups", std::move(groups));
  sde::LoadOptions options;
  options.model = &model;
  return sde::load_map(document, options);
}

/// The table `Event` gets, created by the side's provisioning login and granted to its runtime.
std::string provision(Side& side, const sde::Model& model) {
  const sde::PhysicalLayout layout =
      sde::default_layout(model, model.groups().front(), side.dialect());
  (void)side.engine().ensure_schema(layout, {{"Event", {"id"}}});
  side.grant(layout.table_for("Event"));
  return layout.table_for("Event");
}

sde::Value instant(int micros) {
  return *sde::TimestampTz::from_micros(1789344000000000LL + 123456 + micros);
}

class Batches : public ::testing::TestWithParam<std::string> {
 protected:
  void SetUp() override {
    side_ = side_of(GetParam());
    SDE_REQUIRE_SIDE(side_, GetParam());
    table_ = provision(*side_, model_);
    session_ = std::make_unique<sde::Session>(
        model_, map_, std::map<std::string, sde::Engine*>{{"db", &side_->runtime()}});
  }

  std::unique_ptr<Side> side_;
  sde::Model model_ = events("timestamptz");
  sde::PlacementMap map_ = events_map(model_, {{"db", GetParam()}});
  std::string table_;
  std::unique_ptr<sde::Session> session_;
};

TEST_P(Batches, OneNativeBatchKeepsEveryRowAndEveryMicrosecond) {
  std::vector<sde::Row> values;
  for (int i = 0; i < 1000; ++i) values.push_back({{"id", std::int64_t{i}}, {"value", instant(i)}});
  session_->save_many("Event", values);
  EXPECT_EQ(side_->migration().count(table_), 1000U);
  EXPECT_EQ(side_->migration().key_range(table_, {"id"}, std::nullopt, std::nullopt, std::nullopt),
            values);
  EXPECT_EQ(session_->get("Event", {{"id", std::int64_t{999}}}), values.back());
}

TEST_P(Batches, ABadBatchNeverReachesTheServer) {
  EXPECT_THROW(session_->save_many("Event", {{{"id", std::int64_t{1}}, {"value", instant(0)}},
                                             {{"id", std::int64_t{2}}}}),
               sde::BulkWriteRefused);
  EXPECT_EQ(side_->migration().count(table_), 0U);
  EXPECT_THROW(side_->runtime().capabilities().bulk->insert_many(
                   table_, {{{"a b", std::int64_t{1}}, {"c", std::int64_t{2}}},
                            {{"a", std::int64_t{1}}, {"b c", std::int64_t{2}}}}),
               sde::BulkWriteRefused);
  EXPECT_EQ(side_->migration().count(table_), 0U);
}

INSTANTIATE_TEST_SUITE_P(Engines, Batches, ::testing::ValuesIn(sde::live::dialects()),
                         sde::live::dialect_name);

class BatchesAcross : public ::testing::TestWithParam<Direction> {};

TEST_P(BatchesAcross, ABatchIsFannedOutAndATransactionQueuesItsCopy) {
  // A batch reaches the copy in the other engine with every microsecond. From PostgreSQL inside a
  // transaction - nested, too - the copy waits for the commit and a rollback sends it nowhere;
  // ClickHouse has no transactions, so from there the batch alone.
  const std::unique_ptr<Side> source = side_of(GetParam().source);
  SDE_REQUIRE_SIDE(source, GetParam().source);
  const std::unique_ptr<Side> target = side_of(GetParam().target);
  SDE_REQUIRE_SIDE(target, GetParam().target);
  const sde::Model model = events("timestamptz");
  const std::string table = provision(*source, model);
  ASSERT_EQ(provision(*target, model), table);
  const sde::PlacementMap map =
      events_map(model, {{"source", GetParam().source}, {"copy", GetParam().target}});
  sde::Session session(model, map, {{"source", &source->runtime()}, {"copy", &target->runtime()}});
  std::vector<sde::Row> rows;
  for (int i = 0; i < 3; ++i) rows.push_back({{"id", std::int64_t{i}}, {"value", instant(i)}});
  if (GetParam().source == "postgres") {
    session.transaction({"Event"}, [&] {
      session.transaction({"Event"}, [&] { session.save_many("Event", rows); });
      EXPECT_EQ(target->migration().count(table), 0U) << "the copy was written before the commit";
    });
    struct Rollback {};
    EXPECT_THROW(session.transaction({"Event"}, [&] {
      session.save_many("Event", {{{"id", std::int64_t{99}}, {"value", instant(0)}}});
      throw Rollback{};
    }),
                 Rollback);
  } else {
    session.save_many("Event", rows);
  }
  EXPECT_EQ(source->migration().key_range(table, {"id"}, std::nullopt, std::nullopt, std::nullopt),
            rows);
  EXPECT_EQ(target->migration().key_range(table, {"id"}, std::nullopt, std::nullopt, std::nullopt),
            rows);
}

INSTANTIATE_TEST_SUITE_P(Across, BatchesAcross, ::testing::ValuesIn(sde::live::across()),
                         sde::live::direction_name);
GTEST_ALLOW_UNINSTANTIATED_PARAMETERIZED_TEST(BatchesAcross);

// --- an answer that never came ------------------------------------------------------------------

#ifdef SDE_LIVE_CLICKHOUSE
class LostAnswer : public ::testing::TestWithParam<std::string> {};

TEST_P(LostAnswer, AnInsertTheServerAcceptedIsNotSentAgain) {
  // A proxy passes the first INSERT to the server and drops its answer: the server has the row and
  // the adapter does not know it. The reference measured its driver replaying such an insert and
  // reporting success; here it is reported, with its outcome unknown, and sent once.
  const std::unique_ptr<Side> side = side_of("clickhouse");
  SDE_REQUIRE_SIDE(side, "clickhouse");
  const sde::Model model = events("timestamptz");
  const std::string table = provision(*side, model);
  const sde::detail::clickhouse::Target upstream =
      sde::detail::clickhouse::parse_dsn(side->runtime_dsn());
  int inserts = 0;
  sde::live::ForwardingProxy proxy(upstream.host, upstream.port, [&](const std::string& request) {
    const std::string line = request.substr(0, request.find("\r\n"));
    if (line.find("&query=INSERT") == std::string::npos) return sde::live::ForwardingProxy::Action::relay;
    return ++inserts == 1 ? sde::live::ForwardingProxy::Action::lose
                          : sde::live::ForwardingProxy::Action::relay;
  });
  sde::ClickHouseEngine runtime(sde::live::with_port(side->runtime_dsn(), proxy.port()));
  runtime.connect();
  const sde::PlacementMap map = events_map(model, {{"db", "clickhouse"}});
  sde::Session session(model, map, {{"db", &runtime}});
  const sde::Row values{{"id", std::int64_t{1}}, {"value", instant(0)}};
  const std::string lost =
      " failed: ClickHouse transport failed; the operation was not replayed and its outcome may "
      "be unknown";
  if (GetParam() == "save") {
    EXPECT_EQ(message_of([&] { session.save("Event", values); }), "insert into " + table + lost);
  } else if (GetParam() == "save_many") {
    EXPECT_EQ(message_of([&] { session.save_many("Event", {values}); }),
              "batch insert into " + table + lost);
  } else {
    EXPECT_EQ(message_of([&] { runtime.copy_in(table, {values}); }),
              "copying 1 rows into " + table + lost);
  }
  EXPECT_EQ(inserts, 1) << "the insert was sent again";
  // FINAL would collapse a duplicate and hide the very defect this measures.
  EXPECT_EQ(side->scalar("SELECT toString(count()) FROM `" + table + "`"), "1");
  EXPECT_EQ(side->engine().get(table, {{"id", std::int64_t{1}}}), values);
}

INSTANTIATE_TEST_SUITE_P(Operations, LostAnswer, ::testing::Values("save", "save_many", "copy_in"),
                         [](const ::testing::TestParamInfo<std::string>& operation) {
                           return operation.param == "save"        ? std::string("Save")
                                  : operation.param == "save_many" ? std::string("SaveMany")
                                                                   : std::string("CopyIn");
                         });
#endif

}  // namespace
