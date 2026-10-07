/// The PostgreSQL adapter against a real server: a fake would agree with whatever this library
/// believes about types, quoting and transactions, which is exactly the set of beliefs worth
/// checking. Ported from the reference's slice, failure-semantics and transaction tests.
///
///     SDE_POSTGRES_DSN=postgresql://postgres:sde@127.0.0.1:55432/sde ctest -L live

#include <chrono>
#include <cmath>
#include <future>
#include <string>
#include <thread>
#include <vector>

#include <gtest/gtest.h>

#include "engines/postgres/connection.hpp"
#include "live/live.hpp"
#include "sde/errors.hpp"
#include "sde/json.hpp"
#include "sde/model.hpp"
#include "sde/placement.hpp"
#include "sde/postgres.hpp"
#include "sde/session.hpp"

namespace {

using Clock = std::chrono::steady_clock;

/// Statements the tests set up and clean up with, on a connection of their own.
class Admin {
 public:
  explicit Admin(const std::string& dsn) : connection_(dsn) {}
  sde::detail::postgres::Result run(const std::string& sql) { return connection_.execute(sql); }
  void drop(const std::string& table) { (void)run("DROP TABLE IF EXISTS \"" + table + "\""); }

 private:
  sde::detail::postgres::Connection connection_;
};

class PostgresLive : public ::testing::Test {
 protected:
  void SetUp() override { SDE_REQUIRE_DSN("SDE_POSTGRES_DSN", dsn_); }

  /// A model of one entity with every neutral type PostgreSQL stores, each field nullable but the
  /// key, under a name of its own for this run.
  [[nodiscard]] sde::Model every_type() {
    entity_ = "Probe" + sde::live::fresh(8);
    return sde::load_neutral_model(R"json({"entities": [{"name": ")json" + entity_ + R"json(", "fields": [
        {"name": "id", "type": "int64"},
        {"name": "i32", "type": "int32", "nullable": true},
        {"name": "i64", "type": "int64", "nullable": true},
        {"name": "f32", "type": "float32", "nullable": true},
        {"name": "f64", "type": "float64", "nullable": true},
        {"name": "dec", "type": "decimal(38,10)", "nullable": true},
        {"name": "text", "type": "string", "nullable": true},
        {"name": "flag", "type": "bool", "nullable": true},
        {"name": "raw", "type": "bytes", "nullable": true},
        {"name": "uid", "type": "uuid", "nullable": true},
        {"name": "day", "type": "date", "nullable": true},
        {"name": "wall", "type": "timestamp", "nullable": true},
        {"name": "at", "type": "timestamptz", "nullable": true},
        {"name": "doc", "type": "json", "nullable": true}], "key": ["id"]}]})json");
  }

  [[nodiscard]] sde::PlacementMap placed(const sde::Model& model) {
    const std::string text = R"({"contract": 3, "model_version": ")" + model.version() +
                             R"(", "map_version": 1, "groups": {")" + entity_ +
                             R"(": {"source": {"id": "src@pg", "engine": "pg",
                                 "layout": {"auto": true}}}}})";
    sde::LoadOptions options;
    options.model = &model;
    sde::PlacementMap map = sde::load_map(std::string_view(text), options);
    // The table the auto layout derives, from the map rather than restated here.
    table_ = map.placement_of(entity_).source.layout.table_for(entity_);
    return map;
  }

  void TearDown() override {
    if (dsn_.empty() || table_.empty()) return;
    Admin(dsn_).drop(table_);
  }

  std::string dsn_;
  std::string entity_;
  std::string table_;
};

TEST_F(PostgresLive, EveryNeutralTypeRoundTripsThroughARealServer) {
  const sde::Model model = every_type();
  const sde::PlacementMap map = placed(model);
  sde::PostgresEngine engine(dsn_);
  engine.connect();
  sde::Session session(model, map, {{"pg", &engine}});
  session.ensure_schema();
  std::vector<std::uint8_t> every_byte(256);
  for (std::size_t i = 0; i < every_byte.size(); ++i) every_byte[i] = static_cast<std::uint8_t>(i);
  const sde::Row written{
      {"id", std::int64_t{1}},
      {"i32", std::int64_t{-2147483647 - 1}},
      {"i64", std::int64_t{9223372036854775807}},
      {"f32", 1.5},
      {"f64", 0.1},
      {"dec", sde::Decimal("-1234567890123456789012345678.0123456789")},
      {"text", std::string("Zürich, it's \"quoted\" and \\ back")},
      {"flag", true},
      {"raw", sde::Bytes{every_byte}},
      {"uid", *sde::Uuid::parse("6ba7b810-9dad-11d1-80b4-00c04fd430c8")},
      {"day", *sde::Date::parse("0001-01-01")},
      {"wall", *sde::Timestamp::parse("2026-10-07T12:34:56.123456")},
      {"at", *sde::TimestampTz::parse("2026-10-07T12:34:56.654321+02:00")},
      {"doc", sde::JsonDocument{sde::parse_json(R"({"a": [1, "x", null], "b": {"c": true}})")}},
  };
  session.save(entity_, written);
  const std::optional<sde::Row> read = session.get(entity_, {{"id", std::int64_t{1}}});
  ASSERT_TRUE(read.has_value());
  for (const auto& [field, value] : written) {
    EXPECT_EQ(read->at(field), value) << field;
  }

  sde::Row empty{{"id", std::int64_t{2}}};
  for (const auto& [field, unused] : written) {
    if (field != "id") empty[field] = sde::Null{};
  }
  session.save(entity_, empty);
  EXPECT_EQ(session.get(entity_, {{"id", std::int64_t{2}}}), empty);
}

TEST_F(PostgresLive, AFailedWriteIsReportedInTheServersWordsAndLogged) {
  const sde::Model model = every_type();
  std::vector<std::pair<std::string, sde::Json>> events;
  sde::PostgresOptions options;
  options.log = [&](std::string_view event, const sde::Json& fields) {
    events.emplace_back(std::string(event), fields);
  };
  sde::PostgresEngine engine(dsn_, options);
  engine.connect();
  const sde::PlacementMap map = placed(model);
  sde::Session session(model, map, {{"pg", &engine}});
  session.ensure_schema();
  session.save(entity_, {{"id", std::int64_t{1}}});
  try {
    session.save(entity_, {{"id", std::int64_t{1}}});
    FAIL() << "a duplicate key was accepted";
  } catch (const sde::EngineError& error) {
    // The reference's text for the same failure, byte for byte: psycopg's message is libpq's
    // without its severity, DETAIL included.
    EXPECT_EQ(std::string(error.what()),
              "insert into " + table_ + " failed: duplicate key value violates unique constraint \"" +
                  table_ + "_pkey\"\nDETAIL:  Key (id)=(1) already exists.");
  }
  ASSERT_FALSE(events.empty());
  EXPECT_EQ(events.back().first, "sde.write.failed");
  EXPECT_EQ(*events.back().second.find("error"), sde::Json("UniqueViolation"));
  EXPECT_EQ(*events.back().second.find("table"), sde::Json(table_));
}

TEST_F(PostgresLive, ATransactionIsOneUnitAndASavepointIsItsOwn) {
  const sde::Model model = every_type();
  sde::PostgresEngine engine(dsn_);
  engine.connect();
  const sde::PlacementMap map = placed(model);
  sde::Session session(model, map, {{"pg", &engine}});
  session.ensure_schema();
  struct Rollback {};
  EXPECT_THROW(session.transaction([&] {
    session.save(entity_, {{"id", std::int64_t{1}}});
    session.save(entity_, {{"id", std::int64_t{2}}});
    throw Rollback{};
  }),
               Rollback);
  EXPECT_FALSE(session.get(entity_, {{"id", std::int64_t{1}}}).has_value());
  session.transaction([&] {
    session.save(entity_, {{"id", std::int64_t{1}}});
    // A nested transaction is a savepoint: its failure is undone and the outer one goes on.
    EXPECT_THROW(session.transaction([&] {
      session.save(entity_, {{"id", std::int64_t{3}}});
      throw Rollback{};
    }),
                 Rollback);
    session.save(entity_, {{"id", std::int64_t{2}}});
  });
  EXPECT_TRUE(session.get(entity_, {{"id", std::int64_t{1}}}).has_value());
  EXPECT_TRUE(session.get(entity_, {{"id", std::int64_t{2}}}).has_value());
  EXPECT_FALSE(session.get(entity_, {{"id", std::int64_t{3}}}).has_value());
}

TEST_F(PostgresLive, AnAbortedTransactionIsNeverReportedAsCommitted) {
  const sde::Model model = every_type();
  sde::PostgresEngine engine(dsn_);
  engine.connect();
  const sde::PlacementMap map = placed(model);
  sde::Session session(model, map, {{"pg", &engine}});
  session.ensure_schema();
  session.save(entity_, {{"id", std::int64_t{1}}});
  try {
    session.transaction([&] {
      session.save(entity_, {{"id", std::int64_t{2}}});
      // The duplicate aborts the transaction on the server; the body swallows the error, and the
      // commit must not be reported as one.
      EXPECT_THROW(session.save(entity_, {{"id", std::int64_t{1}}}), sde::EngineError);
    });
    FAIL() << "an aborted transaction was reported as committed";
  } catch (const sde::EngineError& error) {
    EXPECT_STREQ(error.what(), "the transaction is aborted and cannot be reported as committed");
  }
  // Nothing of it landed, and the engine is usable: the outcome was certain.
  EXPECT_FALSE(session.get(entity_, {{"id", std::int64_t{2}}}).has_value());
}

TEST_F(PostgresLive, ASecondThreadIsRefusedWhileOneIsInsideATransaction) {
  const sde::Model model = every_type();
  sde::PostgresEngine engine(dsn_);
  engine.connect();
  const sde::PlacementMap map = placed(model);
  sde::Session session(model, map, {{"pg", &engine}});
  session.ensure_schema();
  std::promise<void> inside;
  std::promise<void> release;
  std::thread holder([&] {
    engine.transaction([&] {
      inside.set_value();
      release.get_future().wait();
    });
  });
  inside.get_future().wait();
  EXPECT_THROW((void)engine.get(table_, {{"id", std::int64_t{1}}}), sde::ResourceBusy);
  release.set_value();
  holder.join();
  EXPECT_FALSE(engine.get(table_, {{"id", std::int64_t{1}}}).has_value());
}

// --- the connection failures, measured, as the reference measures them -------------------------

TEST_F(PostgresLive, AnEngineThatIsNotListeningFailsAtOnceAndSaysWhy) {
  const std::string closed = sde::live::with_port(dsn_, sde::live::free_port());
  ASSERT_NE(closed, dsn_) << "the setup must have moved the DSN, or this tests a healthy engine";
  sde::PostgresEngine engine(closed);
  const auto started = Clock::now();
  try {
    engine.connect();
    FAIL() << "a closed port accepted the connection";
  } catch (const sde::EngineError& error) {
    EXPECT_TRUE(std::string(error.what()).starts_with("could not connect to PostgreSQL: "))
        << error.what();
    EXPECT_NE(std::string(error.what()).find("connection"), std::string::npos);
  }
  EXPECT_LT(Clock::now() - started, std::chrono::seconds(5));
}

TEST_F(PostgresLive, AServerThatAcceptsAndStaysSilentIsBoundedByTheDefault) {
  // Without the bound the call does not return: libpq has no default connect_timeout.
  sde::live::SilentServer silent;
  sde::PostgresEngine engine(sde::live::with_port(dsn_, silent.port()));
  const auto started = Clock::now();
  EXPECT_THROW(engine.connect(), sde::EngineError);
  const auto elapsed = Clock::now() - started;
  EXPECT_GE(elapsed, std::chrono::seconds(9));
  EXPECT_LE(elapsed, std::chrono::seconds(15));
}

TEST_F(PostgresLive, ATimeoutTheCallerChoseWinsOverOurs) {
  sde::live::SilentServer silent;
  sde::PostgresEngine engine(
      sde::live::with_parameter(sde::live::with_port(dsn_, silent.port()), "connect_timeout=2"));
  const auto started = Clock::now();
  EXPECT_THROW(engine.connect(), sde::EngineError);
  EXPECT_LT(Clock::now() - started, std::chrono::seconds(7));
}

TEST_F(PostgresLive, ACutConnectionReachesTheCallerAndNothingIsRetried) {
  const sde::Model model = every_type();
  const std::string application = "sde_cpp_live_" + sde::live::fresh(8);
  sde::PostgresEngine engine(
      sde::live::with_parameter(dsn_, "application_name=" + application));
  engine.connect();
  const sde::PlacementMap map = placed(model);
  sde::Session session(model, map, {{"pg", &engine}});
  session.ensure_schema();
  session.save(entity_, {{"id", std::int64_t{1}}, {"text", std::string("before")}});

  // Only this engine's own backend, found by the name it connected under.
  Admin admin(dsn_);
  const auto terminated = admin.run(
      "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name = '" +
      application + "'");
  ASSERT_EQ(terminated.rows(), 1) << "nothing was terminated, so this test proves nothing";

  try {
    session.save(entity_, {{"id", std::int64_t{2}}, {"text", std::string("during")}});
    FAIL() << "a write on a cut connection succeeded";
  } catch (const sde::EngineError& error) {
    // The server's own words; a wrapper that replaced them would hide whether it was the engine,
    // the network or us.
    // The reference's text for this failure, byte for byte.
    EXPECT_EQ(std::string(error.what()),
              "insert into " + table_ +
                  " failed: terminating connection due to administrator command. The connection "
                  "is gone and this library does not reopen one it was handed: call close() then "
                  "connect() on the engine, or hand the session a new one. Nothing was retried, so "
                  "no write reached the engine twice.");
  }
  try {
    session.save(entity_, {{"id", std::int64_t{3}}});
    FAIL() << "the connection was reopened behind the caller's back";
  } catch (const sde::EngineError& error) {
    EXPECT_EQ(std::string(error.what()),
              "insert into " + table_ +
                  " failed: the connection is closed. The connection is gone and this library does "
                  "not reopen one it was handed: call close() then connect() on the engine, or "
                  "hand the session a new one. Nothing was retried, so no write reached the engine "
                  "twice.");
  }
  // Recovery is the caller's, two calls, and the write that failed did not land.
  engine.close();
  engine.connect();
  EXPECT_EQ(session.get(entity_, {{"id", std::int64_t{1}}})->at("text"),
            sde::Value(std::string("before")));
  EXPECT_FALSE(session.get(entity_, {{"id", std::int64_t{2}}}).has_value());
}

}  // namespace
