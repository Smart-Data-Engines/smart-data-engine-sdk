#pragma once

/// What the library costs per operation (requirement 3.5: one percent, release-blocking), measured
/// as `docs/implementing.md` asks: the work the library adds, timed directly rather than as the
/// difference of two noisy totals, gated on the median, the tail held to an allowance.
///
/// The added work is the whole of `Session::get` for a point read - the usage gate, the shape, the
/// route, the table name, the telemetry, the row handed back - against an engine whose `get`
/// returns a row it already holds. Its median, less the median of that engine's `get` called
/// directly and timed the same way, is the library's: the row's copy and the clock's reads are in
/// both, and nothing over a socket is, so the difference is of two quiet numbers, not two noisy
/// ones. That is more than the reference times (its routing alone), deliberately: a number that
/// includes everything the session does cannot hide a cost in the part left out.
///
/// What it is divided by is the caller's: the cheapest round trip there is (`floor_round_trips`), or
/// an engine's. Gated only where the build is optimised and unsanitised: a debug build or a
/// sanitizer measures the compiler, and its numbers are printed and not judged.
///
/// Interference only ever adds time, so each number is the least of several rounds, each round
/// measuring its floor and its point reads back to back (`least_interfered`). Measured: four busy
/// loops beside one run doubled the library's median and left the floor's, and a run straight
/// after the live suite failed the gate on the code that had passed it ten times in a row. A
/// regression moves every round; a neighbour moves some.

#include <arpa/inet.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <sys/socket.h>
#include <unistd.h>

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <functional>
#include <optional>
#include <stdexcept>
#include <string>
#include <string_view>
#include <thread>
#include <utility>
#include <vector>

#include "sde/engine.hpp"
#include "sde/model.hpp"
#include "sde/placement.hpp"
#include "sde/session.hpp"
#include "sde/telemetry.hpp"

#if defined(__SANITIZE_ADDRESS__) || defined(__SANITIZE_THREAD__)
#define SDE_OVERHEAD_SANITIZED 1
#elif defined(__has_feature)
#if __has_feature(address_sanitizer) || __has_feature(thread_sanitizer) || \
    __has_feature(undefined_behavior_sanitizer)
#define SDE_OVERHEAD_SANITIZED 1
#endif
#endif

namespace sde::overhead {

inline constexpr double BUDGET = 0.01;  // requirement 3.5
/// How far past the budget a *tail* ratio may go, as in both other libraries' tests: an allowance
/// for a scheduler's pause, two orders of magnitude below a second round trip.
inline constexpr double TAIL_ALLOWANCE = 5;
inline constexpr int ROUND_TRIPS = 300;
inline constexpr int LIBRARY_CALLS = 200'000;

#if defined(__OPTIMIZE__) && !defined(SDE_OVERHEAD_SANITIZED)
inline constexpr bool GATED = true;
#else
inline constexpr bool GATED = false;
#endif

using Clock = std::chrono::steady_clock;

inline std::int64_t elapsed(Clock::time_point start) {
  return std::chrono::duration_cast<std::chrono::nanoseconds>(Clock::now() - start).count();
}

/// The sample at this fraction of the ordered samples, as the reference's tests take it.
inline double percentile(std::vector<std::int64_t> samples, double fraction) {
  std::ranges::sort(samples);
  const auto index = std::min(
      samples.size() - 1, static_cast<std::size_t>(static_cast<double>(samples.size()) * fraction));
  return static_cast<double>(samples[index]);
}

/// An engine that holds one row and hands it back: everything else in a point read is the library.
class HeldRow final : public Engine {
 public:
  explicit HeldRow(Row row) : row_(std::move(row)) {}
  [[nodiscard]] std::string_view dialect() const noexcept override { return "postgres"; }
  std::vector<PhysicalFinding> ensure_schema(const PhysicalLayout&, const Keys&) override {
    return {};
  }
  void insert(const std::string&, const Row&) override {}
  std::optional<Row> get(const std::string&, const Row&) override { return row_; }
  void transaction(const std::function<void()>& body) override { body(); }

 private:
  Row row_;
};

/// The cheapest round trip there is: one byte to an echo on loopback and back, no protocol.
inline std::vector<std::int64_t> floor_round_trips() {
  const auto refuse = [](const char* what) { throw std::runtime_error(what); };
  const int listener = ::socket(AF_INET, SOCK_STREAM, 0);
  if (listener < 0) refuse("socket");
  sockaddr_in address{};
  address.sin_family = AF_INET;
  address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
  socklen_t length = sizeof(address);
  if (::bind(listener, reinterpret_cast<sockaddr*>(&address), length) != 0 ||  // NOLINT
      ::listen(listener, 1) != 0 ||
      ::getsockname(listener, reinterpret_cast<sockaddr*>(&address), &length) != 0) {  // NOLINT
    ::close(listener);
    refuse("bind");
  }
  std::thread echo([listener] {
    const int peer = ::accept(listener, nullptr, nullptr);
    if (peer < 0) return;
    const int one = 1;
    ::setsockopt(peer, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
    char byte = 0;
    while (::recv(peer, &byte, 1, 0) == 1 && ::send(peer, &byte, 1, MSG_NOSIGNAL) == 1) {
    }
    ::close(peer);
  });
  const int client = ::socket(AF_INET, SOCK_STREAM, 0);
  const int one = 1;
  ::setsockopt(client, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
  std::vector<std::int64_t> samples;
  if (client >= 0 &&
      ::connect(client, reinterpret_cast<sockaddr*>(&address), sizeof(address)) == 0) {  // NOLINT
    char byte = 'x';
    for (int round = 0; round < ROUND_TRIPS + 50; ++round) {
      const auto start = Clock::now();
      if (::send(client, &byte, 1, MSG_NOSIGNAL) != 1 || ::recv(client, &byte, 1, 0) != 1) break;
      if (round >= 50) samples.push_back(elapsed(start));  // after a warm-up
    }
  }
  if (client >= 0) ::close(client);
  ::shutdown(listener, SHUT_RDWR);
  ::close(listener);
  echo.join();
  if (samples.size() != ROUND_TRIPS) refuse("the loopback round trips did not complete");
  return samples;
}

/// `count` timings of `read`, after a warm-up; a read that finds nothing is a broken fixture.
inline std::vector<std::int64_t> timed(const std::function<std::optional<Row>()>& read,
                                       int count = LIBRARY_CALLS, int warm_up = 1000) {
  for (int warm = 0; warm < warm_up; ++warm) (void)read();
  std::vector<std::int64_t> samples;
  samples.reserve(static_cast<std::size_t>(count));
  for (int call = 0; call < count; ++call) {
    const auto start = Clock::now();
    const std::optional<Row> row = read();
    samples.push_back(elapsed(start));
    if (!row) throw std::runtime_error("a point read found nothing");
  }
  return samples;
}

/// The point read of one entity through a session over `HeldRow`, and the work it added.
struct Added {
  double through_p50 = 0;
  double through_p99 = 0;
  double direct_p50 = 0;
  double p50 = 0;  ///< through less direct: the library's
};

inline Added point_read(bool recorded) {
  const Model model = load_neutral_model(R"({"entities": [{"name": "Reading", "fields": [
      {"name": "id", "type": "int64"}, {"name": "sensor", "type": "string"},
      {"name": "value", "type": "float64"}], "key": ["id"]}]})");
  LoadOptions options;
  options.model = &model;
  const PlacementMap map = load_map(R"({"contract": 3, "model_version": ")" + model.version() +
                                        R"(", "map_version": 1, "groups": {"Reading": {"source":
      {"id": "Reading@pg", "engine": "pg", "layout": {"auto": true}}}}})",
                                    options);
  HeldRow engine{Row{{"id", std::int64_t{7}}, {"sensor", std::string("s")}, {"value", 1.0}}};
  const Row key{{"id", std::int64_t{7}}};
  Recorder recorder(model);
  SessionOptions session_options;
  if (recorded) session_options.recorder = &recorder;
  Session session(model, map, {{"pg", &engine}}, session_options);
  const std::vector<std::int64_t> through = timed([&] { return session.get("Reading", key); });
  const std::string& table = map.placement_of("Reading").source.layout.table_for("Reading");
  const std::vector<std::int64_t> direct = timed([&] { return engine.get(table, key); });
  if (recorded && (!recorder.roll() || recorder.rejected() != 0)) {
    throw std::runtime_error("the recorder did not record every point read");
  }
  Added out;
  out.through_p50 = percentile(through, 0.5);
  out.through_p99 = percentile(through, 0.99);
  out.direct_p50 = percentile(direct, 0.5);
  out.p50 = std::max(out.through_p50 - out.direct_p50, 0.0);
  return out;
}

/// Each number the least of `rounds` rounds of the floor and the point reads, measured back to back.
struct LeastInterfered {
  int rounds = 0;
  double floor_p50 = 0;
  double floor_p99 = 0;
  Added point;  ///< `p50` is the least added work of a round, not the difference of two least
};

inline LeastInterfered least_interfered(bool recorded, int rounds = 5) {
  LeastInterfered out;
  out.rounds = rounds;
  for (int round = 0; round < rounds; ++round) {
    const std::vector<std::int64_t> floor = floor_round_trips();
    const Added added = point_read(recorded);
    const double floor_p50 = percentile(floor, 0.5);
    const double floor_p99 = percentile(floor, 0.99);
    if (round == 0) {
      out.floor_p50 = floor_p50;
      out.floor_p99 = floor_p99;
      out.point = added;
      continue;
    }
    out.floor_p50 = std::min(out.floor_p50, floor_p50);
    out.floor_p99 = std::min(out.floor_p99, floor_p99);
    out.point.through_p50 = std::min(out.point.through_p50, added.through_p50);
    out.point.through_p99 = std::min(out.point.through_p99, added.through_p99);
    out.point.direct_p50 = std::min(out.point.direct_p50, added.direct_p50);
    out.point.p50 = std::min(out.point.p50, added.p50);
  }
  return out;
}

}  // namespace sde::overhead
