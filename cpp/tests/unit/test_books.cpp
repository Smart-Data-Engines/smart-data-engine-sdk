/// The book stream's own checks, beyond the shared vectors (`conformance/test_books.cpp`): what it
/// counts, what its settings refuse, that a refusal changes nothing, and one call at a time.

#include <cmath>
#include <limits>
#include <memory>
#include <stdexcept>
#include <vector>

#include <gtest/gtest.h>

#include "sde/books.hpp"
#include "sde/errors.hpp"

namespace {

using sde::BookLevel;
using sde::BookSide;

class Counting : public sde::BookSink {
 public:
  int whole = 0;
  int changes = 0;
  void send_whole(BookSide, const std::vector<BookLevel>&, std::int64_t) override { ++whole; }
  void send_changes(BookSide, const std::vector<BookLevel>&, std::int64_t) override { ++changes; }
};

sde::BookStreamOptions at_zero() {
  sde::BookStreamOptions options;
  options.clock = [] { return 0.0; };
  return options;
}

TEST(BookStream, CountsWhatItSent) {
  auto sink = std::make_shared<Counting>();
  sde::BookStream stream(sink, at_zero());
  stream.update(BookSide::bid, {{100, 1, 1}, {99, 1, 1}});
  stream.update(BookSide::bid, {{100, 1, 1}, {99, 2, 1}});
  stream.update(BookSide::bid, {{100, 1, 1}, {99, 2, 1}});
  const sde::StreamStats& stats = stream.stats();
  EXPECT_EQ(stats.updates, 3U);
  EXPECT_EQ(stats.whole, 1U);
  EXPECT_EQ(stats.changes, 1U);
  EXPECT_EQ(stats.unchanged, 1U);
  EXPECT_EQ(stats.levels_whole, 2U);
  EXPECT_EQ(stats.levels_changes, 1U);
  EXPECT_EQ(sink->whole, 1);
  EXPECT_EQ(sink->changes, 1);
}

template <typename Body>
void refused(Body&& body, const std::string& match) {
  try {
    body();
    ADD_FAILURE() << "accepted; expected a refusal holding '" << match << "'";
  } catch (const std::invalid_argument& error) {
    EXPECT_NE(std::string(error.what()).find(match), std::string::npos) << error.what();
  }
}

TEST(BookStream, TheSettingsRefuseWhatTheyCannotMean) {
  refused([] { sde::Resync(-1, 60); }, "resync updates");
  refused([] { sde::Resync(10, std::nan("")); }, "resync seconds");
  refused([] { sde::Resync(10, std::numeric_limits<double>::infinity()); }, "resync seconds");
  refused([] { sde::Resync(10, -1.0); }, "resync seconds");
  refused([] { (void)sde::book_stream_mode("some"); }, "mode must be one of");
  refused([] { (void)sde::book_side("buy"); }, "side must be 'bid' or 'ask', not 'buy'");
  auto sink = std::make_shared<Counting>();
  refused(
      [&] {
        sde::BookStreamOptions options;
        options.max_levels = 0;
        sde::BookStream stream(sink, options);
      },
      "max_levels");
  refused([] { sde::BookStream stream(nullptr); }, "sink");
  refused([&] { sde::BookStream(sink, at_zero()).update(BookSide::bid, {}, -1); }, "timestamp_ns");
  EXPECT_EQ(sink->whole + sink->changes, 0);
}

TEST(BookStream, ARefusedTimeChangesNothing) {
  // The refusal is checked before anything moves: the next update is a difference against the side
  // last sent, not against the refused one.
  auto sink = std::make_shared<Counting>();
  sde::BookStream stream(sink, at_zero());
  stream.update(BookSide::bid, {{100, 1, 1}, {99, 1, 1}}, 20);
  refused([&] { stream.update(BookSide::bid, {{101, 1, 1}, {99, 1, 1}}, 19); }, "earlier");
  EXPECT_EQ(stream.book(BookSide::bid), (std::vector<BookLevel>{{100, 1, 1}, {99, 1, 1}}));
  EXPECT_EQ(stream.stats().updates, 1U);
}

TEST(BookStream, ARefusedLevelChangesNothingEither) {
  auto sink = std::make_shared<Counting>();
  sde::BookStream stream(sink, at_zero());
  stream.update(BookSide::ask, {{100, 1, 1}}, 10);
  refused([&] { stream.update(BookSide::ask, {{101, -1, 1}}, 11); }, "negative");
  refused([&] { stream.apply(BookSide::ask, {{101, 1, 1}, {101, 0, 0}}, 11); }, "twice");
  EXPECT_EQ(stream.book(BookSide::ask), (std::vector<BookLevel>{{100, 1, 1}}));
  EXPECT_EQ(stream.stats().updates, 1U);
  stream.apply(BookSide::ask, {{101, 2, 1}}, 11);  // the time 11 was never taken
  EXPECT_EQ(sink->changes, 1);
}

/// A sink that writes to the stream it serves, as a careless callback would.
class Reentering : public sde::BookSink {
 public:
  sde::BookStream* stream = nullptr;
  bool refused_busy = false;
  void send_whole(BookSide side, const std::vector<BookLevel>&, std::int64_t) override {
    try {
      stream->update(side, {{1, 1, 1}});
    } catch (const sde::ResourceBusy&) {
      refused_busy = true;
    }
  }
  void send_changes(BookSide, const std::vector<BookLevel>&, std::int64_t) override {}
};

TEST(BookStream, OneCallAtATime) {
  auto sink = std::make_shared<Reentering>();
  sde::BookStream stream(sink, at_zero());
  sink->stream = &stream;
  stream.update(BookSide::bid, {{100, 1, 1}}, 1);
  EXPECT_TRUE(sink->refused_busy);
  EXPECT_EQ(stream.book(BookSide::bid), (std::vector<BookLevel>{{100, 1, 1}}));
  // The guard is released after the call, and after a refusal.
  sink->stream = nullptr;
  refused([&] { stream.update(BookSide::bid, {{100, 1, 1}}, 0); }, "earlier");
  EXPECT_NO_THROW(stream.update(BookSide::bid, {{100, 2, 1}}, 2));
}

TEST(BookStream, AMovedStreamKeepsItsSides) {
  auto sink = std::make_shared<Counting>();
  sde::BookStream first(sink, at_zero());
  first.update(BookSide::bid, {{100, 1, 1}}, 5);
  sde::BookStream second(std::move(first));
  second.update(BookSide::bid, {{100, 1, 1}, {99, 1, 1}}, 6);
  EXPECT_EQ(sink->whole, 1);
  EXPECT_EQ(sink->changes, 1);
  EXPECT_EQ(second.book(BookSide::bid), (std::vector<BookLevel>{{100, 1, 1}, {99, 1, 1}}));
}

TEST(BookDiff, OrderedChangesRefuseAPriceTwiceAndSendRemovalsFirst) {
  refused([] { (void)sde::ordered_changes(BookSide::bid, {{100, 1, 1}, {100, 0, 0}}); }, "twice");
  EXPECT_EQ(sde::ordered_changes(BookSide::ask, {{103, 1, 1}, {101, 0, 7}, {102, 2, 1}, {104, 0, 0}}),
            (std::vector<BookLevel>{{101, 0, 0}, {104, 0, 0}, {102, 2, 1}, {103, 1, 1}}));
  EXPECT_EQ(sde::ordered_changes(BookSide::bid, {{103, 1, 1}, {101, 0, 7}, {102, 2, 1}, {104, 0, 0}}),
            (std::vector<BookLevel>{{104, 0, 0}, {101, 0, 0}, {103, 1, 1}, {102, 2, 1}}));
}

}  // namespace
