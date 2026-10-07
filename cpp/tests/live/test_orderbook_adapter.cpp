/// The orderbook adapter's decisions, batches and key-order reads, against a fake engine in this
/// process that speaks the protocol and answers as the engine was measured to: arrival order, LIMIT
/// on arrival, OB_ERR_NOT_FOUND, a sequence number per book (`live/orderbook_fake.hpp`). The same
/// decisions as the reference's `test_orderbook_tcp.py` and TypeScript's `orderbook.test.ts`, each
/// message the reference's; the engine itself is held by the live slice. Needs no DSN.

#include <unistd.h>

#include <algorithm>
#include <cstdint>
#include <cstdlib>
#include <functional>
#include <set>
#include <string>
#include <thread>
#include <tuple>
#include <utility>
#include <vector>

#include <gtest/gtest.h>

#include "live/maps.hpp"
#include "live/orderbook_fake.hpp"
#include "sde/errors.hpp"
#include "sde/layout.hpp"
#include "sde/model.hpp"
#include "sde/orderbook.hpp"
#include "sde/query.hpp"
#include "sde/session.hpp"
#include "sde/telemetry.hpp"

namespace sde::detail::orderbook {
/// The suite's access to the scan's bounds, as the reference's tests patch its module constants.
struct Testing {
  static void limits(OrderbookEngine& engine, std::size_t chunk_rows, std::uint64_t first_window_ns) {
    engine.limits_ = ScanLimits{chunk_rows, first_window_ns};
  }
};
}  // namespace sde::detail::orderbook

namespace {

using sde::live::FakeOrderbook;
using sde::live::StoredRow;
using Levels = std::vector<std::tuple<std::int64_t, std::int64_t, std::int64_t>>;

const std::string kTable = "orderbook";

sde::Value text(const char* value) { return sde::Value(std::string(value)); }
sde::Value integer(std::int64_t value) { return sde::Value(value); }
/// A Python int past int64, as a document: the reference's tests pass `1 << 63`.
sde::Value big(const char* digits) { return sde::Value(sde::JsonDocument{sde::parse_json(digits)}); }

sde::Row row(std::vector<std::pair<std::string, sde::Value>> overrides = {}) {
  sde::Row values = {{"symbol", text("BTCUSDT")},   {"exchange", text("binance")},
                     {"timestamp_ns", integer(1000)}, {"side", text("bid")},
                     {"level", integer(0)},           {"price", integer(5000000)},
                     {"quantity", integer(3)},        {"order_count", integer(1)},
                     {"sequence_number", sde::Value()}};
  for (auto& [name, value] : overrides) values[name] = std::move(value);
  return values;
}

sde::Row key_of(const sde::Row& values) {
  sde::Row key;
  for (const std::string_view name : sde::ORDERBOOK_KEY) key.emplace(name, values.at(std::string(name)));
  return key;
}

sde::Model model() {
  std::string fields;
  for (const auto& [name, type] : sde::ORDERBOOK_SHAPE) {
    if (!fields.empty()) fields += ", ";
    fields += R"({"name": ")" + std::string(name) + R"(", "type": ")" + std::string(type) +
              R"(", "nullable": )" + (name == "sequence_number" ? "true" : "false") + "}";
  }
  return sde::load_neutral_model(R"({"name": "market", "entities": [{"name": "DepthLevel", "fields": [)" +
                                 fields +
                                 R"(], "key": ["symbol", "exchange", "timestamp_ns", "side", "level"]}],
                                 "relations": [], "atomic": []})");
}

sde::ReadPlan plan(const sde::ReadOptions& options) {
  const sde::Model declared = model();
  const sde::Entity& entity = declared.entity("DepthLevel");
  std::vector<sde::ReadColumn> columns;
  for (const sde::Field& field : entity.fields) columns.push_back({field.name, field.type});
  return sde::plan_read(columns, entity.key, options);
}

sde::Row book(std::vector<std::pair<std::string, sde::Value>> more = {}) {
  sde::Row where = {{"symbol", text("BTCUSDT")}, {"exchange", text("binance")}};
  for (auto& [name, value] : more) where[name] = std::move(value);
  return where;
}

sde::ReadOptions reading(sde::Row where, sde::PageLimit limit = 100) {
  sde::ReadOptions options;
  options.where = std::move(where);
  options.limit = limit;
  return options;
}

/// What a call raised, `Class: text` for the two classes these tests expect, or `accepted`.
template <typename Body>
std::string failure_of(const Body& body) {
  try {
    body();
  } catch (const sde::QueryRefused& error) {
    return std::string("QueryRefused: ") + error.what();
  } catch (const sde::EngineError& error) {
    return std::string("EngineError: ") + error.what();
  }
  return "accepted";
}

bool contains(const std::string& text, const std::string& part) {
  return text.find(part) != std::string::npos;
}

using Position = std::tuple<std::int64_t, std::string, std::int64_t>;
std::vector<Position> positions(const std::vector<sde::Row>& rows) {
  std::vector<Position> out;
  for (const sde::Row& item : rows) {
    out.emplace_back(std::get<std::int64_t>(item.at("timestamp_ns")),
                     std::get<std::string>(item.at("side")), std::get<std::int64_t>(item.at("level")));
  }
  return out;
}

/// A fake engine, an adapter connected to it, and every event the adapter logged.
struct Rig {
  explicit Rig(const std::string& query = "") {
    sde::OrderbookOptions options;
    options.log = [this](std::string_view event, const sde::Json& fields) {
      events.emplace_back(std::string(event), sde::dump_json(fields));
    };
    engine = std::make_unique<sde::OrderbookEngine>(fake.dsn(query), std::move(options));
    engine->connect();
  }
  /// Six updates of one book written out of event-time order, as a feed with corrections does.
  void six_updates() {
    for (const std::uint64_t stamp : {3000U, 1000U, 2000U}) {
      for (const auto& [side, base] : {std::pair{1, 101}, std::pair{0, 99}}) {
        fake.store("BTCUSDT", "binance", side, stamp,
                   Levels{{base + static_cast<std::int64_t>(stamp), 1, 1},
                          {base + static_cast<std::int64_t>(stamp) + 1, 2, 1}});
      }
    }
  }
  std::vector<std::string> logged(const std::string& event) const {
    std::vector<std::string> out;
    for (const auto& [name, fields] : events) {
      if (name == event) out.push_back(fields);
    }
    return out;
  }

  FakeOrderbook fake;
  std::vector<std::pair<std::string, std::string>> events;
  std::unique_ptr<sde::OrderbookEngine> engine;
};

const std::vector<Position> kSixUpdatesInKeyOrder = {
    {1000, "ask", 0}, {1000, "ask", 1}, {1000, "bid", 0}, {1000, "bid", 1},
    {2000, "ask", 0}, {2000, "ask", 1}, {2000, "bid", 0}, {2000, "bid", 1},
    {3000, "ask", 0}, {3000, "ask", 1}, {3000, "bid", 0}, {3000, "bid", 1}};

// --- connecting --------------------------------------------------------------------------------------

TEST(OrderbookAdapter, ASettingThatWouldDoNothingIsRefusedAndTheSecretIsNeverSaid) {
  EXPECT_EQ(failure_of([] { sde::OrderbookEngine("orderbook://h:1?ca=/ca.pem"); }),
            "EngineError: tls_ca_file without tls=True verifies nothing: the connection would be "
            "plain text");
  EXPECT_EQ(failure_of([] { sde::OrderbookEngine("orderbook://h:1?verify=off"); }),
            "EngineError: tls_verify=False without tls=True: there is no certificate to decline to "
            "check");
  for (const char* dsn : {"orderbook://identity:@h:1", "orderbook://two%20words:secret@h:1"}) {
    EXPECT_TRUE(contains(failure_of([&] { sde::OrderbookEngine{dsn}; }), "two non-empty strings"));
  }
  EXPECT_TRUE(contains(failure_of([] { sde::OrderbookEngine("orderbook://h:1?timeout=0"); }),
                       "timeout is a positive number of seconds"));
  const std::string refused =
      failure_of([] { sde::OrderbookEngine("orderbook://app:TOPSECRET@h:1?mode=x"); });
  EXPECT_FALSE(contains(refused, "TOPSECRET"));
  const sde::OrderbookEngine engine("orderbook://app:TOPSECRET@h:1?tls=on");
  EXPECT_EQ(engine.describe(), "OrderbookEngine(tcp h:1, identity='app', tls=on)");
  EXPECT_EQ(sde::OrderbookEngine("orderbook://127.0.0.1:9090").describe(),
            "OrderbookEngine(tcp 127.0.0.1:9090, tls=off)");
  EXPECT_FALSE(engine.connected());
}

TEST(OrderbookAdapter, ItAuthenticatesAndAWrongSecretIsRefusedByName) {
  FakeOrderbook fake;
  fake.require_auth("desk", "the-secret");
  const std::string port = std::to_string(fake.port());
  sde::OrderbookEngine engine("orderbook://desk:the-secret@127.0.0.1:" + port);
  engine.connect();
  EXPECT_TRUE(engine.connected());
  engine.insert(kTable, row());
  EXPECT_EQ(fake.rows().size(), 1U);
  sde::OrderbookEngine wrong("orderbook://desk:not-it@127.0.0.1:" + port);
  const std::string refused = failure_of([&] { wrong.connect(); });
  EXPECT_EQ(refused, "EngineError: could not open the orderbook engine: Authentication failed: ERR "
                     "auth_failed");
  EXPECT_FALSE(contains(refused, "not-it"));
}

TEST(OrderbookAdapter, AServerThatCannotStoreAnEventTimeIsRefusedBeforeAnyWrite) {
  FakeOrderbook fake;
  fake.capabilities("strict_args");
  sde::OrderbookEngine engine(fake.dsn());
  EXPECT_EQ(failure_of([&] { engine.connect(); }),
            "EngineError: this orderbook server cannot store a write's event time, so every update "
            "would be stamped with its arrival instead of the time it happened and could not be "
            "found by the time it carries. Upgrade the server; nothing was written.");
  std::this_thread::sleep_for(std::chrono::milliseconds(100));
  EXPECT_EQ(fake.commands(), (std::vector<std::string>{"STATUS", "QUIT"}))
      << "the connection it opened to ask is closed again";
  EXPECT_EQ(failure_of([&] { engine.insert(kTable, row()); }),
            "EngineError: not connected; call connect() first");
}

TEST(OrderbookAdapter, AServerThatCannotSayItsCapabilitiesIsRefusedWithWhatItSaid) {
  sde::live::ProtocolServer server([](sde::live::Peer& peer) {
    if (!peer.send(sde::live::kGreeting) || !peer.line()) return;
    (void)peer.send("ERR busy\n");
    (void)peer.drain(std::chrono::milliseconds(200));
  });
  sde::OrderbookEngine engine("orderbook://127.0.0.1:" + std::to_string(server.port()));
  EXPECT_EQ(failure_of([&] { engine.connect(); }),
            "EngineError: could not read the orderbook server's capabilities: STATUS error: busy");
}

// --- what the engine can store -------------------------------------------------------------------------

TEST(OrderbookAdapter, AValueTheEngineCannotStoreIsRefusedBeforeSending) {
  Rig rig;
  const std::vector<std::pair<std::pair<std::string, sde::Value>, std::string>> cases = {
      {{"timestamp_ns", integer(-1)}, "timestamp_ns -1 is outside what this engine stores (0 to "
                                      "9223372036854775807). Refused before sending: the server "
                                      "would refuse it with a message about tokens, or store "
                                      "something else."},
      {{"timestamp_ns", big("9223372036854775808")}, "timestamp_ns 9223372036854775808 is outside"},
      {{"quantity", integer(-5)}, "quantity -5 is outside"},
      {{"order_count", integer(-1)}, "order_count -1 is outside"},
      {{"order_count", integer(std::int64_t{1} << 31)}, "order_count 2147483648 is outside"},
      {{"price", big("9223372036854775808")}, "price 9223372036854775808 is outside"},
      {{"quantity", sde::Value(true)}, "quantity must be an integer, not bool"},
      {{"price", sde::Value(1.5)}, "price must be an integer, not float"},
      {{"symbol", text("BTC USDT")}, "'BTC USDT' cannot be used as a symbol: this engine's query "
                                     "language has no escape sequence inside a string literal and "
                                     "its wire protocol separates fields by whitespace, so a quote, "
                                     "a backslash, a space or a control character cannot be stored "
                                     "or addressed faithfully. Refused rather than escaped, because "
                                     "the escaping one would reach for would address a different "
                                     "book without saying so."},
      {{"symbol", text("")}, "symbol must be a non-empty string, not ''"},
      {{"exchange", text("bin\tance")}, "'bin\\tance' cannot be used as a exchange"},
      {{"symbol", text("BTC'USDT")}, "no escape sequence"},
      {{"symbol", text("BTC\xC2\xA0USDT")}, "cannot be used as a symbol"},
      {{"symbol", text("BTC\xE2\x80\x8BUSDT")}, "cannot be used as a symbol"},
  };
  for (const auto& [override_, message] : cases) {
    SCOPED_TRACE(message);
    EXPECT_TRUE(contains(failure_of([&] { rig.engine->insert(kTable, row({override_})); }), message));
  }
  EXPECT_TRUE(rig.fake.writes().empty());
}

TEST(OrderbookAdapter, ALevelPastTheEnginesDepthIsRefusedWhole) {
  Rig rig;
  std::vector<sde::Row> rows;
  for (std::int64_t level = 0; level <= 1000; ++level) rows.push_back(row({{"level", integer(level)}}));
  EXPECT_TRUE(contains(failure_of([&] { rig.engine->insert_many(kTable, rows); }),
                       "row 1000: level 1000 is outside what this engine stores (0 to 999)"));
  EXPECT_TRUE(rig.fake.writes().empty());
}

TEST(OrderbookAdapter, ANegativePriceIsStoredBecauseTheEngineStoresOne) {
  Rig rig;
  rig.engine->insert(kTable, row({{"price", integer(-37630000)}}));
  EXPECT_EQ(rig.fake.rows().at(0).price, -37630000);
}

TEST(OrderbookAdapter, AWriteGenerationHasNowhereToGoAndIsRefused) {
  Rig rig;
  EXPECT_EQ(failure_of([&] { rig.engine->insert(kTable, row({{std::string(sde::EPOCH_COLUMN), integer(1)}})); }),
            "EngineError: insert into orderbook carries ['__sde_write_epoch'], which this engine "
            "has nowhere to store: its shape is fixed. A write generation in particular is never "
            "stamped here: a group on this engine carries no write generation.");
  EXPECT_TRUE(contains(failure_of([&] {
                         rig.engine->insert_many(kTable, {row({{std::string(sde::EPOCH_COLUMN), integer(1)}})});
                       }),
                       "row 0: carries ['__sde_write_epoch'], which this engine has nowhere to "
                       "store; a group on it carries no write generation"));
  EXPECT_EQ(failure_of([&] { rig.engine->insert(kTable, row({{"level", integer(1)}})); })
                .substr(0, 86),
            "EngineError: insert into orderbook declares level 1, and this engine's write API has n");
  sde::Row missing = row();
  missing.erase("quantity");
  EXPECT_EQ(failure_of([&] { rig.engine->insert(kTable, missing); }),
            "EngineError: insert into orderbook is missing ['quantity']. Every field of the fixed "
            "shape is required: this engine has no defaults to fall back on and no nullable columns "
            "except the sequence number.");
  EXPECT_EQ(failure_of([&] { rig.engine->insert("another", row()); }),
            "EngineError: this engine has one table, 'orderbook', not 'another'");
  EXPECT_TRUE(rig.fake.writes().empty());
}

// --- the sequence number belongs to the server ----------------------------------------------------------

TEST(OrderbookAdapter, AChosenSequenceNumberIsRefusedBeforeSendingAndTheServersComesBack) {
  Rig rig;
  const std::string server_owns =
      "EngineError: over TCP the sequence number is the server's: it numbers every update per "
      "book, and a number chosen here would be refused by a current server and silently replaced "
      "by an older one, so the row read back would disagree with the row written. Write "
      "sequence_number as null and read the server's number back.";
  EXPECT_EQ(failure_of([&] { rig.engine->insert(kTable, row({{"sequence_number", integer(41)}})); }),
            server_owns);
  EXPECT_EQ(failure_of([&] {
              rig.engine->insert_many(kTable, {row({{"sequence_number", integer(41)}})});
            }),
            server_owns);
  EXPECT_TRUE(rig.fake.writes().empty());
  rig.engine->insert(kTable, row());
  EXPECT_EQ(rig.fake.writes(), std::vector<std::string>{"INSERT BTCUSDT binance bid 5000000 3 1 1000"});
  const std::optional<sde::Row> back = rig.engine->get(kTable, key_of(row()));
  ASSERT_TRUE(back.has_value());
  EXPECT_EQ(std::get<std::int64_t>(back->at("sequence_number")), 1);
  // A document's null is Python's None, and goes out as nothing.
  rig.engine->insert(kTable, row({{"sequence_number", big("null")}, {"timestamp_ns", integer(2000)}}));
  EXPECT_EQ(rig.fake.writes().size(), 2U);
}

TEST(OrderbookAdapter, AnUnconfirmedWriteSaysItMayHaveBeenStoredAndIsLogged) {
  Rig rig;
  rig.fake.drop_on_write(1);
  EXPECT_EQ(failure_of([&] { rig.engine->insert(kTable, row()); }),
            "EngineError: the engine did not confirm the write to orderbook: TCP connection closed "
            "by server. If the connection dropped after the update was sent, it may still have been "
            "stored: read the book before writing it again.");
  EXPECT_EQ(rig.logged("sde.write.failed"),
            std::vector<std::string>{R"({"table":"orderbook","error":"OrderbookError"})"});
  // The connection is out of step: every call says why, until connect opens another.
  const std::string why = failure_of([&] { rig.engine->insert(kTable, row()); });
  EXPECT_TRUE(contains(why, "was closed because an exchange (INSERT) did not finish: TCP connection "
                            "closed by server."))
      << why;
  EXPECT_EQ(rig.logged("sde.write.failed").size(), 1U) << "a write never sent is not a failed one";
  EXPECT_TRUE(contains(failure_of([&] { (void)rig.engine->levels("BTCUSDT", "binance"); }),
                       "query failed: SELECT * FROM 'BTCUSDT'.'binance' LIMIT 100001: the "
                       "connection to 127.0.0.1:"));
  EXPECT_FALSE(rig.engine->connected());
  rig.engine->connect();
  EXPECT_TRUE(rig.engine->connected());
  rig.engine->insert(kTable, row());
  EXPECT_EQ(rig.fake.rows().size(), 1U);
}

TEST(OrderbookAdapter, AServersRefusalOfOneWriteIsNamedWithItsWords) {
  Rig rig;
  rig.fake.refuse_write(0);
  EXPECT_EQ(failure_of([&] { rig.engine->insert(kTable, row()); }),
            "EngineError: the engine did not confirm the write to orderbook: TCP INSERT failed: "
            "something the server said. If the connection dropped after the update was sent, it may "
            "still have been stored: read the book before writing it again.");
  EXPECT_TRUE(rig.engine->connected()) << "a refusal leaves the connection in step";
  rig.fake.refuse_write(1);
  EXPECT_TRUE(contains(failure_of([&] {
                         rig.engine->insert_levels(kTable, "BTCUSDT", "binance", "ask", 7,
                                                   {{1, 1, 1}, {2, 2, 2}});
                       }),
                       "TCP MINSERT failed: something the server said"));
}

// --- batches are the engine's updates --------------------------------------------------------------------

TEST(OrderbookAdapter, ABatchBecomesUpdatesInTheOrderTheyFirstAppear) {
  Rig rig;
  rig.engine->insert_many(kTable, {row({{"side", text("ask")}, {"level", integer(1)}, {"price", integer(101)}}),
                                   row({{"side", text("bid")}, {"level", integer(0)}, {"price", integer(99)}}),
                                   row({{"side", text("ask")}, {"level", integer(0)}, {"price", integer(100)}}),
                                   row({{"side", text("bid")}, {"level", integer(1)}, {"price", integer(98)}}),
                                   row({{"timestamp_ns", integer(2000)}, {"price", integer(97)}})});
  EXPECT_EQ(rig.fake.writes(), (std::vector<std::string>{
                                   "MINSERT BTCUSDT binance ask 2 1000\n100 3 1\n101 3 1",
                                   "MINSERT BTCUSDT binance bid 2 1000\n99 3 1\n98 3 1",
                                   "INSERT BTCUSDT binance bid 97 3 1 2000"}));
}

TEST(OrderbookAdapter, UpdatesGoInRoundTripsOfSixtyFour) {
  // The second round trip fails: the first 64 were confirmed, and the rest is unknown.
  Rig rig;
  rig.fake.drop_on_write(70);
  std::vector<sde::Row> rows;
  for (std::int64_t stamp = 0; stamp < 130; ++stamp) rows.push_back(row({{"timestamp_ns", integer(stamp)}}));
  EXPECT_EQ(failure_of([&] { rig.engine->insert_many(kTable, rows); }),
            "EngineError: the outcome of this batch is unknown: 64 of its 130 updates were "
            "confirmed, and the connection failed on the next 64 (OrderbookError). Those may have "
            "been stored in full, in part or not at all; read the book before writing them again, "
            "then connect() again.");
  EXPECT_EQ(rig.logged("sde.write.failed"),
            std::vector<std::string>{R"({"table":"orderbook","error":"OrderbookError"})"});
  EXPECT_FALSE(rig.engine->connected());
  EXPECT_EQ(failure_of([&] { rig.engine->insert(kTable, row()); }),
            "EngineError: not connected; call connect() first");
}

TEST(OrderbookAdapter, AnUpdateWithAGapOrARepeatedLevelIsRefusedWhole) {
  Rig rig;
  EXPECT_EQ(failure_of([&] {
              rig.engine->insert_many(kTable, {row({{"level", integer(0)}}), row({{"level", integer(2)}})});
            }),
            "EngineError: the update for BTCUSDT binance bid at 1000 has levels [0, 2]. An update is "
            "levels 0 to n-1 without a gap: the engine numbers levels by position, so a missing one "
            "would renumber every level after it.");
  EXPECT_TRUE(contains(failure_of([&] { rig.engine->insert_many(kTable, {row({{"level", integer(1)}})}); }),
                       "has levels [1]"));
  EXPECT_EQ(failure_of([&] {
              rig.engine->insert_many(kTable, {row({{"price", integer(1)}}), row({{"price", integer(2)}})});
            }),
            "EngineError: rows 0 and 1 are both level 0 of one update (BTCUSDT binance bid at 1000). "
            "An update holds one price per level, so the second would have nowhere to go.");
  EXPECT_TRUE(contains(failure_of([&] { rig.engine->insert_many(kTable, {row({{"side", text("mid")}})}); }),
                       "row 0: side must be one of ['ask', 'bid'], not 'mid'"));
  EXPECT_TRUE(rig.fake.writes().empty()) << "every refusal that can be decided before sending is";
}

TEST(OrderbookAdapter, ARefusalByTheServerNamesTheUpdateAndWhatWasStored) {
  Rig rig;
  rig.fake.refuse_write(65);
  std::vector<sde::Row> rows;
  for (std::int64_t stamp = 0; stamp < 130; ++stamp) rows.push_back(row({{"timestamp_ns", integer(stamp)}}));
  EXPECT_EQ(failure_of([&] { rig.engine->insert_many(kTable, rows); }),
            "EngineError: the server refused 1 of 130 updates in this batch; the first was update 65 "
            "(rows [65]): something the server said. 127 updates were stored - a batch here is not "
            "a transaction - and any after this part of the batch were not sent.");
  EXPECT_EQ(rig.fake.writes().size(), 128U) << "nothing after the refused part was sent";
  EXPECT_EQ(rig.logged("sde.write.failed"),
            std::vector<std::string>{R"({"table":"orderbook","error":"refused"})"});
  EXPECT_TRUE(rig.engine->connected());
}

TEST(OrderbookAdapter, AWriteWithoutAConnectionIsRefusedBeforeAnythingIsSent) {
  // The reference reads its client inside the write's own failure, so it reports a write it never
  // sent as one that may have been stored (and a batch before the first connect fails on an
  // attribute of None). Here the write is refused first, and nothing is logged as a failed write.
  FakeOrderbook fake;
  std::vector<std::pair<std::string, std::string>> events;
  sde::OrderbookOptions options;
  options.log = [&](std::string_view event, const sde::Json&) { events.emplace_back(event, ""); };
  sde::OrderbookEngine engine(fake.dsn(), std::move(options));
  EXPECT_EQ(failure_of([&] { engine.insert(kTable, row()); }),
            "EngineError: not connected; call connect() first");
  EXPECT_EQ(failure_of([&] { engine.insert_many(kTable, {row()}); }),
            "EngineError: not connected; call connect() first");
  EXPECT_EQ(failure_of([&] { engine.insert_levels(kTable, "A", "B", "bid", 1, {{1, 1, 1}}); }),
            "EngineError: not connected; call connect() first");
  engine.insert_many(kTable, {});  // nothing to send, nothing refused
  // A refusal of the rows comes first, as in the reference.
  EXPECT_TRUE(contains(failure_of([&] { engine.insert(kTable, row({{"level", integer(1)}})); }),
                       "declares level 1"));
  // A read says what the reference says, inside its own failure.
  EXPECT_EQ(failure_of([&] { (void)engine.levels("A", "B"); }),
            "EngineError: query failed: SELECT * FROM 'A'.'B' LIMIT 100001: not connected; call "
            "connect() first");
  EXPECT_TRUE(events.empty());
  EXPECT_EQ(fake.connections(), 0);
}

// --- reads in key order, from an engine that answers in arrival order ----------------------------------

TEST(OrderbookAdapter, AScanAnswersInKeyOrderWhateverTheArrivalOrder) {
  Rig rig;
  rig.six_updates();
  EXPECT_EQ(positions(rig.engine->select_rows(kTable, plan(reading(book())))), kSixUpdatesInKeyOrder);
  sde::ReadOptions backwards = reading(book());
  backwards.descending = true;
  std::vector<Position> reversed = kSixUpdatesInKeyOrder;
  std::reverse(reversed.begin(), reversed.end());
  EXPECT_EQ(positions(rig.engine->select_rows(kTable, plan(backwards))), reversed);
  const std::vector<sde::Row> rows = rig.engine->select_rows(kTable, plan(reading(book(), 3)));
  ASSERT_EQ(rows.size(), 4U) << "one more than the page, so Session::scan knows there is another";
  const sde::Row expected = {{"symbol", text("BTCUSDT")},   {"exchange", text("binance")},
                             {"timestamp_ns", integer(1000)}, {"side", text("ask")},
                             {"level", integer(0)},           {"price", integer(1101)},
                             {"quantity", integer(1)},        {"order_count", integer(1)},
                             {"sequence_number", integer(3)}};
  EXPECT_EQ(rows[0], expected);
}

TEST(OrderbookAdapter, APageReturnsLimitPlusOneAndTheNextPageStartsAfterIt) {
  Rig rig;
  rig.six_updates();
  const std::vector<sde::Row> first = rig.engine->select_rows(kTable, plan(reading(book(), 5)));
  ASSERT_EQ(first.size(), 6U);
  sde::ReadOptions next = reading(book());
  next.after = key_of(first[4]);
  const std::vector<sde::Row> second = rig.engine->select_rows(kTable, plan(next));
  std::vector<Position> joined = positions({first.begin(), first.begin() + 5});
  for (const Position& position : positions(second)) joined.push_back(position);
  EXPECT_EQ(joined, kSixUpdatesInKeyOrder);
  sde::ReadOptions back = reading(book(), 2);
  back.descending = true;
  back.after = key_of(first[4]);  // (2000, ask, 0)
  EXPECT_EQ(positions(rig.engine->select_rows(kTable, plan(back))),
            (std::vector<Position>{{1000, "bid", 1}, {1000, "bid", 0}, {1000, "ask", 1}}));
}

TEST(OrderbookAdapter, FiltersOnSideLevelPriceAndTimeReachTheEngineWhereTheyCan) {
  Rig rig;
  rig.six_updates();
  sde::ReadOptions narrowed = reading(book({{"side", text("ask")}, {"level", integer(1)}}), 10);
  narrowed.bounds = sde::Range{"timestamp_ns", integer(1500), integer(3000)};
  const std::vector<sde::Row> rows = rig.engine->select_rows(kTable, plan(narrowed));
  ASSERT_EQ(rows.size(), 1U);
  EXPECT_EQ(std::get<std::int64_t>(rows[0].at("timestamp_ns")), 2000);
  EXPECT_EQ(std::get<std::int64_t>(rows[0].at("price")), 2102);
  for (const std::string& query : rig.fake.queries()) {
    EXPECT_TRUE(contains(query, "BETWEEN 1500 AND 2999")) << query;
  }
  const std::vector<sde::Row> priced =
      rig.engine->select_rows(kTable, plan(reading(book({{"price", integer(1099)}}), 10)));
  EXPECT_EQ(positions(priced), (std::vector<Position>{{1000, "bid", 0}}));
  EXPECT_TRUE(contains(rig.fake.queries().back(), "AND price BETWEEN 1099 AND 1099"));
  sde::ReadOptions low = reading(book(), 10);
  low.bounds = sde::Range{"price", sde::Value(), integer(1102)};
  (void)rig.engine->select_rows(kTable, plan(low));
  EXPECT_TRUE(contains(rig.fake.queries().back(), "AND price BETWEEN -9223372036854775808 AND 1101"));
}

TEST(OrderbookAdapter, AReadTheEngineCannotAnswerIsRefusedBeforeAnyQuery) {
  Rig rig;
  sde::ReadOptions ranged = reading(book(), 10);
  ranged.bounds = sde::Range{"level", integer(0), integer(3)};
  sde::ReadOptions ordered = reading(book(), 10);
  ordered.order_by = "price";
  const std::vector<std::pair<sde::ReadOptions, std::string>> cases = {
      {reading({{"symbol", text("BTCUSDT")}}, 10),
       "QueryRefused: a read here names one book: where= must fix both symbol and exchange. The "
       "engine's query language takes them in its FROM clause, so there is no scan across books - "
       "a property of an engine built for one workload, not a limitation to route around."},
      {reading(book({{"quantity", integer(1)}}), 10),
       "QueryRefused: this engine cannot filter on ['quantity']: its query language selects one "
       "book by symbol and exchange and narrows it by time and price. Reading a book's history to "
       "filter it here would be a full scan dressed as a query."},
      {ranged, "QueryRefused: this engine has no range over ['level']; it bounds a read by "
               "timestamp_ns or price"},
      {ordered, "QueryRefused: this engine answers one book in time order (then side, then level), "
                "and this read asks for ['price', 'timestamp_ns', 'side', 'level']. Ordering a "
                "book's history by anything else would mean reading all of it first."}};
  for (const auto& [options, message] : cases) {
    EXPECT_EQ(failure_of([&] { (void)rig.engine->select_rows(kTable, plan(options)); }), message);
  }
  EXPECT_TRUE(rig.fake.queries().empty());
}

TEST(OrderbookAdapter, AReadThatMatchesNothingAsksNothing) {
  Rig rig;
  sde::ReadOptions empty = reading(book());
  empty.bounds = sde::Range{"timestamp_ns", integer(5), integer(5)};
  EXPECT_TRUE(rig.engine->select_rows(kTable, plan(empty)).empty());
  EXPECT_TRUE(rig.engine->select_rows(kTable, plan(reading(book({{"side", text("mid")}})))).empty());
  sde::ReadOptions prices = reading(book());
  prices.bounds = sde::Range{"price", integer(10), integer(10)};
  EXPECT_TRUE(rig.engine->select_rows(kTable, plan(prices)).empty());
  // Nothing can match, so the book's name is not even checked, as in the reference.
  EXPECT_TRUE(rig.engine->select_rows(kTable, plan(reading({{"symbol", text("bad name")},
                                                            {"exchange", text("x")},
                                                            {"side", text("mid")}})))
                  .empty());
  // A key no row can have is answered without asking: a negative time, a side that is neither.
  EXPECT_FALSE(rig.engine->get(kTable, key_of(row({{"timestamp_ns", integer(-1)}}))).has_value());
  EXPECT_FALSE(rig.engine->get(kTable, key_of(row({{"side", text("mid")}}))).has_value());
  EXPECT_FALSE(rig.engine->get(kTable, key_of(row({{"timestamp_ns", big("9223372036854775808")}})))
                   .has_value());
  EXPECT_TRUE(rig.fake.queries().empty());
}

TEST(OrderbookAdapter, ABookNobodyHasWrittenToIsEmptyNotAnError) {
  Rig rig;
  EXPECT_TRUE(rig.engine->select_rows(kTable, plan(reading({{"symbol", text("NEW")},
                                                            {"exchange", text("X")}}, 10)))
                  .empty());
  EXPECT_FALSE(rig.engine->get(kTable, key_of(row({{"symbol", text("NEW")}, {"exchange", text("X")}})))
                   .has_value());
  EXPECT_TRUE(rig.engine->levels("NEW", "X").empty());
}

TEST(OrderbookAdapter, ARowOutsideTheModelsTypesIsRefusedOnReadWithoutItsValue) {
  // The engine stores unsigned 64-bit values, which the model's int64 cannot all hold. Measured: an
  // engine before c1f14c0 read a stored quantity of 2^60 - 1 back as 2^64 - 1.
  struct Case {
    std::string field;
    StoredRow stored;
    std::string value;
  };
  const auto good = [](const std::string& symbol) {
    StoredRow stored;
    stored.symbol = symbol;
    stored.exchange = "binance";
    stored.timestamp = 1000;
    stored.price = 10;
    stored.quantity = "5";
    return stored;
  };
  std::vector<Case> cases;
  cases.push_back({"quantity", good("A"), "18446744073709551615"});
  cases.back().stored.quantity = cases.back().value;
  cases.push_back({"quantity", good("B"), "9223372036854775808"});
  cases.back().stored.quantity = cases.back().value;
  cases.push_back({"timestamp_ns", good("C"), "9223372036854775808"});
  cases.back().stored.timestamp = 9223372036854775808ULL;
  cases.push_back({"order_count", good("D"), "2147483648"});
  cases.back().stored.count = cases.back().value;
  cases.push_back({"level", good("E"), "1000"});
  cases.back().stored.level = cases.back().value;
  cases.push_back({"sequence_number", good("F"), "9223372036854775808"});
  cases.back().stored.sequence = cases.back().value;
  Rig rig;
  for (const Case& item : cases) {
    SCOPED_TRACE(item.field);
    rig.fake.add_row(item.stored);
    const std::string refused = failure_of([&] {
      (void)rig.engine->select_rows(kTable, plan(reading({{"symbol", text(item.stored.symbol.c_str())},
                                                          {"exchange", text("binance")}})));
    });
    EXPECT_TRUE(contains(refused, "the engine returned a row whose " + item.field + " is outside"))
        << refused;
    EXPECT_FALSE(contains(refused, item.value)) << "a value read back is data and stays out";
  }
}

TEST(OrderbookAdapter, TheEdgesOfTheModelsTypesReadBack) {
  // The control for the refusal above: the largest value of each type is a value, and an unknown
  // sequence number - the engine's 0 - is read as unknown, not refused.
  Rig rig;
  StoredRow edge;
  edge.symbol = "BTCUSDT";
  edge.exchange = "binance";
  edge.timestamp = 9223372036854775807ULL;
  edge.level = "999";
  edge.price = INT64_MIN;
  edge.quantity = "9223372036854775807";
  edge.count = "2147483647";
  edge.sequence = "0";
  rig.fake.add_row(edge);
  const std::vector<sde::Row> rows = rig.engine->select_rows(kTable, plan(reading(book())));
  ASSERT_EQ(rows.size(), 1U);
  EXPECT_EQ(std::get<std::int64_t>(rows[0].at("quantity")), INT64_MAX);
  EXPECT_EQ(std::get<std::int64_t>(rows[0].at("order_count")), 2147483647);
  EXPECT_EQ(std::get<std::int64_t>(rows[0].at("level")), 999);
  EXPECT_EQ(std::get<std::int64_t>(rows[0].at("price")), INT64_MIN);
  EXPECT_TRUE(std::holds_alternative<sde::Null>(rows[0].at("sequence_number")));
}

TEST(OrderbookAdapter, TwoRowsWithOneKeyAreRefusedInAPageAndInGet) {
  Rig rig;
  rig.fake.store("BTCUSDT", "binance", 0, 7, Levels{{1, 1, 1}});
  rig.fake.store("BTCUSDT", "binance", 0, 7, Levels{{2, 1, 1}});
  const std::string duplicate =
      "2 rows in orderbook share the key {'symbol': 'BTCUSDT', 'exchange': 'binance', "
      "'timestamp_ns': 7, 'side': 'bid', 'level': 0}. This engine is an append-only log of depth "
      "updates and does not enforce a key, so this is a key violation it could not have prevented. "
      "Refused rather than answered with one of them: picking either would be a read that lies "
      "about uniqueness, and you would not see it.";
  EXPECT_EQ(failure_of([&] { (void)rig.engine->get(kTable, key_of(row({{"timestamp_ns", integer(7)}}))); }),
            "EngineError: " + duplicate);
  EXPECT_EQ(failure_of([&] { (void)rig.engine->select_rows(kTable, plan(reading(book()))); }),
            "EngineError: " + duplicate);
}

TEST(OrderbookAdapter, AWindowTooLargeIsSplitAndThePageStaysInKeyOrder) {
  Rig rig;
  sde::detail::orderbook::Testing::limits(*rig.engine, 4, 10000);
  for (const std::uint64_t stamp : {900U, 500U, 100U, 700U, 300U}) {
    rig.fake.store("BTCUSDT", "binance", 0, stamp,
                   Levels{{static_cast<std::int64_t>(stamp), 1, 1},
                          {static_cast<std::int64_t>(stamp) - 1, 1, 1}});
  }
  std::vector<Position> expected;
  for (const std::int64_t stamp : {100, 300, 500, 700, 900}) {
    for (const std::int64_t level : {0, 1}) expected.emplace_back(stamp, "bid", level);
  }
  EXPECT_EQ(positions(rig.engine->select_rows(kTable, plan(reading(book())))), expected);
  for (const std::string& query : rig.fake.queries()) {
    EXPECT_TRUE(query.ends_with("LIMIT 5")) << "never more than the cap in memory: " << query;
  }
}

TEST(OrderbookAdapter, OneInstantHoldingMoreThanTheCapIsRefused) {
  Rig rig;
  sde::detail::orderbook::Testing::limits(*rig.engine, 4, 1000000000);
  rig.fake.store("BTCUSDT", "binance", 0, 100, Levels{{5, 1, 1}, {4, 1, 1}, {3, 1, 1}, {2, 1, 1}, {1, 1, 1}});
  EXPECT_EQ(failure_of([&] { (void)rig.engine->select_rows(kTable, plan(reading(book()))); }),
            "EngineError: BTCUSDT binance holds more than 4 rows at the instant 100; that many "
            "updates at one nanosecond is a key collision on a scale this read cannot page through");
  EXPECT_EQ(failure_of([&] { (void)rig.engine->get(kTable, key_of(row({{"timestamp_ns", integer(100)}}))); }),
            "EngineError: this read of BTCUSDT binance holds more than 4 rows; bound it by time, "
            "give a limit, or page through it with Session.scan");
}

TEST(OrderbookAdapter, ASparseBookGrowsTheWindowInsteadOfAskingOncePerSecond) {
  // Logarithmic in the span of time: about eleven doublings of a second for two thousand seconds,
  // at most about 64 over the whole uint64 range.
  Rig rig;
  rig.fake.store("BTCUSDT", "binance", 0, 10ULL * 1000000000ULL, Levels{{1, 1, 1}});
  rig.fake.store("BTCUSDT", "binance", 0, 1000ULL * 1000000000ULL, Levels{{2, 1, 1}});
  sde::ReadOptions bounded = reading(book());
  bounded.bounds = sde::Range{"timestamp_ns", integer(0), integer(2000LL * 1000000000LL)};
  EXPECT_EQ(rig.engine->select_rows(kTable, plan(bounded)).size(), 2U);
  EXPECT_LE(rig.fake.queries().size(), 12U);
  rig.fake.forget_commands();
  EXPECT_EQ(rig.engine->select_rows(kTable, plan(reading(book()))).size(), 2U);
  EXPECT_LE(rig.fake.queries().size(), 65U);
  EXPECT_TRUE(contains(rig.fake.queries().back(), "AND 18446744073709551615 LIMIT"))
      << "the last window reaches the engine's last instant: " << rig.fake.queries().back();
}

TEST(OrderbookAdapter, AReadFlushesFirstWhenSomethingWasWrittenAndSaysSo) {
  Rig rig;
  rig.engine->insert_many(kTable, {row(), row({{"level", integer(1)}})});
  (void)rig.engine->select_rows(kTable, plan(reading(book())));
  (void)rig.engine->select_rows(kTable, plan(reading(book())));
  std::vector<std::string> sent = rig.fake.commands();
  EXPECT_EQ(std::count(sent.begin(), sent.end(), "FLUSH"), 1) << "one flush, for one write";
  EXPECT_EQ(rig.logged("sde.orderbook.flushed"), std::vector<std::string>{R"({"rows":2})"});
}

TEST(OrderbookAdapter, AQueryAnsweredWithAnythingButStandardRowsIsRefusedInTheClientsWords) {
  for (const auto& [header, said] : std::vector<std::pair<std::string, std::string>>{
           {"name\tvalue\tscale\n", "this query returned aggregates, not rows; use query_agg() "
                                    "(columns: ['name', 'value', 'scale'])"},
           {"price\ttimestamp_ns\tquantity\torder_count\tside\tlevel\n",
            "this client reads the standard row columns by position and the server answered with "
            "['price', 'timestamp_ns', 'quantity', 'order_count', 'side', 'level']. Ask for `SELECT "
            "*`, or read the response with a client that reads columns by name."},
           {"timestamp_ns\tprice\tquantity\torder_count\tside\tlevel\tsequence_number\n1\tx\t1\t1\t0\t0\t1\n",
            "invalid literal for int() with base 10: 'x'"}}) {
    sde::live::ProtocolServer server([header = header](sde::live::Peer& peer) {
      if (!peer.send(sde::live::kGreeting) || !peer.line()) return;
      (void)peer.send("OK\ncapabilities: insert_event_time\n\n");
      if (!peer.line()) return;
      (void)peer.send("OK\n" + header + "\n");
      (void)peer.drain(std::chrono::milliseconds(200));
    });
    sde::OrderbookEngine engine("orderbook://127.0.0.1:" + std::to_string(server.port()));
    engine.connect();
    EXPECT_EQ(failure_of([&] { (void)engine.levels("A", "B", std::nullopt, std::nullopt, 7); }),
              "EngineError: query failed: SELECT * FROM 'A'.'B' LIMIT 7: " + said);
    EXPECT_TRUE(engine.connected()) << "a reply read whole leaves the connection in step";
  }
}

// --- the rest of the adapter ----------------------------------------------------------------------------

TEST(OrderbookAdapter, TheShapeCheckIsASchemaValidationWithNoFindings) {
  Rig rig;
  const sde::Model declared = model();
  const sde::PhysicalLayout layout = sde::default_layout(declared, declared.groups().front(), "orderbook");
  const sde::Keys keys = {{"DepthLevel", {"symbol", "exchange", "timestamp_ns", "side", "level"}}};
  EXPECT_TRUE(rig.engine->validate_schema(layout, keys).empty());
  EXPECT_TRUE(rig.engine->ensure_schema(layout, keys).empty());
  EXPECT_EQ(rig.logged("sde.schema.applied"),
            std::vector<std::string>{R"({"engine":"orderbook","statements":0})"});
  EXPECT_EQ(failure_of([&] { (void)rig.engine->validate_schema(layout, {{"DepthLevel", {"symbol"}}}); }),
            "EngineError: the map keys DepthLevel by ['symbol'] and this engine addresses rows by "
            "['symbol', 'exchange', 'timestamp_ns', 'side', 'level']. The order is positional and it "
            "is load-bearing: the symbol and the exchange are how a query reaches the data at all.");
  sde::PhysicalLayout renamed = layout;
  renamed.tables["DepthLevel"] = "depth";
  EXPECT_TRUE(contains(failure_of([&] { (void)rig.engine->validate_schema(renamed, keys); }),
                       "the map calls the table 'depth' and this engine's storage is 'orderbook'"));
  sde::PhysicalLayout reshaped = layout;
  reshaped.columns["DepthLevel"].erase("price");
  reshaped.columns["DepthLevel"]["venue"] = "string";
  EXPECT_EQ(failure_of([&] { (void)rig.engine->validate_schema(reshaped, keys); }),
            "EngineError: the map's layout for DepthLevel does not match this engine's fixed shape: "
            "missing ['price']; unexpected ['venue']. The shape is fixed in the engine and the whole "
            "of it is ['exchange', 'level', 'order_count', 'price', 'quantity', 'sequence_number', "
            "'side', 'symbol', 'timestamp_ns'].");
  sde::PhysicalLayout two = layout;
  two.tables["Trade"] = "orderbook";
  EXPECT_TRUE(contains(failure_of([&] { (void)rig.engine->validate_schema(two, keys); }),
                       "this engine stores one thing and the map gives it ['DepthLevel', 'Trade']"));
}

TEST(OrderbookAdapter, ItHasNoTransactionsAndRunsNoBody) {
  Rig rig;
  bool ran = false;
  EXPECT_TRUE(contains(failure_of([&] { rig.engine->transaction([&] { ran = true; }); }),
                       "this engine has no multi-statement transactions"));
  EXPECT_FALSE(ran);
  const sde::Capabilities offered = rig.engine->capabilities();
  EXPECT_EQ(offered.query, rig.engine.get());
  EXPECT_EQ(offered.bulk, rig.engine.get());
  EXPECT_EQ(offered.schema, rig.engine.get());
  EXPECT_EQ(offered.count, nullptr);
  EXPECT_EQ(offered.summary, nullptr);
  EXPECT_EQ(offered.watermark, nullptr) << "a fixed schema has nowhere to keep the bookkeeping";
  EXPECT_EQ(offered.migration, nullptr);
  EXPECT_EQ(offered.fences, nullptr);
  EXPECT_EQ(offered.storage, nullptr);
}

TEST(OrderbookAdapter, UpdatesAndBooksAreWrittenAndReadDirectly) {
  Rig rig;
  rig.engine->insert_levels(kTable, "ETHUSDT", "kraken", "ask", 42, {{10, 1, 1}, {11, 2, 1}, {12, 3, 2}});
  EXPECT_EQ(rig.fake.writes(), std::vector<std::string>{"MINSERT ETHUSDT kraken ask 3 42\n10 1 1\n11 2 1\n12 3 2"});
  EXPECT_TRUE(contains(failure_of([&] { rig.engine->insert_levels(kTable, "ETHUSDT", "kraken", "ask", 42, {}); }),
                       "an update with no levels is not an empty update"));
  EXPECT_EQ(failure_of([&] {
              rig.engine->insert_levels(kTable, "ETHUSDT", "kraken", "ask", 42,
                                        std::vector<sde::OrderbookLevel>(1001, {1, 1, 1}));
            }),
            "EngineError: has 1001 levels and this engine stores at most 1000 per side");
  const std::vector<sde::Row> levels = rig.engine->levels("ETHUSDT", "kraken", 40, 50);
  ASSERT_EQ(levels.size(), 3U);
  EXPECT_EQ(std::get<std::int64_t>(levels[2].at("level")), 2);
  EXPECT_TRUE(contains(rig.fake.queries().back(), "WHERE timestamp BETWEEN 40 AND 50 LIMIT 100001"));
  EXPECT_EQ(rig.engine->levels("ETHUSDT", "kraken", std::nullopt, std::nullopt, 2).size(), 2U);
  EXPECT_TRUE(rig.fake.queries().back().ends_with("FROM 'ETHUSDT'.'kraken' LIMIT 2"));
}

TEST(OrderbookAdapter, AKeyIsReadAsTheReferenceReadsItWithStrAndInt) {
  Rig rig;
  rig.fake.store("BTCUSDT", "binance", 0, 1000, Levels{{5, 1, 1}, {6, 1, 1}});
  rig.fake.store("12", "binance", 1, 1000, Levels{{7, 1, 1}});
  // `int()` of the time and the level, `str()` of the book: a document's number, text, a float.
  const sde::Row typed = {{"symbol", big("\"BTCUSDT\"")}, {"exchange", text("binance")},
                          {"timestamp_ns", text(" 1_000 ")}, {"side", big("\"bid\"")},
                          {"level", sde::Value(1.9)}};
  const std::optional<sde::Row> found = rig.engine->get(kTable, typed);
  ASSERT_TRUE(found.has_value());
  EXPECT_EQ(std::get<std::int64_t>(found->at("price")), 6);
  const sde::Row numbered = {{"symbol", integer(12)}, {"exchange", text("binance")},
                             {"timestamp_ns", integer(1000)}, {"side", text("ask")}, {"level", sde::Value(false)}};
  ASSERT_TRUE(rig.engine->get(kTable, numbered).has_value());
  sde::Row unreadable = numbered;
  unreadable["timestamp_ns"] = text("soon");
  EXPECT_EQ(failure_of([&] { (void)rig.engine->get(kTable, unreadable); }),
            "EngineError: invalid literal for int() with base 10: 'soon'");
  // `int()` of the level is read only for a row of the key's side, as in the reference.
  sde::Row lazy = {{"symbol", text("BTCUSDT")}, {"exchange", text("binance")},
                   {"timestamp_ns", integer(1000)}, {"side", text("ask")}, {"level", text("x")}};
  EXPECT_FALSE(rig.engine->get(kTable, lazy).has_value());
  lazy["side"] = text("bid");
  EXPECT_EQ(failure_of([&] { (void)rig.engine->get(kTable, lazy); }),
            "EngineError: invalid literal for int() with base 10: 'x'");
  sde::Row partial = numbered;
  partial.erase("level");
  partial.erase("side");
  EXPECT_EQ(failure_of([&] { (void)rig.engine->get(kTable, partial); }),
            "EngineError: get from orderbook is missing ['level', 'side'] from the key. This engine "
            "addresses rows by ['symbol', 'exchange', 'timestamp_ns', 'side', 'level'] and cannot "
            "scan for a partial one: the symbol and the exchange are how a query reaches the data at "
            "all.");
}

TEST(OrderbookAdapter, AfterForkTheEngineRefusesEverything) {
  Rig rig;
  EXPECT_EXIT(
      {
        try {
          rig.engine->flush();
        } catch (const sde::ResourceClosed&) {
          std::exit(0);
        }
        std::exit(1);
      },
      ::testing::ExitedWithCode(0), "");
}

// --- a session over the orderbook ---------------------------------------------------------------------

TEST(OrderbookAdapter, ASessionWritesUpdatesPagesInKeyOrderAndRefusesCountAndSummaryByName) {
  Rig rig;
  const sde::Model declared = model();
  const sde::PlacementMap placement = sde::live::default_map(declared, "orderbook", "ob");
  sde::Recorder recorder(declared);
  sde::SessionOptions options;
  options.recorder = &recorder;
  sde::Session session(declared, placement, {{"ob", rig.engine.get()}}, options);
  session.ensure_schema();
  session.save_many("DepthLevel", {row({{"timestamp_ns", integer(2)}}), row({{"timestamp_ns", integer(1)}}),
                                   row({{"timestamp_ns", integer(1)}, {"level", integer(1)}, {"price", integer(1)}})});
  sde::ScanOptions scan;
  scan.where = book();
  scan.limit = 2;
  const sde::ScanPage page = session.scan("DepthLevel", scan);
  EXPECT_EQ(positions(page.rows), (std::vector<Position>{{1, "bid", 0}, {1, "bid", 1}}));
  ASSERT_TRUE(page.next_after.has_value());
  sde::CountOptions counting;
  counting.where = book();
  EXPECT_EQ(failure_of([&] { (void)session.count("DepthLevel", counting); }),
            "QueryRefused: " + std::string(sde::OrderbookEngine::COUNT_REFUSAL));
  EXPECT_TRUE(contains(failure_of([&] { (void)session.summarize("DepthLevel", "price", {}); }),
                       "summarizes nothing over its history"));
  // Refused before they were timed: the window holds the write and the scan, and no failure.
  const std::optional<sde::Window> window = recorder.roll();
  ASSERT_TRUE(window.has_value());
  std::vector<std::pair<std::string, std::uint64_t>> shapes;
  for (const sde::ShapeStats& shape : window->shapes) shapes.emplace_back(shape.kind, shape.errors);
  std::sort(shapes.begin(), shapes.end());
  EXPECT_EQ(shapes, (std::vector<std::pair<std::string, std::uint64_t>>{{"bulk_write", 0}, {"full_scan", 0}}));
}

}  // namespace
