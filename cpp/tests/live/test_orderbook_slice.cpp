/// The orderbook engine, live: the measurements the fake in the adapter tests encodes, a Session
/// over the engine, the server with client authentication and TLS, and one book written by this
/// library and read by the reference, and the other way round. The reference's
/// `test_orderbook_slice.py` and `test_orderbook_session_slice.py` over TCP, and TypeScript's
/// `orderbook.slice.test.ts`, ported. If the engine changes, this fails and the fake stops being a
/// description of something true - the failure a fake normally hides.
///
/// `SDE_ORDERBOOK_TCP=host:port` names a plain `ob_tcp_server`, `SDE_ORDERBOOK_SECURE_DSN` one with
/// `--auth-secret-file` and `--tls-client`, as `orderbook://identity:secret@host:port?tls=on&ca=PATH`,
/// and `SDE_PYTHON` an interpreter with the SDK and the engine's Python client for the reference's
/// half. Labelled `orderbook`: the SDK's `orderbook` CI job runs it beside the engine it builds.

#include <algorithm>
#include <array>
#include <cctype>
#include <map>
#include <cstdint>
#include <cstdio>
#include <memory>
#include <optional>
#include <random>
#include <set>
#include <string>
#include <tuple>
#include <utility>
#include <vector>

#include <gtest/gtest.h>

#include "engines/orderbook/dsn.hpp"
#include "engines/orderbook/wire.hpp"
#include "live/live.hpp"
#include "live/maps.hpp"
#include "sde/errors.hpp"
#include "sde/json.hpp"
#include "sde/layout.hpp"
#include "sde/model.hpp"
#include "sde/orderbook.hpp"
#include "sde/schema.hpp"
#include "sde/session.hpp"
#include "sde/telemetry.hpp"

namespace {

const std::string kTable = "orderbook";
const std::string kExchange = "binance";
constexpr std::int64_t kT0 = 1790000000000000000;

using Position = std::tuple<std::int64_t, std::string, std::int64_t>;

sde::Value text(const std::string& value) { return sde::Value(value); }
sde::Value integer(std::int64_t value) { return sde::Value(value); }

/// A symbol no other test has written to: over TCP every test shares one server.
std::string fresh_book() {
  std::string out = "T";
  for (const char c : sde::live::fresh(12)) out += static_cast<char>(std::toupper(static_cast<unsigned char>(c)));
  return out;
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

bool contains(const std::string& text_, const std::string& part) {
  return text_.find(part) != std::string::npos;
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

sde::Row values(const std::string& symbol, std::vector<std::pair<std::string, sde::Value>> overrides = {}) {
  sde::Row out = {{"symbol", text(symbol)},     {"exchange", text(kExchange)},
                  {"timestamp_ns", integer(kT0)}, {"side", text("bid")},
                  {"level", integer(0)},          {"price", integer(5000000)},
                  {"quantity", integer(3)},       {"order_count", integer(1)},
                  {"sequence_number", sde::Value()}};
  for (auto& [name, value] : overrides) out[name] = std::move(value);
  return out;
}

sde::Row key_of(const sde::Row& row) {
  sde::Row key;
  for (const std::string_view name : sde::ORDERBOOK_KEY) key.emplace(name, row.at(std::string(name)));
  return key;
}

sde::Row without_sequence(sde::Row row) {
  row.erase("sequence_number");
  return row;
}

/// Both sides of one book at one instant, as a feed's snapshot: two updates of `depth` levels.
std::vector<sde::Row> snapshot(const std::string& symbol, std::int64_t stamp, std::int64_t depth = 3) {
  std::vector<sde::Row> out;
  for (const auto& [side, top, step] : {std::tuple{"bid", 6500000, -100}, std::tuple{"ask", 6500100, 100}}) {
    for (std::int64_t level = 0; level < depth; ++level) {
      out.push_back({{"symbol", text(symbol)},       {"exchange", text(kExchange)},
                     {"timestamp_ns", integer(stamp)}, {"side", text(side)},
                     {"level", integer(level)},        {"price", integer(top + level * step)},
                     {"quantity", integer(1000 + level)}, {"order_count", integer(1 + level)}});
    }
  }
  return out;
}

Position position_of(const sde::Row& row) {
  return {std::get<std::int64_t>(row.at("timestamp_ns")), std::get<std::string>(row.at("side")),
          std::get<std::int64_t>(row.at("level"))};
}

std::vector<Position> positions(const std::vector<sde::Row>& rows) {
  std::vector<Position> out;
  for (const sde::Row& row : rows) out.push_back(position_of(row));
  return out;
}

std::vector<sde::Row> in_key_order(std::vector<sde::Row> rows) {
  std::sort(rows.begin(), rows.end(),
            [](const sde::Row& a, const sde::Row& b) { return position_of(a) < position_of(b); });
  return rows;
}

/// An adapter on the plain server, connected; and a Session over it, as the reference opens one.
class OrderbookSlice : public ::testing::Test {
 protected:
  void SetUp() override {
    SDE_REQUIRE_DSN("SDE_ORDERBOOK_TCP", address_);
    engine_ = std::make_unique<sde::OrderbookEngine>("orderbook://" + address_);
    engine_->connect();
  }
  void TearDown() override {
    if (engine_) engine_->close();
  }

  sde::Session& session(sde::Recorder* recorder = nullptr) {
    model_ = std::make_unique<sde::Model>(model());
    placement_ = std::make_unique<sde::PlacementMap>(sde::live::default_map(*model_, "orderbook", "ob"));
    sde::SessionOptions options;
    options.recorder = recorder;
    session_ = std::make_unique<sde::Session>(*model_, *placement_,
                                              std::map<std::string, sde::Engine*>{{"ob", engine_.get()}},
                                              options);
    session_->ensure_schema();
    return *session_;
  }
  std::vector<sde::Row> book(const std::string& symbol, std::optional<std::uint64_t> start = std::nullopt,
                             std::optional<std::uint64_t> end = std::nullopt,
                             std::optional<std::int64_t> limit = std::nullopt) {
    return engine_->levels(symbol, kExchange, start, end, limit);
  }

  std::string address_;
  std::unique_ptr<sde::OrderbookEngine> engine_;
  std::unique_ptr<sde::Model> model_;
  std::unique_ptr<sde::PlacementMap> placement_;
  std::unique_ptr<sde::Session> session_;
};

// --- the measurements -----------------------------------------------------------------------------------

TEST_F(OrderbookSlice, AReadAfterAWriteFlushesFirstAndFindsIt) {
  // Measurement 1 over TCP: the server flushes on its own tick too, so a raw read is a race; the
  // adapter flushes before every read that follows a write.
  const std::string symbol = fresh_book();
  engine_->insert_levels(kTable, symbol, kExchange, "bid", kT0, {{5000000, 3, 1}});
  EXPECT_EQ(book(symbol).size(), 1U);
}

TEST_F(OrderbookSlice, ASingleLevelWriteLandsAtLevelZeroAndAnyOtherLevelIsRefused) {
  // Measurement 2: the write API has no level; a price's level is its position in an update.
  const std::string symbol = fresh_book();
  engine_->insert(kTable, values(symbol));
  EXPECT_EQ(positions(book(symbol)), (std::vector<Position>{{kT0, "bid", 0}}));
  EXPECT_TRUE(contains(failure_of([&] {
                         engine_->insert(kTable, values(symbol, {{"level", integer(3)}, {"timestamp_ns", integer(kT0 + 1)}}));
                       }),
                       "has no level parameter"));
}

TEST_F(OrderbookSlice, AMultiLevelUpdateNumbersLevelsByPosition) {
  const std::string symbol = fresh_book();
  engine_->insert_levels(kTable, symbol, kExchange, "bid", kT0,
                         {{5000000, 3, 1}, {4999900, 7, 2}, {4999800, 11, 4}});
  std::vector<std::pair<std::int64_t, std::int64_t>> levels;
  for (const sde::Row& row : book(symbol)) {
    levels.emplace_back(std::get<std::int64_t>(row.at("level")), std::get<std::int64_t>(row.at("price")));
  }
  EXPECT_EQ(levels, (std::vector<std::pair<std::int64_t, std::int64_t>>{
                        {0, 5000000}, {1, 4999900}, {2, 4999800}}));
}

TEST_F(OrderbookSlice, TwoWritesWithOneKeyBothPersistAndGetRefusesToPickOne) {
  // Measurement 3: an append-only log of depth updates does not enforce a key.
  const std::string symbol = fresh_book();
  for (const std::int64_t price : {5000000, 4999900}) {
    engine_->insert_levels(kTable, symbol, kExchange, "bid", kT0, {{price, 1, 1}});
  }
  const std::vector<sde::Row> rows = book(symbol);
  ASSERT_EQ(rows.size(), 2U);
  EXPECT_EQ(positions(rows), (std::vector<Position>{{kT0, "bid", 0}, {kT0, "bid", 0}}));
  EXPECT_TRUE(contains(failure_of([&] { (void)engine_->get(kTable, key_of(values(symbol))); }),
                       "2 rows in orderbook share the key"));
}

TEST_F(OrderbookSlice, TheServerNumbersEveryUpdateAndAChosenNumberNeverLeaves) {
  // Measurement 4 over TCP: one number per update, and the server's.
  const std::string symbol = fresh_book();
  engine_->insert(kTable, values(symbol));
  engine_->insert(kTable, values(symbol, {{"side", text("ask")}, {"price", integer(5000100)}}));
  std::vector<std::int64_t> numbers;
  for (const sde::Row& row : book(symbol)) numbers.push_back(std::get<std::int64_t>(row.at("sequence_number")));
  ASSERT_EQ(numbers.size(), 2U);
  EXPECT_GE(numbers[0], 1);
  EXPECT_NE(numbers[0], numbers[1]) << "one number per update";
  EXPECT_TRUE(contains(failure_of([&] {
                         engine_->insert(kTable, values(symbol, {{"timestamp_ns", integer(kT0 + 5)},
                                                                 {"sequence_number", integer(41)}}));
                       }),
                       "sequence number is the server's"));
  EXPECT_EQ(book(symbol).size(), 2U) << "nothing was sent";
}

TEST_F(OrderbookSlice, TheEngineAnswersInArrivalOrderAndItsLimitKeepsTheFirstToArrive) {
  // Measurement 5: the reason a page is assembled from windows of time.
  const std::string symbol = fresh_book();
  for (const std::int64_t offset : {3000, 2000, 1000}) {
    engine_->insert(kTable, values(symbol, {{"timestamp_ns", integer(kT0 + offset)}}));
  }
  std::vector<std::int64_t> stored;
  for (const sde::Row& row : book(symbol)) stored.push_back(std::get<std::int64_t>(row.at("timestamp_ns")) - kT0);
  EXPECT_EQ(stored, (std::vector<std::int64_t>{3000, 2000, 1000}));
  const std::vector<sde::Row> first = book(symbol, std::nullopt, std::nullopt, 1);
  ASSERT_EQ(first.size(), 1U);
  EXPECT_EQ(std::get<std::int64_t>(first[0].at("timestamp_ns")) - kT0, 3000) << "LIMIT keeps the first to arrive";
}

TEST_F(OrderbookSlice, ABookNobodyHasWrittenToIsNamedByTheServerAndReadAsEmpty) {
  // Measurement 6: the server answers OB_ERR_NOT_FOUND, and the adapter reads an empty book.
  const std::string unknown = fresh_book();
  EXPECT_TRUE(book(unknown).empty());
  EXPECT_FALSE(engine_->get(kTable, key_of(values(unknown))).has_value());
}

TEST_F(OrderbookSlice, AStoredValueTheModelsTypeCannotHoldIsRefusedOnReadWithoutIt) {
  // The engine stores unsigned 64-bit quantities; the model's int64 holds half of them. Written
  // straight over the protocol, as no write of this library would.
  const std::string symbol = fresh_book();
  sde::detail::orderbook::Connection raw(sde::detail::orderbook::parse_dsn("orderbook://" + address_));
  EXPECT_EQ(raw.execute("INSERT " + symbol + " " + kExchange + " bid 100 9223372036854775808 1 " +
                        std::to_string(kT0)),
            "OK\n\n");
  EXPECT_EQ(raw.execute("FLUSH"), "OK\n\n");
  raw.close();
  const std::string refused = failure_of([&] { (void)book(symbol); });
  EXPECT_TRUE(contains(refused, "the engine returned a row whose quantity is outside 0 to "
                                "9223372036854775807, the range of the model's type."))
      << refused;
  EXPECT_FALSE(contains(refused, "9223372036854775808")) << "a value read back is data and stays out";
}

// --- the adapter against the engine ----------------------------------------------------------------------

TEST_F(OrderbookSlice, TheFixedShapeIsAcceptedAndNothingIsCreated) {
  const sde::Model declared = model();
  const sde::PhysicalLayout layout = sde::default_layout(declared, declared.groups().front(), "orderbook");
  const sde::Keys keys = {{"DepthLevel", {"symbol", "exchange", "timestamp_ns", "side", "level"}}};
  EXPECT_TRUE(engine_->ensure_schema(layout, keys).empty());
  EXPECT_TRUE(sde::schema_statements(layout, keys, "orderbook").empty());
}

TEST_F(OrderbookSlice, ARowWrittenThroughTheMapComesBackWithEveryField) {
  const std::string symbol = fresh_book();
  const sde::Row written = values(symbol);
  engine_->insert(kTable, written);
  std::optional<sde::Row> row = engine_->get(kTable, key_of(written));
  ASSERT_TRUE(row.has_value());
  EXPECT_GE(std::get<std::int64_t>(row->at("sequence_number")), 1) << "the server's number, not unknown";
  EXPECT_EQ(without_sequence(*row), without_sequence(written));
}

TEST_F(OrderbookSlice, ARangeReadIsAddressedByBookAndBoundedByTimeAtBothEnds) {
  const std::string symbol = fresh_book();
  for (std::int64_t offset = 0; offset < 5; ++offset) {
    engine_->insert(kTable, values(symbol, {{"timestamp_ns", integer(kT0 + offset)}, {"price", integer(100 + offset)}}));
  }
  std::vector<std::int64_t> within;
  for (const sde::Row& row : book(symbol, kT0 + 1, kT0 + 3)) within.push_back(std::get<std::int64_t>(row.at("timestamp_ns")));
  EXPECT_EQ(within, (std::vector<std::int64_t>{kT0 + 1, kT0 + 2, kT0 + 3}))
      << "both ends inclusive, because the engine's BETWEEN is";
  EXPECT_EQ(book(symbol, std::nullopt, std::nullopt, 2).size(), 2U);
  const std::string other = fresh_book();
  engine_->insert(kTable, values(other, {{"price", integer(300000)}}));
  EXPECT_EQ(book(other).size(), 1U) << "another book is another address, not a filter";
}

TEST_F(OrderbookSlice, TransactionsAreRefusedAgainstTheRealEngineToo) {
  bool ran = false;
  EXPECT_TRUE(contains(failure_of([&] { engine_->transaction([&] { ran = true; }); }),
                       "no multi-statement transactions"));
  EXPECT_FALSE(ran);
}

// --- a session over the engine ------------------------------------------------------------------------

TEST_F(OrderbookSlice, SaveManyWritesWholeUpdatesAndEveryRowReadsBack) {
  sde::Session& current = session();
  const std::string symbol = fresh_book();
  const std::vector<sde::Row> written = snapshot(symbol, kT0, 5);
  current.save_many("DepthLevel", written);
  sde::ScanOptions scan;
  scan.where = sde::Row{{"symbol", text(symbol)}, {"exchange", text(kExchange)}};
  scan.limit = 100;
  const sde::ScanPage page = current.scan("DepthLevel", scan);
  std::vector<sde::Row> read;
  std::set<std::int64_t> numbers;
  for (const sde::Row& row : page.rows) {
    read.push_back(without_sequence(row));
    numbers.insert(std::get<std::int64_t>(row.at("sequence_number")));
  }
  EXPECT_EQ(read, in_key_order(written));
  EXPECT_EQ(numbers.size(), 2U) << "the server numbers each of the two updates";
  const std::optional<sde::Row> one = current.get("DepthLevel", key_of(written[0]));
  ASSERT_TRUE(one.has_value());
  EXPECT_EQ(without_sequence(*one), written[0]);
}

TEST_F(OrderbookSlice, EveryQuantityTheAdapterAdmitsReadsBackAsWritten) {
  // An engine before c1f14c0 (its #198) wrote exactly 2^60 - 1 as Simple8b's fallback marker: that
  // value read back as garbage and every later quantity in the segment as 0. The pin is past it.
  sde::Session& current = session();
  const std::string symbol = fresh_book();
  const std::array<std::int64_t, 6> edges = {0, 1, (std::int64_t{1} << 60) - 2, (std::int64_t{1} << 60) - 1,
                                             std::int64_t{1} << 60, INT64_MAX};
  std::vector<sde::Row> first;
  for (std::size_t level = 0; level < edges.size(); ++level) {
    first.push_back({{"symbol", text(symbol)}, {"exchange", text(kExchange)}, {"timestamp_ns", integer(kT0)},
                     {"side", text("bid")}, {"level", integer(static_cast<std::int64_t>(level))},
                     {"price", integer(6500000 - static_cast<std::int64_t>(level) * 100)},
                     {"quantity", integer(edges[level])}, {"order_count", integer(1)}});
  }
  const std::vector<sde::Row> later = snapshot(symbol, kT0 + 1000);
  current.save_many("DepthLevel", first);
  current.save_many("DepthLevel", later);
  sde::ScanOptions scan;
  scan.where = sde::Row{{"symbol", text(symbol)}, {"exchange", text(kExchange)}};
  const sde::ScanPage page = current.scan("DepthLevel", scan);
  std::vector<sde::Row> read;
  for (const sde::Row& row : page.rows) read.push_back(without_sequence(row));
  std::vector<sde::Row> all = first;
  all.insert(all.end(), later.begin(), later.end());
  EXPECT_EQ(read, in_key_order(all));
}

TEST_F(OrderbookSlice, AScanPagesInKeyOrderAlthoughTheEngineAnswersInArrivalOrder) {
  sde::Session& current = session();
  const std::string symbol = fresh_book();
  for (const std::int64_t offset : {5, 1, 4, 2, 3}) current.save_many("DepthLevel", snapshot(symbol, kT0 + offset * 1000));
  std::vector<Position> expected;
  for (const std::int64_t offset : {1, 2, 3, 4, 5}) {
    for (const char* side : {"ask", "bid"}) {
      for (std::int64_t level = 0; level < 3; ++level) expected.emplace_back(kT0 + offset * 1000, side, level);
    }
  }
  sde::ScanOptions scan;
  scan.where = sde::Row{{"symbol", text(symbol)}, {"exchange", text(kExchange)}};
  scan.limit = 7;
  std::vector<Position> seen;
  for (;;) {
    const sde::ScanPage page = current.scan("DepthLevel", scan);
    for (const Position& position : positions(page.rows)) seen.push_back(position);
    if (!page.next_after) break;
    scan.after = *page.next_after;
  }
  EXPECT_EQ(seen, expected);
  sde::ScanOptions backwards;
  backwards.where = scan.where;
  backwards.descending = true;
  std::vector<Position> reversed(expected.rbegin(), expected.rend());
  EXPECT_EQ(positions(current.scan("DepthLevel", backwards).rows), reversed);
}

TEST_F(OrderbookSlice, AScanNarrowedBySideLevelTimeAndPrice) {
  sde::Session& current = session();
  const std::string symbol = fresh_book();
  for (const std::int64_t offset : {1, 2, 3}) current.save_many("DepthLevel", snapshot(symbol, kT0 + offset * 1000));
  sde::ScanOptions narrowed;
  narrowed.where = sde::Row{{"symbol", text(symbol)}, {"exchange", text(kExchange)},
                            {"side", text("ask")}, {"level", integer(1)}};
  narrowed.bounds = sde::Range{"timestamp_ns", integer(kT0 + 1500), integer(kT0 + 3000)};
  narrowed.limit = 10;
  EXPECT_EQ(positions(current.scan("DepthLevel", narrowed).rows),
            (std::vector<Position>{{kT0 + 2000, "ask", 1}}));
  sde::ScanOptions priced;
  priced.where = sde::Row{{"symbol", text(symbol)}, {"exchange", text(kExchange)}};
  priced.bounds = sde::Range{"price", integer(6499900), integer(6500101)};
  std::set<std::int64_t> prices;
  for (const sde::Row& row : current.scan("DepthLevel", priced).rows) prices.insert(std::get<std::int64_t>(row.at("price")));
  EXPECT_EQ(prices, (std::set<std::int64_t>{6499900, 6500000, 6500100}));
}

TEST_F(OrderbookSlice, ADuplicatedKeyIsRefusedByAScanAsByAGet) {
  sde::Session& current = session();
  const std::string symbol = fresh_book();
  const sde::Row one = snapshot(symbol, kT0, 1).front();
  current.save_many("DepthLevel", {one});
  sde::Row cheaper = one;
  cheaper["price"] = integer(std::get<std::int64_t>(one.at("price")) - 1);
  current.save_many("DepthLevel", {cheaper});
  sde::ScanOptions scan;
  scan.where = sde::Row{{"symbol", text(symbol)}, {"exchange", text(kExchange)}};
  EXPECT_TRUE(contains(failure_of([&] { (void)current.scan("DepthLevel", scan); }), "does not enforce a key"));
  EXPECT_TRUE(contains(failure_of([&] { (void)current.get("DepthLevel", key_of(one)); }), "share the key"));
}

TEST_F(OrderbookSlice, CountAndSummarizeAreRefusedByNameAndAreNotRecordedAsErrors) {
  const sde::Model declared = model();
  sde::Recorder recorder(declared);
  sde::Session& current = session(&recorder);
  const std::string symbol = fresh_book();
  current.save_many("DepthLevel", snapshot(symbol, kT0));
  sde::CountOptions counting;
  counting.where = sde::Row{{"symbol", text(symbol)}, {"exchange", text(kExchange)}};
  EXPECT_TRUE(contains(failure_of([&] { (void)current.count("DepthLevel", counting); }),
                       "QueryRefused: this engine counts nothing over its history"));
  EXPECT_TRUE(contains(failure_of([&] { (void)current.summarize("DepthLevel", "quantity", {}); }),
                       "summarizes nothing over its history"));
  const std::optional<sde::Window> window = recorder.roll();
  ASSERT_TRUE(window.has_value());
  std::set<std::string> kinds;
  for (const sde::ShapeStats& shape : window->shapes) {
    kinds.insert(shape.kind);
    EXPECT_EQ(shape.errors, 0U) << "a read the engine cannot answer is not an engine error";
  }
  EXPECT_EQ(kinds, (std::set<std::string>{"bulk_write"}));
}

TEST_F(OrderbookSlice, TheWindowSeesEveryCallAndTheSizeIsUnknownAndSaidSo) {
  const sde::Model declared = model();
  sde::Recorder recorder(declared);
  sde::Session& current = session(&recorder);
  const std::string symbol = fresh_book();
  current.save_many("DepthLevel", snapshot(symbol, kT0));
  sde::ScanOptions scan;
  scan.where = sde::Row{{"symbol", text(symbol)}, {"exchange", text(kExchange)}};
  scan.bounds = sde::Range{"timestamp_ns", integer(kT0), integer(kT0 + 1)};
  (void)current.scan("DepthLevel", scan);
  const sde::StorageMeasurement measured = current.measure_storage();
  EXPECT_TRUE(measured.sizes.empty());
  EXPECT_EQ(measured.unavailable, (std::map<std::string, std::string>{{"DepthLevel", "unsupported"}}));
  const std::optional<sde::Window> window = recorder.roll();
  ASSERT_TRUE(window.has_value());
  std::map<std::string, const sde::ShapeStats*> kinds;
  for (const sde::ShapeStats& shape : window->shapes) kinds[shape.kind] = &shape;
  ASSERT_EQ(kinds.size(), 2U);
  ASSERT_TRUE(kinds.contains("bulk_write") && kinds.contains("range_read"));
  EXPECT_EQ(kinds["bulk_write"]->rows, 6U);
  EXPECT_EQ(kinds["range_read"]->filtered,
            (std::map<sde::Predicates, std::uint64_t>{{{{"exchange", "symbol"}, "timestamp_ns"}, 1}}));
}

TEST_F(OrderbookSlice, AChosenSequenceNumberNeverLeavesTheSession) {
  sde::Session& current = session();
  const std::string symbol = fresh_book();
  sde::Row chosen = snapshot(symbol, kT0, 1).front();
  chosen["sequence_number"] = integer(41);
  EXPECT_TRUE(contains(failure_of([&] { current.save("DepthLevel", chosen); }), "sequence number is the server's"));
  EXPECT_TRUE(contains(failure_of([&] { current.save_many("DepthLevel", {chosen}); }), "sequence number is the server's"));
  sde::ScanOptions scan;
  scan.where = sde::Row{{"symbol", text(symbol)}, {"exchange", text(kExchange)}};
  EXPECT_TRUE(current.scan("DepthLevel", scan).rows.empty()) << "nothing was sent";
}

// --- one book, two libraries ---------------------------------------------------------------------------

/// The reference's half: `python/tests/orderbook_peer.py write|read`, run with `SDE_PYTHON`.
std::string peer(const std::string& python, const std::string& action, const std::string& address,
                 const std::string& symbol) {
  const std::string command = "'" + python + "' '" SDE_REPOSITORY "/python/tests/orderbook_peer.py' " +
                              action + " " + address + " " + symbol;
  std::FILE* pipe = ::popen(command.c_str(), "r");
  if (pipe == nullptr) return "";
  std::string out;
  char chunk[4096];
  while (const std::size_t count = std::fread(chunk, 1, sizeof chunk, pipe)) out.append(chunk, count);
  const int status = ::pclose(pipe);
  EXPECT_EQ(status, 0) << command << " -> " << out;
  return out;
}

TEST_F(OrderbookSlice, ThisLibraryReadsWhatTheReferenceWroteAndTheReferenceWhatThisOneWrote) {
  std::string python;
  SDE_REQUIRE_DSN("SDE_PYTHON", python);
  sde::Session& current = session();
  const std::string from_python = fresh_book();
  (void)peer(python, "write", address_, from_python);
  sde::ScanOptions scan;
  scan.where = sde::Row{{"symbol", text(from_python)}, {"exchange", text(kExchange)}};
  scan.limit = 1000;
  const std::vector<sde::Row> read = current.scan("DepthLevel", scan).rows;
  ASSERT_EQ(read.size(), 12U);
  const sde::Row first = {{"symbol", text(from_python)}, {"exchange", text(kExchange)},
                          {"timestamp_ns", integer(kT0 + 1000)}, {"side", text("ask")},
                          {"level", integer(0)}, {"price", integer(101 * 100 + 1000)},
                          {"quantity", integer(5)}, {"order_count", integer(1)},
                          {"sequence_number", integer(3)}};
  EXPECT_EQ(read[0], first);
  const std::string from_cpp = fresh_book();
  std::vector<sde::Row> written;
  for (const std::int64_t stamp : {3000, 1000, 2000}) {
    for (const auto& [side, base] : {std::pair{"ask", 10100}, std::pair{"bid", 9900}}) {
      for (std::int64_t level = 0; level < 2; ++level) {
        written.push_back({{"symbol", text(from_cpp)}, {"exchange", text(kExchange)},
                           {"timestamp_ns", integer(kT0 + stamp)}, {"side", text(side)},
                           {"level", integer(level)}, {"price", integer(base + stamp + level)},
                           {"quantity", integer(5 + level)}, {"order_count", integer(1 + level)},
                           {"sequence_number", sde::Value()}});
      }
    }
  }
  current.save_many("DepthLevel", written);
  engine_->flush();
  const sde::Json seen = sde::parse_json(peer(python, "read", address_, from_cpp));
  scan.where = sde::Row{{"symbol", text(from_cpp)}, {"exchange", text(kExchange)}};
  const std::vector<sde::Row> mine = current.scan("DepthLevel", scan).rows;
  ASSERT_EQ(seen.as_array().size(), 12U);
  ASSERT_EQ(mine.size(), 12U);
  for (std::size_t i = 0; i < mine.size(); ++i) {
    SCOPED_TRACE(i);
    const sde::Json& theirs = seen.as_array()[i];
    for (const auto& [name, value] : mine[i]) {
      const std::string expected = std::holds_alternative<std::int64_t>(value)
                                       ? std::to_string(std::get<std::int64_t>(value))
                                       : std::get<std::string>(value);
      const sde::Json* field = theirs.find(name);
      ASSERT_NE(field, nullptr) << name;
      EXPECT_EQ(field->as_string(), expected) << name;
    }
  }
}

// --- with credentials and TLS ------------------------------------------------------------------------

class OrderbookSecureSlice : public ::testing::Test {
 protected:
  void SetUp() override { SDE_REQUIRE_DSN("SDE_ORDERBOOK_SECURE_DSN", dsn_); }

  /// The DSN with another secret, or another query.
  std::string variant(const std::optional<std::string>& secret, const std::optional<std::string>& query) const {
    std::string out = dsn_;
    if (secret) {
      const std::size_t at = out.rfind('@');
      const std::size_t colon = out.find(':', std::string("orderbook://").size());
      out = out.substr(0, colon + 1) + *secret + out.substr(at);
    }
    if (query) out = out.substr(0, out.find('?')) + *query;
    return out;
  }

  std::string dsn_;
};

TEST_F(OrderbookSecureSlice, WithCredentialsAndTlsASessionWritesAndReads) {
  sde::OrderbookEngine engine(dsn_);
  engine.connect();
  const sde::Model declared = model();
  const sde::PlacementMap placement = sde::live::default_map(declared, "orderbook", "ob");
  sde::Session current(declared, placement, {{"ob", &engine}});
  const std::string symbol = fresh_book();
  current.save_many("DepthLevel", snapshot(symbol, kT0));
  sde::ScanOptions scan;
  scan.where = sde::Row{{"symbol", text(symbol)}, {"exchange", text(kExchange)}};
  EXPECT_EQ(current.scan("DepthLevel", scan).rows.size(), 6U);
  const std::optional<sde::Row> one = engine.get(kTable, key_of(snapshot(symbol, kT0).front()));
  ASSERT_TRUE(one.has_value());
  EXPECT_GE(std::get<std::int64_t>(one->at("sequence_number")), 1);
}

TEST_F(OrderbookSecureSlice, AWrongSecretIsRefusedAtConnectAsAnAuthenticationFailure) {
  // By name: a refusal for any other reason - a certificate the client does not accept, which is
  // how the reference's test once passed in CI - would say nothing about the secret.
  sde::OrderbookEngine wrong(variant("not-the-secret", std::nullopt));
  const std::string refused = failure_of([&] { wrong.connect(); });
  EXPECT_EQ(refused, "EngineError: could not open the orderbook engine: Authentication failed: ERR auth_failed");
  EXPECT_FALSE(contains(refused, "not-the-secret"));
}

TEST_F(OrderbookSecureSlice, WithoutTheCaThatSignedItTheCertificateIsRefused) {
  // TLS still on, the CA left out: the system's store is the anchor, and it knows nothing of it.
  sde::OrderbookEngine untrusted(variant(std::nullopt, std::string("?tls=on")));
  const std::string refused = failure_of([&] { untrusted.connect(); });
  EXPECT_TRUE(contains(refused, "could not open the orderbook engine: TLS handshake with ")) << refused;
  EXPECT_TRUE(contains(refused, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")) << refused;
}

TEST_F(OrderbookSecureSlice, PlainTextAgainstTheTlsPortTimesOutRatherThanConnecting) {
  // Measured: the server waits for a handshake and the client for its greeting, so the connection
  // times out rather than being refused - and a server that is not there at all would be refused.
  sde::OrderbookEngine plain(variant(std::nullopt, std::string("?timeout=2")));
  EXPECT_EQ(failure_of([&] { plain.connect(); }), "EngineError: could not open the orderbook engine: timed out");
}

}  // namespace
