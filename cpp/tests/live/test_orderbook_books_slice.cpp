/// A book stream against a real `ob_tcp_server`, from C++: the book the engine composes from what a
/// stream sent is the book the application gave it, in both stream modes.
///
/// The application is a seeded walk of whole sides and of changes, and the oracle is the walk's own
/// book, not the stream's. After every step the engine is asked for the book as of that step's time
/// (`Session::book_at`), which it composes by price from history. TypeScript's
/// `orderbook.books.test.ts`, ported; what a stream sends is the same in every library, which the
/// shared `books/` vectors hold. Runs where `SDE_ORDERBOOK_TCP=host:port` names a server, as the
/// `orderbook` CI job does.

#include <array>
#include <cctype>
#include <cstdint>
#include <map>
#include <memory>
#include <random>
#include <set>
#include <string>
#include <utility>
#include <vector>

#include <gtest/gtest.h>

#include "live/live.hpp"
#include "live/maps.hpp"
#include "sde/books.hpp"
#include "sde/model.hpp"
#include "sde/orderbook.hpp"
#include "sde/session.hpp"

namespace {

using sde::BookLevel;
using sde::BookSide;

const std::string kExchange = "binance";
constexpr std::int64_t kT0 = 1790000000000000000;

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

/// A symbol no other test has written to: over TCP every test shares one server.
std::string fresh_book() {
  std::string out = "T";
  for (const char c : sde::live::fresh(12)) {
    out += static_cast<char>(std::toupper(static_cast<unsigned char>(c)));
  }
  return out;
}

using Truth = std::map<std::int64_t, std::pair<std::int64_t, std::int64_t>>;

std::vector<BookLevel> levels(BookSide side, const Truth& book) {
  std::vector<BookLevel> out;
  if (side == BookSide::bid) {
    for (auto it = book.rbegin(); it != book.rend(); ++it) {
      out.push_back({it->first, it->second.first, it->second.second});
    }
  } else {
    for (const auto& [price, value] : book) out.push_back({price, value.first, value.second});
  }
  return out;
}

class BooksSlice : public ::testing::Test {
 protected:
  void SetUp() override {
    SDE_REQUIRE_DSN("SDE_ORDERBOOK_TCP", address_);
    engine_ = std::make_unique<sde::OrderbookEngine>("orderbook://" + address_);
    engine_->connect();
    placement_ =
        std::make_unique<sde::PlacementMap>(sde::live::default_map(model_, "orderbook", "ob"));
    session_ = std::make_unique<sde::Session>(
        model_, *placement_, std::map<std::string, sde::Engine*>{{"ob", engine_.get()}});
    session_->ensure_schema();
  }
  void TearDown() override {
    if (engine_) engine_->close();
  }

  /// 120 steps of one book, each side inside its own band of prices, so a side holds at most sixty;
  /// the book read back after every one.
  void walk(sde::BookStreamMode mode, std::uint32_t seed) {
    const std::string symbol = fresh_book();
    sde::BookStream stream =
        session_->book_stream("DepthLevel", symbol, kExchange, mode, sde::Resync(9, 0));
    std::mt19937 random(seed);
    const auto between = [&](std::int64_t low, std::int64_t high) {
      return std::uniform_int_distribution<std::int64_t>(low, high - 1)(random);
    };
    const auto chance = [&] { return std::uniform_real_distribution<double>(0.0, 1.0)(random); };
    const std::array<std::pair<std::int64_t, std::int64_t>, 2> bands = {{{940, 1000}, {1000, 1060}}};
    std::array<Truth, 2> truth;
    std::int64_t at = kT0;
    for (int step = 0; step < 120; ++step) {
      at += 1000;
      const std::size_t i = step < 2 ? static_cast<std::size_t>(step)
                                     : static_cast<std::size_t>(between(0, 2));
      const BookSide side = i == 0 ? BookSide::bid : BookSide::ask;
      const auto [low, high] = bands[i];
      if (mode == sde::BookStreamMode::incremental && step >= 2 && chance() < 0.5) {
        std::vector<BookLevel> changes;
        std::set<std::int64_t> picked;
        for (std::int64_t k = between(1, 9); k > 0; --k) picked.insert(between(low, high));
        for (const std::int64_t price : picked) {
          const bool held = truth[i].contains(price);
          if ((held && chance() < 0.5) || (!held && chance() < 0.2)) {
            changes.push_back({price, 0, 0});
            truth[i].erase(price);
          } else {
            const std::pair<std::int64_t, std::int64_t> value{between(1, 51), between(1, 6)};
            changes.push_back({price, value.first, value.second});
            truth[i][price] = value;
          }
        }
        stream.apply(side, changes, at);
      } else {
        Truth next;
        for (const auto& entry : truth[i]) {
          if (chance() > 0.2) next.insert(entry);
        }
        for (std::int64_t k = between(1, 7); k > 0; --k) {
          next[between(low, high)] = {between(1, 51), between(1, 6)};
        }
        truth[i] = next;
        stream.update(side, levels(side, next), at);
      }
      const sde::Book read = session_->book_at("DepthLevel", symbol, kExchange, at);
      ASSERT_EQ(read.bid, levels(BookSide::bid, truth[0])) << "step " << step;
      ASSERT_EQ(read.ask, levels(BookSide::ask, truth[1])) << "step " << step;
    }
    if (mode == sde::BookStreamMode::incremental) {
      EXPECT_GT(stream.stats().changes, 0U);
      EXPECT_GT(stream.stats().whole, 0U);
    }
  }

  std::string address_;
  sde::Model model_ = model();
  std::unique_ptr<sde::OrderbookEngine> engine_;
  std::unique_ptr<sde::PlacementMap> placement_;
  std::unique_ptr<sde::Session> session_;
};

TEST_F(BooksSlice, AFullStreamComposesTheBookTheApplicationGave) {
  walk(sde::BookStreamMode::full, 233);
}

TEST_F(BooksSlice, AnIncrementalStreamComposesTheBookTheApplicationGave) {
  walk(sde::BookStreamMode::incremental, 235);
}

TEST_F(BooksSlice, AnEarlierBookReadsAsItWas) {
  const std::string symbol = fresh_book();
  sde::BookStream stream = session_->book_stream("DepthLevel", symbol, kExchange);
  stream.update(BookSide::ask, {{1000, 5, 1}, {1001, 3, 1}, {1002, 2, 1}}, kT0 + 10);
  stream.apply(BookSide::ask, {{1001, 0, 0}, {1003, 7, 2}}, kT0 + 20);
  stream.update(BookSide::ask, {{1000, 5, 1}, {1003, 8, 2}}, kT0 + 30);
  const auto read = [&](std::int64_t at) {
    return session_->book_at("DepthLevel", symbol, kExchange, at).ask;
  };
  EXPECT_EQ(read(kT0 + 10), (std::vector<BookLevel>{{1000, 5, 1}, {1001, 3, 1}, {1002, 2, 1}}));
  EXPECT_EQ(read(kT0 + 25), (std::vector<BookLevel>{{1000, 5, 1}, {1002, 2, 1}, {1003, 7, 2}}));
  EXPECT_EQ(read(kT0 + 30), (std::vector<BookLevel>{{1000, 5, 1}, {1003, 8, 2}}));
  EXPECT_EQ(stream.stats().whole, 1U);
  EXPECT_EQ(stream.stats().changes, 2U);
}

TEST_F(BooksSlice, ADepthBoundsEachSide) {
  const std::string symbol = fresh_book();
  sde::BookStream stream = session_->book_stream("DepthLevel", symbol, kExchange);
  stream.update(BookSide::bid, {{100, 1, 1}, {99, 2, 1}, {98, 3, 1}}, kT0 + 1);
  stream.update(BookSide::ask, {{101, 1, 1}, {102, 2, 1}, {103, 3, 1}}, kT0 + 1);
  const sde::Book top = session_->book_at("DepthLevel", symbol, kExchange, kT0 + 1, 2);
  EXPECT_EQ(top.bid, (std::vector<BookLevel>{{100, 1, 1}, {99, 2, 1}}));
  EXPECT_EQ(top.ask, (std::vector<BookLevel>{{101, 1, 1}, {102, 2, 1}}));
}

}  // namespace
