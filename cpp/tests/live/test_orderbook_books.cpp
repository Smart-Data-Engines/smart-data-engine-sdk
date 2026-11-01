/// A book stream through the orderbook adapter, against the fake engine in this process
/// (`live/orderbook_fake.hpp`): the bytes of a side replacement and of changes by price, the
/// refusals that come before anything is sent, and a stream's writes as a session records them. The
/// reference's `test_orderbook_books.py` and TypeScript's `orderbook.books` tests decide the same;
/// what the engine composes from them is held by the live slice (`test_orderbook_books_slice.cpp`).
/// Needs no DSN.

#include <memory>
#include <string>
#include <vector>

#include <gtest/gtest.h>

#include "live/maps.hpp"
#include "live/orderbook_fake.hpp"
#include "sde/books.hpp"
#include "sde/errors.hpp"
#include "sde/model.hpp"
#include "sde/orderbook.hpp"
#include "sde/session.hpp"
#include "sde/telemetry.hpp"

namespace {

using sde::BookLevel;
using sde::BookSide;
using sde::live::FakeOrderbook;

const std::string kTable = "orderbook";

sde::Model model() {
  std::string fields;
  for (const auto& [name, type] : sde::ORDERBOOK_SHAPE) {
    if (!fields.empty()) fields += ", ";
    fields += R"({"name": ")" + std::string(name) + R"(", "type": ")" + std::string(type) +
              R"(", "nullable": )" + (name == "sequence_number" ? "true" : "false") + "}";
  }
  return sde::load_neutral_model(
      R"({"name": "market", "entities": [{"name": "DepthLevel", "fields": [)" + fields +
      R"(], "key": ["symbol", "exchange", "timestamp_ns", "side", "level"]}],
          "relations": [], "atomic": []})");
}

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

/// The fake with the capabilities given, an adapter connected to it, and a session over it.
struct Books {
  explicit Books(const std::string& capabilities = "insert_event_time,strict_args,replace_side")
      : recorder(declared) {
    fake.capabilities(capabilities);
    engine = std::make_unique<sde::OrderbookEngine>(fake.dsn());
    engine->connect();
    placement = std::make_unique<sde::PlacementMap>(sde::live::default_map(declared, "orderbook", "ob"));
    sde::SessionOptions options;
    options.recorder = &recorder;
    session = std::make_unique<sde::Session>(
        declared, *placement, std::map<std::string, sde::Engine*>{{"ob", engine.get()}}, options);
  }

  FakeOrderbook fake;
  sde::Model declared = model();
  sde::Recorder recorder;
  std::unique_ptr<sde::OrderbookEngine> engine;
  std::unique_ptr<sde::PlacementMap> placement;
  std::unique_ptr<sde::Session> session;
};

TEST(OrderbookBooks, AReplacementIsOneBlockAndAnEmptyOneIsItsHeader) {
  Books rig;
  rig.engine->replace_levels(kTable, "BTCUSDT", "binance", BookSide::bid, 7,
                             {{101, 2, 1}, {100, 5, 3}});
  rig.engine->replace_levels(kTable, "BTCUSDT", "binance", BookSide::ask, 8, {});
  rig.engine->replace_levels(kTable, "BTCUSDT", "binance", BookSide::ask, 9, {{102, 1, 1}});
  EXPECT_EQ(rig.fake.writes(), (std::vector<std::string>{
                                   "MREPLACE BTCUSDT binance bid 2 7\n101 2 1\n100 5 3",
                                   "MREPLACE BTCUSDT binance ask 0 8",
                                   "MREPLACE BTCUSDT binance ask 1 9\n102 1 1"}));
}

TEST(OrderbookBooks, ChangesAreAnOrdinaryUpdateInTheirOrderAndNoneIsNoUpdate) {
  Books rig;
  rig.engine->apply_levels(kTable, "BTCUSDT", "binance", BookSide::ask, 10,
                           {{103, 0, 0}, {101, 4, 2}});
  rig.engine->apply_levels(kTable, "BTCUSDT", "binance", BookSide::ask, 11, {{104, 0, 0}});
  rig.engine->apply_levels(kTable, "BTCUSDT", "binance", BookSide::ask, 12, {});
  EXPECT_EQ(rig.fake.writes(),
            (std::vector<std::string>{"MINSERT BTCUSDT binance ask 2 10\n103 0 0\n101 4 2",
                                      "INSERT BTCUSDT binance ask 104 0 0 11"}));
}

TEST(OrderbookBooks, WhatTheEngineCannotStoreIsRefusedBeforeSending) {
  Books rig;
  const std::vector<BookLevel> deep(1001, BookLevel{1, 1, 1});
  EXPECT_EQ(failure_of([&] {
              rig.engine->replace_levels(kTable, "BTCUSDT", "binance", BookSide::bid, 1, deep);
            }),
            "EngineError: has 1001 levels and this engine stores at most 1000 per side");
  EXPECT_NE(failure_of([&] {
              rig.engine->apply_levels(kTable, "BTC USDT", "binance", BookSide::bid, 1, {{1, 1, 1}});
            }),
            "accepted");
  EXPECT_NE(failure_of([&] {
              rig.engine->apply_levels(kTable, "BTCUSDT", "binance", BookSide::bid, -1, {{1, 1, 1}});
            }),
            "accepted");
  EXPECT_NE(failure_of([&] {
              rig.engine->apply_levels(kTable, "BTCUSDT", "binance", BookSide::bid, 1, {{1, -1, 1}});
            }),
            "accepted");
  EXPECT_TRUE(rig.fake.writes().empty());
}

TEST(OrderbookBooks, TheCapabilityIsTheServers) {
  Books with;
  EXPECT_TRUE(with.engine->supports_replace_side());
  Books without("insert_event_time,strict_args");
  EXPECT_FALSE(without.engine->supports_replace_side());
}

TEST(OrderbookBooks, ABookAsOfAMomentIsAskedByTimeAndAServerByPositionIsRefused) {
  Books rig;
  const sde::Book none = rig.engine->book_at(kTable, "BTCUSDT", "binance", 5000, 20);
  EXPECT_TRUE(none.bid.empty() && none.ask.empty()) << "a book nothing was written to is empty";
  EXPECT_EQ(rig.fake.queries(),
            (std::vector<std::string>{"SELECT * FROM 'BTCUSDT'.'binance' WHERE AT 5000 AND level < 20"}));
  Books old("insert_event_time,strict_args");
  EXPECT_EQ(failure_of([&] { (void)old.engine->book_at(kTable, "BTCUSDT", "binance", 5000, {}); }),
            "EngineError: this orderbook server composes a book by position, not by price (it "
            "predates the capability replace_side, which came with reads by price), and would read "
            "a book written by price wrongly. Upgrade the server; nothing was read.");
  EXPECT_TRUE(old.fake.queries().empty());
}

TEST(OrderbookBooks, AStreamWritesThroughTheSessionAndEachUpdateIsOneWrite) {
  Books rig;
  sde::BookStream stream = rig.session->book_stream("DepthLevel", "BTCUSDT", "binance");
  stream.update(BookSide::bid, {{100, 5, 1}, {99, 3, 1}}, 10);
  stream.update(BookSide::bid, {{101, 2, 1}, {99, 3, 1}}, 20);
  stream.update(BookSide::bid, {{101, 2, 1}, {99, 3, 1}}, 30);  // unchanged: nothing sent
  EXPECT_EQ(rig.fake.writes(),
            (std::vector<std::string>{"MREPLACE BTCUSDT binance bid 2 10\n100 5 1\n99 3 1",
                                      "MINSERT BTCUSDT binance bid 2 20\n100 0 0\n101 2 1"}));
  const sde::Json window = rig.recorder.roll()->as_record(rig.declared);
  const std::string text = sde::dump_json(window);
  EXPECT_NE(text.find("bulk_write"), std::string::npos) << text;
}

TEST(OrderbookBooks, AServerWithoutReplaceSideIsRefusedBeforeAStreamSendsAnything) {
  Books rig("insert_event_time,strict_args");
  EXPECT_EQ(failure_of([&] { (void)rig.session->book_stream("DepthLevel", "BTCUSDT", "binance"); }),
            "EngineError: this orderbook server does not replace a side of a book (capability "
            "replace_side), which a book stream needs: in full mode for every update, in "
            "incremental mode for each resync. Upgrade the server; nothing was sent.");
  EXPECT_TRUE(rig.fake.writes().empty());
}

TEST(OrderbookBooks, AStreamForABookWhoseNameAWriteWouldRefuseIsRefusedAtOnce) {
  Books rig;
  EXPECT_NE(failure_of([&] { (void)rig.session->book_stream("DepthLevel", "BTC USDT", "binance"); }),
            "accepted");
}

TEST(OrderbookBooks, ABookAsOfAMomentIsRecordedAsARangeReadOfTheEntitysTime) {
  // A book is its history up to an instant: a range read over `timestamp_ns`, filtered on the
  // symbol and the exchange, as the reference records it.
  Books rig;
  (void)rig.session->book_at("DepthLevel", "BTCUSDT", "binance", 42);
  const auto window = rig.recorder.roll();
  ASSERT_TRUE(window.has_value());
  const std::string records = sde::dump_json(window->shape_records(rig.declared, "DepthLevel"));
  EXPECT_NE(records.find(R"("filtered_on":[{"equal":["exchange","symbol"],"range":"timestamp_ns","calls":1}])"),
            std::string::npos)
      << records;
}

TEST(OrderbookBooks, TheSessionReadsTheBookAsOfAMoment) {
  Books rig;
  const sde::Book none = rig.session->book_at("DepthLevel", "BTCUSDT", "binance", 42);
  EXPECT_TRUE(none.bid.empty() && none.ask.empty());
  EXPECT_EQ(rig.fake.queries(),
            (std::vector<std::string>{"SELECT * FROM 'BTCUSDT'.'binance' WHERE AT 42"}));
}

}  // namespace
