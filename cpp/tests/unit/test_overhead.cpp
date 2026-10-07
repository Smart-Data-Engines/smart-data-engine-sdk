/// The library's cost per point read against the **floor**: the cheapest round trip C++ can make,
/// a loopback socket this test starts itself (`support/overhead.hpp` says what is timed and how).
/// Anything a real engine does is slower, so one percent of the floor implies one percent of every
/// real operation, and the break-even says how fast an operation would have to be before this
/// library cost one percent of it.
///
/// Without telemetry the claim holds with room: on an i3-7100U, built optimised, 0.12 us at the
/// median against a floor of 25-40 us, a break-even of 12 us. With a recorder the latency costs two
/// reads of the clock and the recorder's atomics, 0.24 us, which is 0.6 to 1% of this floor - so
/// that path is printed here and gated against a real round trip instead (`test_overhead_postgres`).
/// Both were 0.64 and 0.75 us before the session decided a point read's shape and place once
/// instead of on every call: 2.5 and 2.9% of the floor (`Session::Session`).

#include <cstdio>
#include <vector>

#include <gtest/gtest.h>

#include "support/overhead.hpp"

namespace {

using namespace sde::overhead;

void report(const char* what, const Added& added, const std::vector<std::int64_t>& floor,
            bool gate) {
  const double floor_p50 = percentile(floor, 0.5);
  const double floor_p99 = percentile(floor, 0.99);
  const double ratio = added.p50 / floor_p50;
  const double tail = added.through_p99 / floor_p99;
  std::printf(
      "  %s\n"
      "    loopback floor p50     %9.1f us  (n=%d)\n"
      "    loopback floor p99     %9.1f us\n"
      "    through the session    %9.3f us  p50, %.3f us p99  (n=%d)\n"
      "    the engine alone       %9.3f us  p50\n"
      "    library added p50      %9.3f us\n"
      "    ratio p50/p50          %9.3f %%  budget %.0f %%\n"
      "    ratio p99/p99          %9.3f %%  ceiling %.0f %%  (the session's whole p99)\n"
      "    break-even             %9.1f us  (an operation faster than this pays over 1%%)\n",
      what, floor_p50 / 1000, ROUND_TRIPS, floor_p99 / 1000, added.through_p50 / 1000,
      added.through_p99 / 1000, LIBRARY_CALLS, added.direct_p50 / 1000, added.p50 / 1000,
      ratio * 100, BUDGET * 100, tail * 100, BUDGET * TAIL_ALLOWANCE * 100,
      added.p50 / BUDGET / 1000);
  // A floor under a microsecond is not a round trip, and would make any ratio look bad or good.
  EXPECT_GT(floor_p50, 1000.0) << "the loopback round trip measured under a microsecond";
  if (!gate) return;
  EXPECT_LT(ratio, BUDGET) << what << ": the library adds " << ratio * 100
                           << "% of the floor at the median, over the 1% budget of requirement 3.5";
  EXPECT_LT(tail, BUDGET * TAIL_ALLOWANCE)
      << what << ": the session's p99 is " << tail * 100 << "% of the floor's p99, past the "
      << BUDGET * TAIL_ALLOWANCE * 100 << "% allowance - a code path, not a scheduler's pause";
}

TEST(Overhead, APointReadAddsUnderOnePercentOfTheCheapestRoundTrip) {
  report("a point read, no telemetry", point_read(false), floor_round_trips(), GATED);
}

TEST(Overhead, ARecordedPointReadIsMeasuredAgainstTheCheapestRoundTrip) {
  report("a point read, recorded", point_read(true), floor_round_trips(), false);
}

}  // namespace
