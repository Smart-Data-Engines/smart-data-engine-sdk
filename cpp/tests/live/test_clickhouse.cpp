/// The ClickHouse adapter against a real server, and against local sockets where the claim is about
/// the transport: a fake would agree with whatever this library believes about types, quoting and
/// merge semantics, which is the set of beliefs worth checking. Ported from the reference's
/// `test_clickhouse_slice.py`, `test_clickhouse_tls.py` and the ClickHouse half of
/// `test_failure_semantics.py`; expected messages are the reference's, captured against the same
/// server.
///
///     SDE_CLICKHOUSE_DSN=clickhouse://default:sde@127.0.0.1:58123/sde ctest -L live

#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>

#include <atomic>
#include <chrono>
#include <future>
#include <limits>
#include <map>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <thread>
#include <vector>

#include <gtest/gtest.h>

#include "live/clickhouse.hpp"
#include "live/http_stub.hpp"
#include "live/live.hpp"
#include "sde/clickhouse.hpp"
#include "sde/errors.hpp"
#include "sde/json.hpp"
#include "sde/model.hpp"
#include "sde/placement.hpp"
#include "sde/schema.hpp"
#include "sde/session.hpp"

namespace {

using Clock = std::chrono::steady_clock;
using sde::live::ClickHouseScope;

std::string message_of(const std::function<void()>& body) {
  try {
    body();
  } catch (const std::exception& error) {
    return error.what();
  }
  return "accepted";
}

/// A listener that records the first bytes of every connection: five of them, and for an HTTP
/// request the whole request, before it closes without answering. Answering nothing is the point:
/// a request that reached a server and got no answer is the case where a replay would duplicate.
class FirstBytes {
 public:
  FirstBytes() {
    listener_ = ::socket(AF_INET, SOCK_STREAM, 0);
    sockaddr_in address{};
    address.sin_family = AF_INET;
    address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    ::bind(listener_, reinterpret_cast<sockaddr*>(&address), sizeof address);
    socklen_t length = sizeof address;
    ::getsockname(listener_, reinterpret_cast<sockaddr*>(&address), &length);
    port_ = ntohs(address.sin_port);
    ::listen(listener_, 8);
    worker_ = std::thread([this] { serve(); });
  }
  ~FirstBytes() {
    ::shutdown(listener_, SHUT_RDWR);
    ::close(listener_);
    worker_.join();
  }
  FirstBytes(const FirstBytes&) = delete;
  FirstBytes& operator=(const FirstBytes&) = delete;
  [[nodiscard]] int port() const noexcept { return port_; }
  [[nodiscard]] std::vector<std::string> seen() const {
    const std::lock_guard<std::mutex> lock(mutex_);
    return seen_;
  }

 private:
  void serve() {
    while (true) {
      const int socket = ::accept(listener_, nullptr, nullptr);
      if (socket < 0) return;
      timeval timeout{1, 0};
      ::setsockopt(socket, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof timeout);
      std::string prefix;
      char buffer[65536];
      while (prefix.size() < 5) {
        const ssize_t got = ::recv(socket, buffer, 5 - prefix.size(), 0);
        if (got <= 0) break;
        prefix.append(buffer, static_cast<std::size_t>(got));
      }
      if (prefix.size() == 5) {
        {
          const std::lock_guard<std::mutex> lock(mutex_);
          seen_.push_back(prefix);
        }
        if (prefix == "POST ") {
          std::string request = prefix;
          while (request.find("\r\n\r\n") == std::string::npos) {
            const ssize_t got = ::recv(socket, buffer, sizeof buffer, 0);
            if (got <= 0) break;
            request.append(buffer, static_cast<std::size_t>(got));
          }
        }
      }
      ::close(socket);
    }
  }

  int listener_ = -1;
  int port_ = 0;
  mutable std::mutex mutex_;
  std::vector<std::string> seen_;
  std::thread worker_;
};

class ClickHouseLive : public ::testing::Test {
 protected:
  void SetUp() override { SDE_REQUIRE_DSN("SDE_CLICKHOUSE_DSN", dsn_); }

  /// The reference's slice model: an event keyed by a UUID, with a name, a moment and an amount.
  static sde::Model events() {
    return sde::load_neutral_model(R"json({"entities": [{"name": "Event", "fields": [
        {"name": "id", "type": "uuid"}, {"name": "name", "type": "string"},
        {"name": "at", "type": "timestamptz"}, {"name": "amount", "type": "decimal(12,2)"}],
        "key": ["id"]}]})json");
  }

  static sde::PlacementMap auto_map(const sde::Model& model) {
    return sde::live::default_map(model, "clickhouse");
  }

  static std::unique_ptr<sde::ClickHouseEngine> engine(const std::string& dsn,
                                                       sde::ClickHouseOptions options = {}) {
    auto made = std::make_unique<sde::ClickHouseEngine>(dsn, std::move(options));
    made->connect();
    return made;
  }

  static sde::Row event(const std::string& name, const std::string& at = "2026-08-27T12:00:00.5Z") {
    static std::atomic<int> next{1};
    char id[40];
    std::snprintf(id, sizeof id, "6ba7b810-9dad-11d1-80b4-%012d", next++);
    return {{"id", *sde::Uuid::parse(id)},
            {"name", name},
            {"at", *sde::TimestampTz::parse(at)},
            {"amount", sde::Decimal("1234.56")}};
  }

  std::string dsn_;
};

TEST_F(ClickHouseLive, TheSchemaIsCreatedAndCreatingItAgainChangesNothing) {
  const ClickHouseScope scope(dsn_);
  const sde::Model model = events();
  const sde::PlacementMap map = auto_map(model);
  const auto ch = engine(scope.dsn());
  const sde::PhysicalLayout& layout = map.placement_of("Event").source.layout;
  EXPECT_TRUE(ch->ensure_schema(layout, {{"Event", {"id"}}}).empty());
  EXPECT_TRUE(ch->ensure_schema(layout, {{"Event", {"id"}}}).empty());
  std::map<std::string, std::string> types;
  for (const sde::Json& row : scope.admin().rows("DESCRIBE TABLE `event`")) {
    types[row.find("name")->as_string()] = row.find("type")->as_string();
  }
  EXPECT_EQ(types, (std::map<std::string, std::string>{{"amount", "Decimal(12, 2)"},
                                                       {"at", "DateTime64(6, 'UTC')"},
                                                       {"id", "UUID"},
                                                       {"name", "String"}}));
  const std::vector<sde::Json> created = scope.admin().rows(
      "SELECT engine, sorting_key FROM system.tables WHERE database = currentDatabase() "
      "AND name = 'event'");
  ASSERT_EQ(created.size(), 1U);
  EXPECT_EQ(created[0].find("engine")->as_string(), "ReplacingMergeTree");
  EXPECT_EQ(created[0].find("sorting_key")->as_string(), "id")
      << "ORDER BY has to be the declared key, or the table is unindexed";
}

TEST_F(ClickHouseLive, EveryNeutralTypeClickHouseHoldsRoundTripsThroughARealServer) {
  // bytes and json have no ClickHouse layout: the reference refuses both, for reasons it measured.
  const ClickHouseScope scope(dsn_);
  const sde::Model model = sde::load_neutral_model(R"json({"entities": [{"name": "Probe",
      "fields": [{"name": "id", "type": "int64"},
                 {"name": "i32", "type": "int32", "nullable": true},
                 {"name": "i64", "type": "int64", "nullable": true},
                 {"name": "f32", "type": "float32", "nullable": true},
                 {"name": "f64", "type": "float64", "nullable": true},
                 {"name": "dec", "type": "decimal(38,10)", "nullable": true},
                 {"name": "text", "type": "string", "nullable": true},
                 {"name": "flag", "type": "bool", "nullable": true},
                 {"name": "uid", "type": "uuid", "nullable": true},
                 {"name": "day", "type": "date", "nullable": true},
                 {"name": "wall", "type": "timestamp", "nullable": true},
                 {"name": "at", "type": "timestamptz", "nullable": true}], "key": ["id"]}]})json");
  const sde::PlacementMap map = auto_map(model);
  const auto ch = engine(scope.dsn());
  sde::Session session(model, map, {{"ch", ch.get()}});
  session.ensure_schema();
  const sde::Row written{
      {"id", std::int64_t{1}},
      {"i32", std::int64_t{-2147483647 - 1}},
      {"i64", std::int64_t{9223372036854775807}},
      {"f32", 1.5},
      {"f64", 0.1},
      {"dec", sde::Decimal("-1234567890123456789012345678.0123456789")},
      {"text", std::string("Zürich, it's \"quoted\", a `tick`, a \\ back\tand\na line")},
      {"flag", true},
      {"uid", *sde::Uuid::parse("6ba7b810-9dad-11d1-80b4-00c04fd430c8")},
      {"day", *sde::Date::parse("0001-01-01")},
      {"wall", *sde::Timestamp::parse("2026-10-07T12:34:56.123456")},
      {"at", *sde::TimestampTz::parse("2026-10-07T12:34:56.654321+02:00")},
  };
  session.save("Probe", written);
  const std::optional<sde::Row> read = session.get("Probe", {{"id", std::int64_t{1}}});
  ASSERT_TRUE(read.has_value());
  for (const auto& [field, value] : written) EXPECT_EQ(read->at(field), value) << field;

  sde::Row empty{{"id", std::int64_t{2}}};
  for (const auto& [field, unused] : written) {
    if (field != "id") empty[field] = sde::Null{};
  }
  session.save("Probe", empty);
  EXPECT_EQ(session.get("Probe", {{"id", std::int64_t{2}}}), empty);

  // Dates and moments outside the range ClickHouse prints and parses as text, 1900 to 2299: in
  // binary, as the reference's driver writes them, they are the values they were.
  sde::Row edges = written;
  edges["id"] = std::int64_t{5};
  edges["day"] = *sde::Date::parse("9999-12-31");
  edges["wall"] = *sde::Timestamp::parse("0001-01-01T00:00:00.000001");
  edges["at"] = *sde::TimestampTz::parse("9999-12-31T23:59:59.999999Z");
  session.save("Probe", edges);
  EXPECT_EQ(session.get("Probe", {{"id", std::int64_t{5}}}), edges);

  // NaN and the infinities, both ways.
  sde::Row floats{{"id", std::int64_t{3}},
                  {"f64", std::numeric_limits<double>::infinity()},
                  {"f32", -std::numeric_limits<double>::infinity()}};
  for (const auto& [field, unused] : written) {
    if (!floats.contains(field)) floats[field] = sde::Null{};
  }
  session.save("Probe", floats);
  EXPECT_EQ(session.get("Probe", {{"id", std::int64_t{3}}}), floats);
  floats["id"] = std::int64_t{4};
  floats["f64"] = std::numeric_limits<double>::quiet_NaN();
  session.save("Probe", floats);
  const std::optional<sde::Row> nan = session.get("Probe", {{"id", std::int64_t{4}}});
  ASSERT_TRUE(nan.has_value());
  EXPECT_TRUE(std::isnan(std::get<double>(nan->at("f64"))));
}

TEST_F(ClickHouseLive, AValueTheColumnCannotHoldIsRefusedNeverChanged) {
  // Measured against the reference on the same server: it stores 1.239 in a Decimal(12, 2) as
  // 1.23 and 12345678901.23 as 12345678901.20, without a word - where PostgreSQL rounds the first
  // to 1.24. Refused here, before anything is sent; an integer past Int32 is refused by both.
  const ClickHouseScope scope(dsn_);
  scope.admin().run("CREATE TABLE w (id Int64, d Decimal(12, 2), i Int32) "
                    "ENGINE = ReplacingMergeTree ORDER BY id");
  const auto ch = engine(scope.dsn());
  for (const sde::Row& row : std::vector<sde::Row>{
           {{"id", std::int64_t{1}}, {"d", sde::Decimal("1.239")}},
           {{"id", std::int64_t{2}}, {"d", sde::Decimal("12345678901.23")}},
           {{"id", std::int64_t{3}}, {"i", std::int64_t{2147483648}}},
           {{"id", std::int64_t{4}}, {"i", std::int64_t{-2147483649}}}}) {
    const std::string refused = message_of([&] { ch->insert("w", row); });
    EXPECT_TRUE(refused.starts_with("insert into w failed: ")) << refused;
  }
  EXPECT_EQ(scope.admin().rows("SELECT count() AS rows FROM w")[0].find("rows")->as_string(), "0");
  // What the column does hold goes in exactly: a whole number, and one written with an exponent.
  ch->insert("w", {{"id", std::int64_t{5}}, {"d", sde::Decimal("1E+3")}, {"i", std::int64_t{-2147483648}}});
  EXPECT_EQ(ch->get("w", {{"id", std::int64_t{5}}})->at("d"), sde::Value(sde::Decimal("1000.00")));
}

TEST_F(ClickHouseLive, AMomentIsUtcAndAnInstantGivenWithAnOffsetIsRespected) {
  const ClickHouseScope scope(dsn_);
  const sde::Model model = events();
  const sde::PlacementMap map = auto_map(model);
  const auto ch = engine(scope.dsn());
  (void)ch->ensure_schema(map.placement_of("Event").source.layout, {{"Event", {"id"}}});
  const sde::Row aware = event("aware", "2026-08-27T12:00:00+05:00");
  ch->insert("event", aware);
  const std::optional<sde::Row> read = ch->get("event", {{"id", aware.at("id")}});
  ASSERT_TRUE(read.has_value());
  EXPECT_EQ(read->at("at"), sde::Value(*sde::TimestampTz::parse("2026-08-27T07:00:00Z")));
  EXPECT_EQ(read->at("amount"), sde::Value(sde::Decimal("1234.56")));
}

TEST_F(ClickHouseLive, SavingTheSameKeyTwiceReplacesRatherThanRaising) {
  // Merges stopped: a background merge collapses the duplicate within seconds, after which a read
  // with FINAL and one without return the same thing, and deleting FINAL passes.
  const ClickHouseScope scope(dsn_);
  const sde::Model model = events();
  const sde::PlacementMap map = auto_map(model);
  const auto ch = engine(scope.dsn());
  (void)ch->ensure_schema(map.placement_of("Event").source.layout, {{"Event", {"id"}}});
  scope.admin().run("SYSTEM STOP MERGES `event`");
  sde::Row values = event("first");
  ch->insert("event", values);
  values["name"] = std::string("second");
  ch->insert("event", values);
  EXPECT_EQ(scope.admin().rows("SELECT count() AS stored FROM `event`")[0].find("stored")->as_string(),
            "2")
      << "two parts were expected, so the rest of this test is about deduplication on read";
  EXPECT_EQ(ch->get("event", {{"id", values.at("id")}})->at("name"),
            sde::Value(std::string("second")))
      << "a point read returned a superseded row";
  EXPECT_EQ(ch->count("event"), 1U) << "count() must count entities rather than stored rows";
  EXPECT_EQ(ch->key_range("event", {"id"}, std::nullopt, std::nullopt, std::nullopt).size(), 1U)
      << "a keyset read returned the duplicate too";
  scope.admin().run("SYSTEM START MERGES `event`");
}

TEST_F(ClickHouseLive, AHostileStringIsAValueAndNeverSyntax) {
  const ClickHouseScope scope(dsn_);
  const sde::Model model = events();
  const sde::PlacementMap map = auto_map(model);
  const auto ch = engine(scope.dsn());
  sde::Session session(model, map, {{"ch", ch.get()}});
  session.ensure_schema();
  const std::vector<std::string> hostile = {"x' OR 1=1 --", "a\\' OR 1=1 --", "`; DROP TABLE event; --",
                                            "tab\there", "line\nbreak", "\\", "'", "é€K"};
  for (const std::string& text : hostile) {
    session.save("Event", event(text));
    sde::CountOptions counted;
    counted.where = sde::Row{{"name", text}};
    EXPECT_EQ(session.count("Event", counted), 1U) << text;
  }
  EXPECT_EQ(session.count("Event"), hostile.size());
}

TEST_F(ClickHouseLive, AskingForATransactionRefusesBeforeTheBodyRuns) {
  const ClickHouseScope scope(dsn_);
  const auto ch = engine(scope.dsn());
  bool ran = false;
  const std::string refused = message_of([&] { ch->transaction([&] { ran = true; }); });
  EXPECT_NE(refused.find("no multi-statement transactions"), std::string::npos) << refused;
  EXPECT_FALSE(ran);
}

TEST_F(ClickHouseLive, AKeyNamingAColumnTheLayoutLacksIsRefused) {
  const ClickHouseScope scope(dsn_);
  const sde::Model model = events();
  const sde::PlacementMap map = auto_map(model);
  const auto ch = engine(scope.dsn());
  const std::string refused = message_of([&] {
    (void)ch->ensure_schema(map.placement_of("Event").source.layout, {{"Event", {"not_a_column"}}});
  });
  EXPECT_NE(refused.find("names columns the layout does not have"), std::string::npos) << refused;
}

TEST_F(ClickHouseLive, ACopiedChunkOfTwoShapesIsRefusedBeforeAnyRowIsSent) {
  // A chunk comes from one table; rows of two column sets are a caller assembling it from two.
  const ClickHouseScope scope(dsn_);
  scope.admin().run("CREATE TABLE t (id Int64, a String, b String) ENGINE = ReplacingMergeTree "
                    "ORDER BY id");
  const auto ch = engine(scope.dsn());
  EXPECT_EQ(message_of([&] {
              ch->copy_in("t", {{{"id", std::int64_t{1}}, {"a", std::string("x")}},
                                {{"id", std::int64_t{2}}, {"b", std::string("y")}}});
            }),
            "copy_in into t was given rows with different columns (['a', 'id'] and ['b', 'id']). "
            "A chunk comes from one table, so this is a caller assembling it from two.");
  EXPECT_EQ(ch->count("t"), 0U);
}

TEST_F(ClickHouseLive, AFailedReadIsReportedInTheServersWordsAsTheReferenceReportsIt) {
  const ClickHouseScope scope(dsn_);
  std::vector<std::pair<std::string, sde::Json>> events;
  sde::ClickHouseOptions options;
  options.log = [&](std::string_view name, const sde::Json& fields) { events.emplace_back(name, fields); };
  const auto ch = engine(scope.dsn(), options);
  const sde::detail::clickhouse::Target target = sde::detail::clickhouse::parse_dsn(scope.dsn());
  const std::string url = "http://" + target.host + ":" + std::to_string(target.port);
  EXPECT_EQ(message_of([&] { (void)ch->get("missing", {{"id", std::int64_t{1}}}); }),
            "select from missing failed: Received ClickHouse exception, code: 60, server response: "
            "Code: 60. DB::Exception: Unknown table expression identifier 'missing' in scope SELECT * "
            "FROM missing FINAL WHERE id = 1 LIMIT 1. (UNKNOWN_TABLE) (version " +
                ch->server_version() + " (official build)) (for url " + url + ")");
  EXPECT_EQ(message_of([&] { (void)ch->count("missing"); }),
            "count on missing failed: Received ClickHouse exception, code: 60, server response: Code: "
            "60. DB::Exception: Unknown table expression identifier 'missing' in scope SELECT count() "
            "FROM missing FINAL. (UNKNOWN_TABLE) (version " +
                ch->server_version() + " (official build)) (for url " + url + ")");
  const std::string written = message_of([&] { ch->insert("missing", {{"id", std::int64_t{1}}}); });
  EXPECT_TRUE(written.starts_with("insert into missing failed: Received ClickHouse exception, code: 60"))
      << written;
  ASSERT_FALSE(events.empty());
  EXPECT_EQ(events.back().first, "sde.write.failed");
  EXPECT_EQ(sde::dump_json(events.back().second), R"({"table":"missing","error":"DatabaseError"})");
}

TEST_F(ClickHouseLive, AnEngineThatIsNotListeningFailsAtOnceAndSaysWhy) {
  const std::string dsn = "clickhouse://default:sde@127.0.0.1:" + std::to_string(sde::live::free_port()) + "/sde";
  const auto started = Clock::now();
  EXPECT_EQ(message_of([&] { sde::ClickHouseEngine(dsn).connect(); }),
            "could not connect to ClickHouse: ClickHouse transport failed; the operation was not "
            "replayed and its outcome may be unknown");
  EXPECT_LT(Clock::now() - started, std::chrono::seconds(2));
}

TEST_F(ClickHouseLive, AServerThatAcceptsAndStaysSilentIsBoundedByTheTimeout) {
  sde::live::SilentServer silent;
  const std::string base = "clickhouse://default:sde@127.0.0.1:" + std::to_string(silent.port()) + "/sde";
  auto started = Clock::now();
  EXPECT_EQ(message_of([&] { sde::ClickHouseEngine(base + "?send_receive_timeout=2").connect(); }),
            "could not connect to ClickHouse: ClickHouse transport failed; the operation was not "
            "replayed and its outcome may be unknown");
  auto elapsed = Clock::now() - started;
  EXPECT_GE(elapsed, std::chrono::milliseconds(1900));
  EXPECT_LT(elapsed, std::chrono::seconds(5));
  // And with nothing chosen, the reference's fifteen seconds.
  started = Clock::now();
  EXPECT_NE(message_of([&] { sde::ClickHouseEngine(base).connect(); }), "accepted");
  elapsed = Clock::now() - started;
  EXPECT_GE(elapsed, std::chrono::seconds(14));
  EXPECT_LT(elapsed, std::chrono::seconds(20));
}

TEST_F(ClickHouseLive, AHandshakeThatNeverEndsIsBoundedAsAWhole) {
  // A server that answers byte by byte and never finishes is never silent, so the bound on silence
  // alone would wait for ever; the handshake is bounded as a whole by send_receive_timeout too.
  std::optional<sde::live::TrickleServer> server(std::in_place, std::chrono::milliseconds(200));
  sde::ClickHouseEngine ch("clickhouse://fixture:canary@127.0.0.1:" + std::to_string(server->port()) +
                           "/default?send_receive_timeout=2");
  const auto started = Clock::now();
  auto opened = std::async(std::launch::async, [&] { return message_of([&] { ch.connect(); }); });
  if (opened.wait_for(std::chrono::seconds(10)) != std::future_status::ready) {
    server.reset();  // the connection closes, and the handshake with it
    FAIL() << "the handshake was still waiting after ten seconds";
  }
  const double seconds = std::chrono::duration<double>(Clock::now() - started).count();
  EXPECT_EQ(opened.get(),
            "could not connect to ClickHouse: ClickHouse transport failed; the operation was not "
            "replayed and its outcome may be unknown");
  EXPECT_GE(seconds, 1.8);
  EXPECT_LT(seconds, 5.0);
}

TEST_F(ClickHouseLive, AnExchangeAfterTheHandshakeIsBoundedBySilence) {
  // The handshake has a bound of its own as a whole; every later exchange is bounded by silence
  // alone, so a server that takes a read and says nothing is given up on after the URI's two
  // seconds - and the read is reported, not retried.
  sde::live::ScriptedServer server(
      {sde::live::version_answer("24.8.14.39"), sde::live::Reply::silent()});
  sde::ClickHouseEngine ch("clickhouse://fixture:canary@127.0.0.1:" + std::to_string(server.port()) +
                           "/default?send_receive_timeout=2");
  ch.connect();
  const auto started = Clock::now();
  EXPECT_EQ(message_of([&] { (void)ch.get("events", {{"id", std::int64_t{1}}}); }),
            "select from events failed: ClickHouse transport failed; the operation was not "
            "replayed and its outcome may be unknown");
  const double seconds = std::chrono::duration<double>(Clock::now() - started).count();
  EXPECT_GE(seconds, 1.8);
  EXPECT_LT(seconds, 5.0);
  EXPECT_EQ(server.requests().size(), 2U);
}

TEST_F(ClickHouseLive, ProgressHeadersKeepALongAnswerAlive) {
  // A long query is not silence: the server sends a progress header while it works, and the bound
  // counts from the last one. Here a header a second for four seconds, under a two-second bound,
  // then the answer.
  std::vector<std::pair<std::chrono::milliseconds, std::string>> pieces{
      {std::chrono::milliseconds(0), "HTTP/1.1 200 OK\r\n"}};
  for (int n = 0; n < 4; ++n) {
    pieces.emplace_back(std::chrono::milliseconds(1000),
                        "X-ClickHouse-Progress: {\"read_rows\":\"" + std::to_string(n) + "\"}\r\n");
  }
  const std::string body = sde::live::texts_body({"name"}, {{"slow"}});
  pieces.emplace_back(std::chrono::milliseconds(500), "Content-Length: " + std::to_string(body.size()) +
                                                        "\r\nConnection: close\r\n\r\n" + body);
  sde::live::ScriptedServer server(
      {sde::live::version_answer("24.8.14.39"), sde::live::Reply::paced(std::move(pieces))});
  sde::ClickHouseEngine ch("clickhouse://fixture:canary@127.0.0.1:" + std::to_string(server.port()) +
                           "/default?send_receive_timeout=2");
  ch.connect();
  const auto started = Clock::now();
  EXPECT_EQ(ch.get("events", {{"name", std::string("slow")}}),
            (sde::Row{{"name", std::string("slow")}}));
  EXPECT_GE(std::chrono::duration<double>(Clock::now() - started).count(), 4.0);
}

TEST_F(ClickHouseLive, AWrongPasswordRevealsNothingTheServerSaid) {
  const std::string wrong = sde::live::with_login(dsn_, "default", "not-the-password");
  EXPECT_EQ(message_of([&] { sde::ClickHouseEngine(wrong).connect(); }),
            "could not connect to ClickHouse with the configured transport");
}

TEST_F(ClickHouseLive, ASecondThreadIsRefusedWhileOneIsInside) {
  sde::live::SilentServer silent;
  sde::ClickHouseEngine ch("clickhouse://default:sde@127.0.0.1:" + std::to_string(silent.port()) +
                           "/sde?send_receive_timeout=2");
  std::future<std::string> first = std::async(std::launch::async, [&] { return message_of([&] { ch.connect(); }); });
  std::this_thread::sleep_for(std::chrono::milliseconds(300));
  try {
    ch.connect();
    ADD_FAILURE() << "a second thread entered the adapter";
  } catch (const sde::ResourceBusy& error) {
    EXPECT_STREQ(error.what(), "the connection already has an operation in progress");
  }
  EXPECT_NE(first.get(), "accepted");
}

TEST_F(ClickHouseLive, AFirstExchangeTheServerClosesIsNotReplayed) {
  FirstBytes endpoint;
  EXPECT_NE(message_of([&] {
              sde::ClickHouseEngine("clickhouse://fixture:canary@127.0.0.1:" + std::to_string(endpoint.port()) +
                                    "/default")
                  .connect();
            }),
            "accepted");
  EXPECT_EQ(endpoint.seen(), std::vector<std::string>{"POST "});
}

TEST_F(ClickHouseLive, AValueTravelsInTheBodyAndNeverInTheUrl) {
  // A URL is what a proxy between the application and the server logs. The reference's driver
  // sends a statement as the body, and an INSERT's rows as the body under a statement that holds no
  // value. The server here answers the handshake and the insert's DESCRIBE, and closes the rest.
  const std::string canary = "canary-value-7c1e";
  sde::live::ScriptedServer server({sde::live::version_answer("24.8.14.39"), "",
                                    sde::live::texts_answer({"name", "type"}, {{"name", "String"}}),
                                    ""});
  sde::ClickHouseEngine ch("clickhouse://fixture:secret@127.0.0.1:" + std::to_string(server.port()) +
                           "/default");
  ch.connect();
  EXPECT_NE(message_of([&] { (void)ch.get("events", {{"name", canary}}); }), "accepted");
  EXPECT_NE(message_of([&] { ch.insert("events", {{"name", canary}}); }), "accepted");
  const std::vector<std::string> requests = server.requests();
  ASSERT_EQ(requests.size(), 4U) << "the handshake, the read, the DESCRIBE and the INSERT";
  for (std::size_t i = 0; i < requests.size(); ++i) {
    const std::string line = requests[i].substr(0, requests[i].find("\r\n"));
    EXPECT_EQ(line.find(canary), std::string::npos) << line;
    EXPECT_EQ(line.find("secret"), std::string::npos) << line;
    // `wait_end_of_query=1` is a setting; the statement would be `&query=`.
    EXPECT_EQ(line.find("&query="), i == 3 ? line.find("&query=INSERT") : std::string::npos) << line;
  }
  EXPECT_NE(requests[1].find("\r\n\r\nSELECT * FROM `events` FINAL WHERE `name` = "
                             "'canary-value-7c1e' LIMIT 1 FORMAT RowBinaryWithNamesAndTypes"),
            std::string::npos)
      << requests[1];
  EXPECT_NE(requests[3].find("query=INSERT%20INTO%20%60events%60"), std::string::npos) << requests[3];
  EXPECT_NE(requests[3].find(canary, requests[3].find("\r\n\r\n")), std::string::npos)
      << "the row is the body";
}

TEST_F(ClickHouseLive, ATlsSchemeSendsAClientHelloAndNeverAPlainRequest) {
  for (const auto& [scheme, query] : std::vector<std::pair<std::string, std::string>>{
           {"https", ""}, {"clickhouses", ""}, {"clickhouse", "?secure=true"}}) {
    FirstBytes endpoint;
    const std::string dsn = scheme + "://fixture:canary@127.0.0.1:" + std::to_string(endpoint.port()) +
                            "/default" + query;
    EXPECT_NE(message_of([&] { sde::ClickHouseEngine(dsn).connect(); }), "accepted") << dsn;
    const std::vector<std::string> seen = endpoint.seen();
    ASSERT_FALSE(seen.empty()) << dsn;
    for (const std::string& prefix : seen) EXPECT_EQ(prefix[0], '\x16') << dsn;
  }
}

TEST_F(ClickHouseLive, ATransportSettingThatBreaksARuleIsRefusedBeforeAnySocket) {
  FirstBytes endpoint;
  for (const std::string& option :
       std::vector<std::string>{"verify=false", "verify=tru", "verify=", "secure=TRUE", "unknown=setting"}) {
    EXPECT_NE(message_of([&] {
                sde::ClickHouseEngine("https://fixture:canary@127.0.0.1:" + std::to_string(endpoint.port()) +
                                      "/default?" + option)
                    .connect();
              }),
              "accepted")
        << option;
  }
  EXPECT_TRUE(endpoint.seen().empty());
}

TEST_F(ClickHouseLive, ACaThatIsNotCertificatesIsRefusedBeforeAnySocket) {
  FirstBytes endpoint;
  const std::string directory = "/tmp";
  for (const std::string& path : std::vector<std::string>{"/nonexistent/ca.pem", directory, "/dev/null",
                                                         "/etc/hostname"}) {
    EXPECT_EQ(message_of([&] {
                sde::ClickHouseEngine("https://fixture:canary@127.0.0.1:" + std::to_string(endpoint.port()) +
                                      "/default?ca_cert=" + path)
                    .connect();
              }),
              "could not connect to ClickHouse: ClickHouse CA file must contain readable valid PEM "
              "certificates within 1 MiB")
        << path;
  }
  EXPECT_TRUE(endpoint.seen().empty());
}

}  // namespace
