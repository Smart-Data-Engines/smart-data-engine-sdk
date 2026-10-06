/// The recorder and the window, where the shared vectors do not reach: threads, rolls racing
/// writes, the buffer, storage samples and the arithmetic past 64 bits.

#include <atomic>
#include <cstdint>
#include <thread>
#include <vector>

#include <gtest/gtest.h>

#include "sde/model.hpp"
#include "sde/telemetry.hpp"

namespace {

sde::Model events() {
  return sde::load_neutral_model(R"({"entities": [
    {"name": "Event", "fields": [{"name": "id", "type": "int64"},
                                 {"name": "at", "type": "timestamptz"}], "key": ["id"]}]})");
}

const sde::OperationShape& shape_of(const sde::Model& model, std::string_view kind) {
  for (const sde::OperationShape& shape : model.shapes()) {
    if (shape.kind == kind) return shape;
  }
  throw std::logic_error("no such kind");
}

std::uint64_t calls_in(const sde::Window& window) {
  std::uint64_t calls = 0;
  for (const sde::ShapeStats& stats : window.shapes) calls += stats.calls;
  return calls;
}

TEST(Histogram, BucketsByTheBitLengthOfWholeMicroseconds) {
  EXPECT_EQ(sde::Histogram::bucket_of(-5), 0);
  EXPECT_EQ(sde::Histogram::bucket_of(999), 0);
  EXPECT_EQ(sde::Histogram::bucket_of(1000), 1);
  EXPECT_EQ(sde::Histogram::bucket_of(1999), 1);
  EXPECT_EQ(sde::Histogram::bucket_of(2000), 2);
  EXPECT_EQ(sde::Histogram::bucket_of(INT64_MAX), sde::BUCKET_COUNT - 1);
  EXPECT_EQ(sde::Histogram::upper_edge_ms(0), 0.001);
  EXPECT_EQ(sde::Histogram::upper_edge_ms(24), 16777.216);
  const sde::Histogram empty;
  EXPECT_FALSE(empty.percentile_ms(0.5).has_value());
}

TEST(Recorder, ANothingWindowIsNoWindowAndAFanOutAloneIsOne) {
  const sde::Model model = events();
  std::int64_t now = 0;
  sde::Recorder recorder(model, 64, [&now] { return now; });
  EXPECT_FALSE(recorder.roll().has_value());
  recorder.record_fan_out("Event", "Event@ch", 2000, true);
  const auto window = recorder.roll();
  ASSERT_TRUE(window.has_value());
  EXPECT_TRUE(window->shapes.empty());
  ASSERT_EQ(window->fanned.size(), 1U);
  EXPECT_EQ(window->fanned[0].failures, 1U);
  EXPECT_EQ(window->copies("Event").at(0).as_record().find("complete")->as_bool(), false);
}

TEST(Recorder, AShapeOfAnotherModelIsDroppedAndCounted) {
  const sde::Model model = events();
  const sde::Model other = sde::load_neutral_model(R"({"entities": [
    {"name": "Other", "fields": [{"name": "id", "type": "int64"}], "key": ["id"]}]})");
  sde::Recorder recorder(model);
  recorder.record(other.shapes().front(), 1000);
  EXPECT_EQ(recorder.rejected(), 1U);
  EXPECT_FALSE(recorder.roll().has_value());
}

TEST(Recorder, MarksAWindowIncompleteOnceAndKeepsTheBufferBounded) {
  const sde::Model model = events();
  sde::Recorder recorder(model, 2);
  const auto& write = shape_of(model, "write");
  for (int i = 0; i < 4; ++i) {
    if (i == 1) recorder.mark_incomplete();
    recorder.record(write, 1000, 1);
    const auto window = recorder.roll();
    ASSERT_TRUE(window.has_value());
    EXPECT_EQ(window->complete, i != 1) << i;
    // Drops are counted before a window joins the buffer: the third roll is the first to drop.
    EXPECT_EQ(window->dropped_windows, static_cast<std::uint64_t>(i < 3 ? 0 : 1)) << i;
  }
  EXPECT_EQ(recorder.pending().size(), 2U);
  recorder.acknowledge(1);
  EXPECT_EQ(recorder.pending().size(), 1U);
  recorder.acknowledge(10);
  EXPECT_TRUE(recorder.pending().empty());
}

TEST(Recorder, CountsWriteRowsBySecondAndOnlyForWritesThatSucceeded) {
  const sde::Model model = events();
  std::int64_t now = 0;
  sde::Recorder recorder(model, 64, [&now] { return now; });
  const auto& write = shape_of(model, "write");
  const auto& read = shape_of(model, "point_read");
  recorder.record(write, 1000, 5);
  now = 1'500'000'000;
  recorder.record(write, 1000, 7);
  recorder.record(write, 1000, 9, true);  // failed: no rows
  recorder.record(read, 1000, 3);         // a read: no rows written
  now = 3'000'000'000;
  const auto window = recorder.roll();
  ASSERT_TRUE(window.has_value());
  const auto& seconds = window->write_seconds.at("Event");
  EXPECT_EQ(seconds.size(), 2U);
  EXPECT_EQ(seconds.at(0), 5U);
  EXPECT_EQ(seconds.at(1), 7U);
  // M * S / W: the busiest second (7), three whole seconds, twelve rows.
  EXPECT_EQ(window->features("Event").write_burstiness, 7.0 * 3.0 / 12.0);
}

TEST(Window, TheSpanCoversEverySecondAWriteLandedIn) {
  // `max(1, duration, last + 1)`: a window whose clock closed it before the second its last write
  // landed in - a clock the caller supplies need not agree with the order of calls - still counts
  // that second. Four rows in second 5 of a three-second window: six seconds, burstiness six.
  sde::Window late;
  late.started_ns = 0;
  late.ended_ns = 3'000'000'000;
  late.shapes.push_back(sde::ShapeStats{"w", "Event", "Event", "write", 1, 4, 0, {}, {}});
  late.shapes.back().latency.record(1000);
  late.write_seconds["Event"][5] = 4;
  EXPECT_EQ(late.features("Event").write_burstiness, 4.0 * 6.0 / 4.0);
}

TEST(Recorder, ReportsTheEqualityFieldsOfAFilterSortedAndOnce) {
  const sde::Model model = events();
  sde::Recorder recorder(model);
  const auto& scan = shape_of(model, "full_scan");
  const sde::Filter filter{{"id", "at", "id"}, std::nullopt};
  recorder.record(scan, 1000, 1, false, &filter);
  const sde::Filter same{{"at", "id"}, std::nullopt};
  recorder.record(scan, 1000, 1, false, &same);
  const auto window = recorder.roll();
  ASSERT_TRUE(window.has_value());
  const sde::Json records = window->shape_records(model, "Event");
  ASSERT_EQ(records.as_array().size(), 1U);
  EXPECT_EQ(sde::dump_json(*records.as_array()[0].find("filtered_on")),
            R"([{"equal":["at","id"],"calls":2}])");
}

TEST(Recorder, StorageSamplesAreCheckedAndKeptForADay) {
  const sde::Model model = events();
  std::int64_t now = 0;
  sde::Recorder recorder(model, 64, [&now] { return now; });
  recorder.record_storage("Event", 100, 200);  // the index part outside the total
  recorder.record_storage("Event", -1, 0);
  recorder.record_storage("", 1, 0);
  EXPECT_EQ(recorder.rejected(), 3U);
  recorder.record_storage("Event", 1000, 100);
  now = sde::DAY_NS + 1;
  recorder.record_storage("Event", 4000, 100);  // the first sample is now older than a day
  recorder.record(shape_of(model, "write"), 1000, 1);
  const auto window = recorder.roll();
  ASSERT_TRUE(window.has_value());
  EXPECT_EQ(window->storage.size(), 2U);
  ASSERT_EQ(window->storage_history.size(), 1U);
  EXPECT_EQ(window->storage_history[0].total_bytes, 4000);
}

TEST(Window, GrowthPastSixtyFourBitsIsExact) {
  // A terabyte's change over an hour and a nanosecond: |delta| * a day is past 2^64, and the
  // projection is the reference's exact integer, truncated toward zero.
  sde::Window window;
  window.shapes.push_back(sde::ShapeStats{"x", "G", "E", "write", 1, 1, 0, {}, {}});
  window.shapes.back().latency.record(1000);
  const std::int64_t tb = 1'000'000'000'000;
  window.storage = {{"G", sde::GROWTH_MIN_NS + 1, 5 * tb, 0}};
  window.storage_history = {{"G", 0, 6 * tb, 0}, window.storage[0]};
  const auto features = window.features("G");
  // -(1e12 * 86400e9 // (3600e9 + 1)), computed by Python's integers.
  EXPECT_EQ(features.daily_growth_bytes, -23999999999993);
  EXPECT_EQ(features.total_bytes, 5 * tb);
}

TEST(Recorder, ThreadsAndRollsLoseNoCallAndCountNoneTwice) {
  // Writers on every shape while another thread rolls: every call lands in exactly one window.
  const sde::Model model = events();
  sde::Recorder recorder(model, 1'000'000);
  constexpr int kThreads = 4;
  constexpr int kPerThread = 20'000;
  std::atomic<bool> done{false};
  std::uint64_t seen = 0;
  std::thread roller([&] {
    while (!done.load()) {
      if (auto window = recorder.roll()) seen += calls_in(*window);
      std::this_thread::yield();
    }
  });
  std::vector<std::thread> writers;
  for (int t = 0; t < kThreads; ++t) {
    writers.emplace_back([&, t] {
      const sde::Filter filter{{"id"}, std::nullopt};
      for (int i = 0; i < kPerThread; ++i) {
        const auto& shape = model.shapes()[static_cast<std::size_t>(i + t) % model.shapes().size()];
        recorder.record(shape, 1000 + i, 1, false, i % 3 == 0 ? &filter : nullptr);
      }
    });
  }
  for (std::thread& writer : writers) writer.join();
  done.store(true);
  roller.join();
  if (auto window = recorder.roll()) seen += calls_in(*window);
  EXPECT_EQ(seen, static_cast<std::uint64_t>(kThreads) * kPerThread);
  EXPECT_EQ(recorder.rejected(), 0U);
}

}  // namespace
