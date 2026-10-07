/// The library's cost per point read, telemetry on, against a real one: a PostgreSQL point read
/// through this library's own adapter, over the environment's server. That is the operation
/// requirement 3.5 gives this library one percent of, measured in this runtime as the reference
/// measures its own - the added work timed apart (`support/overhead.hpp`) and divided by a round
/// trip timed through the driver, the library not in it. The floor, the stricter bar, is
/// `test_overhead.cpp`'s; there the path with a recorder reaches its budget, and here it is judged.
///
///     SDE_POSTGRES_DSN=postgresql://postgres:sde@127.0.0.1:55432/sde ctest -L live -R Overhead

#include <cstdint>
#include <cstdio>
#include <string>
#include <vector>

#include <gtest/gtest.h>

#include "live/live.hpp"
#include "live/postgres.hpp"
#include "sde/model.hpp"
#include "sde/placement.hpp"
#include "sde/postgres.hpp"
#include "sde/session.hpp"
#include "support/overhead.hpp"

namespace {

using namespace sde::overhead;

TEST(PostgresOverhead, ARecordedPointReadAddsUnderOnePercentOfAPostgresPointRead) {
  std::string dsn;
  SDE_REQUIRE_DSN("SDE_POSTGRES_DSN", dsn);
  const sde::live::Scope scope(dsn);
  const sde::Model model = sde::load_neutral_model(R"({"entities": [{"name": "Reading", "fields": [
      {"name": "id", "type": "int64"}, {"name": "sensor", "type": "string"},
      {"name": "value", "type": "float64"}], "key": ["id"]}]})");
  sde::LoadOptions options;
  options.model = &model;
  const sde::PlacementMap map = sde::load_map(R"({"contract": 3, "model_version": ")" +
                                                  model.version() + R"(", "map_version": 1,
      "groups": {"Reading": {"source": {"id": "Reading@pg", "engine": "pg",
      "layout": {"auto": true}}}}})",
                                              options);
  sde::PostgresEngine engine(scope.dsn());
  engine.connect();
  sde::Session(model, map, {{"pg", &engine}}).ensure_schema();
  const std::string& table = map.placement_of("Reading").source.layout.table_for("Reading");
  const sde::Row key{{"id", std::int64_t{7}}};
  engine.insert(table, {{"id", std::int64_t{7}}, {"sensor", std::string("s")}, {"value", 1.0}});

  // The round trip, through the driver: the library is not in it at all.
  const std::vector<std::int64_t> round_trips =
      timed([&] { return engine.get(table, key); }, ROUND_TRIPS, 50);
  const double round_trip_p50 = percentile(round_trips, 0.5);
  const double round_trip_p99 = percentile(round_trips, 0.99);
  const Added added = point_read(true);
  const double ratio = added.p50 / round_trip_p50;
  const double tail = added.through_p99 / round_trip_p99;
  std::printf(
      "  a recorded point read, against PostgreSQL\n"
      "    round trip p50         %9.1f us  (n=%d)\n"
      "    round trip p99         %9.1f us\n"
      "    library added p50      %9.3f us  (n=%d)\n"
      "    ratio p50/p50          %9.3f %%  budget %.0f %%\n"
      "    ratio p99/p99          %9.3f %%  ceiling %.0f %%  (the session's whole p99)\n",
      round_trip_p50 / 1000, ROUND_TRIPS, round_trip_p99 / 1000, added.p50 / 1000, LIBRARY_CALLS,
      ratio * 100, BUDGET * 100, tail * 100, BUDGET * TAIL_ALLOWANCE * 100);
  // And a floor on the denominator, so that a broken fixture cannot make the ratio look good.
  EXPECT_GT(round_trip_p50, 10'000.0) << "a PostgreSQL round trip under 10 us is not one";
  if (!GATED) return;
  EXPECT_LT(ratio, BUDGET) << "with telemetry the library adds " << ratio * 100
                           << "% of a PostgreSQL point read at the median, over the 1% budget";
  EXPECT_LT(tail, BUDGET * TAIL_ALLOWANCE)
      << "the session's p99 is " << tail * 100 << "% of the round trip's p99, past the "
      << BUDGET * TAIL_ALLOWANCE * 100 << "% allowance - a code path, not a scheduler's pause";
}

}  // namespace
