#include "sde/books.hpp"

#include <algorithm>
#include <array>
#include <atomic>
#include <charconv>
#include <chrono>
#include <cmath>
#include <map>
#include <stdexcept>
#include <string>
#include <utility>

#include "sde/errors.hpp"

namespace sde {

namespace {

/// A side as the stream holds it: price to (quantity, order count).
using SideBook = std::map<std::int64_t, std::pair<std::int64_t, std::int64_t>>;

std::string quoted(std::string_view text) { return "'" + std::string(text) + "'"; }

/// A double in its shortest round-trip spelling, as the refusals print it.
std::string shortest(double value) {
  std::array<char, 64> buffer{};
  const auto [end, error] = std::to_chars(buffer.data(), buffer.data() + buffer.size(), value);
  return error == std::errc{} ? std::string(buffer.data(), end) : std::string("?");
}

/// Calls `visit(price, quantity and count)` in the side's own order: bids from the highest price,
/// asks from the lowest.
template <typename Visit>
void best_first(BookSide side, const SideBook& book, Visit&& visit) {
  if (side == BookSide::bid) {
    for (auto it = book.rbegin(); it != book.rend(); ++it) visit(it->first, it->second);
  } else {
    for (const auto& [price, value] : book) visit(price, value);
  }
}

std::vector<BookLevel> levels_of(BookSide side, const SideBook& book) {
  std::vector<BookLevel> out;
  out.reserve(book.size());
  best_first(side, book, [&](std::int64_t price, const std::pair<std::int64_t, std::int64_t>& v) {
    out.push_back({price, v.first, v.second});
  });
  return out;
}

void check_level(const BookLevel& level, const std::string& where) {
  if (level.quantity < 0 || level.order_count < 0) {
    throw std::invalid_argument(where + "has a negative quantity or order count");
  }
}

/// A whole side, checked: each price once, none with a quantity of 0.
SideBook whole_side(BookSide side, const std::vector<BookLevel>& levels) {
  const std::string name(to_string(side));
  SideBook out;
  for (std::size_t i = 0; i < levels.size(); ++i) {
    const BookLevel& level = levels[i];
    const std::string where = name + " level " + std::to_string(i) + " ";
    check_level(level, where);
    if (level.quantity == 0) {
      throw std::invalid_argument(where +
                                  "has a quantity of 0. A whole side lists the prices it holds; a "
                                  "removal is a change, which apply() sends");
    }
    if (!out.emplace(level.price, std::make_pair(level.quantity, level.order_count)).second) {
      throw std::invalid_argument(name + " price " + std::to_string(level.price) +
                                  " is in the side twice");
    }
  }
  return out;
}

/// The difference of two checked sides: the removals, then the rest, each best first.
std::vector<BookLevel> difference(BookSide side, const SideBook& before, const SideBook& after) {
  std::vector<BookLevel> out;
  best_first(side, before, [&](std::int64_t price, const std::pair<std::int64_t, std::int64_t>&) {
    if (after.find(price) == after.end()) out.push_back({price, 0, 0});
  });
  best_first(side, after, [&](std::int64_t price, const std::pair<std::int64_t, std::int64_t>& v) {
    const auto old = before.find(price);
    if (old == before.end() || old->second != v) out.push_back({price, v.first, v.second});
  });
  return out;
}

struct SideState {
  SideBook book;
  bool sent_whole = false;       // the side has been sent whole since the stream began
  bool unsure = false;           // a send failed: what the engine holds is not known
  std::int64_t since_whole = 0;  // updates since the side was last sent whole
  double whole_at = 0.0;         // the policy clock when it was
  std::int64_t last_ns = -1;     // the time of the last update accepted for the side
};

std::size_t index_of(BookSide side) noexcept { return side == BookSide::bid ? 0 : 1; }

/// One call at a time: a second, while one is in progress, is refused.
class Exclusive {
 public:
  explicit Exclusive(std::atomic<bool>& busy) : busy_(busy) {
    if (busy_.exchange(true)) {
      throw ResourceBusy(
          "this book stream already has an update in progress: one call at a time, so the engine "
          "applies the updates in the order the stream computed them");
    }
  }
  ~Exclusive() { busy_.store(false); }
  Exclusive(const Exclusive&) = delete;
  Exclusive& operator=(const Exclusive&) = delete;

 private:
  std::atomic<bool>& busy_;
};

}  // namespace

std::string_view to_string(BookSide side) noexcept { return side == BookSide::bid ? "bid" : "ask"; }

BookSide book_side(std::string_view name) {
  if (name == "bid") return BookSide::bid;
  if (name == "ask") return BookSide::ask;
  throw std::invalid_argument("side must be 'bid' or 'ask', not " + quoted(name));
}

std::string_view to_string(BookStreamMode mode) noexcept {
  return mode == BookStreamMode::full ? "full" : "incremental";
}

BookStreamMode book_stream_mode(std::string_view name) {
  if (name == "full") return BookStreamMode::full;
  if (name == "incremental") return BookStreamMode::incremental;
  throw std::invalid_argument("mode must be one of ['full', 'incremental'], not " + quoted(name));
}

std::vector<BookLevel> book_diff(BookSide side, const std::vector<BookLevel>& before,
                                 const std::vector<BookLevel>& after) {
  const SideBook old = whole_side(side, before);
  const SideBook now = whole_side(side, after);
  return difference(side, old, now);
}

std::vector<BookLevel> ordered_changes(BookSide side, const std::vector<BookLevel>& changes) {
  const std::string name(to_string(side));
  std::map<std::int64_t, BookLevel> seen;
  for (std::size_t i = 0; i < changes.size(); ++i) {
    check_level(changes[i], name + " change " + std::to_string(i) + " ");
    if (!seen.emplace(changes[i].price, changes[i]).second) {
      throw std::invalid_argument(name + " price " + std::to_string(changes[i].price) +
                                  " is changed twice in one update");
    }
  }
  std::vector<BookLevel> removals;
  std::vector<BookLevel> rest;
  const auto sort = [&](const BookLevel& level) {
    if (level.quantity == 0) {
      removals.push_back({level.price, 0, 0});
    } else {
      rest.push_back(level);
    }
  };
  if (side == BookSide::bid) {
    for (auto it = seen.rbegin(); it != seen.rend(); ++it) sort(it->second);
  } else {
    for (const auto& [price, level] : seen) sort(level);
  }
  removals.insert(removals.end(), rest.begin(), rest.end());
  return removals;
}

Resync::Resync(std::int64_t updates, double seconds) : updates_(updates), seconds_(seconds) {
  if (updates < 0) {
    throw std::invalid_argument("resync updates must be a non-negative integer, not " +
                                std::to_string(updates));
  }
  if (!std::isfinite(seconds) || seconds < 0) {
    throw std::invalid_argument("resync seconds must be a non-negative number, not " +
                                shortest(seconds));
  }
}

struct BookStream::State {
  std::shared_ptr<BookSink> sink;
  BookStreamMode mode = BookStreamMode::incremental;
  Resync resync{};
  std::size_t max_levels = ENGINE_DEPTH;
  std::function<double()> clock;
  std::function<std::int64_t()> wall_clock_ns;
  std::array<SideState, 2> sides{};
  StreamStats stats{};
  std::atomic<bool> busy{false};

  std::int64_t stamp(BookSide side, SideState& state, std::optional<std::int64_t> timestamp_ns,
                     bool record) const {
    std::int64_t at = 0;
    if (!timestamp_ns) {
      at = std::max(wall_clock_ns(), state.last_ns);
    } else {
      if (*timestamp_ns < 0) {
        throw std::invalid_argument(
            "timestamp_ns must be an integer of nanoseconds in [0, 2^63), not " +
            std::to_string(*timestamp_ns));
      }
      if (*timestamp_ns < state.last_ns) {
        throw std::invalid_argument(
            "timestamp_ns " + std::to_string(*timestamp_ns) + " is earlier than " +
            std::to_string(state.last_ns) + ", the time of the last update of the " +
            std::string(to_string(side)) +
            " side: the engine's history applies updates in time order and its live book in the "
            "order they arrive, so the two would disagree. Nothing was sent");
      }
      at = *timestamp_ns;
    }
    if (record) state.last_ns = at;
    return at;
  }

  [[nodiscard]] bool due(const SideState& state) const {
    if (!state.sent_whole || state.unsure) return true;
    if (resync.updates() != 0 && state.since_whole >= resync.updates()) return true;
    return resync.seconds() != 0.0 && clock() - state.whole_at >= resync.seconds();
  }

  void send_whole(BookSide side, SideState& state, SideBook now, std::int64_t at) {
    std::vector<BookLevel> levels = levels_of(side, now);
    if (levels.size() > max_levels) levels.resize(max_levels);
    state.book = std::move(now);
    try {
      sink->send_whole(side, levels, at);
    } catch (...) {
      state.unsure = true;
      throw;
    }
    state.unsure = false;
    state.sent_whole = true;
    state.since_whole = 0;
    state.whole_at = clock();
    stats.whole += 1;
    stats.levels_whole += levels.size();
  }

  void send_changes(BookSide side, SideState& state, SideBook now,
                    const std::vector<BookLevel>& levels, std::int64_t at) {
    state.book = std::move(now);
    state.since_whole += 1;
    if (levels.empty()) {
      stats.unchanged += 1;
      return;
    }
    try {
      sink->send_changes(side, levels, at);
    } catch (...) {
      state.unsure = true;
      throw;
    }
    stats.changes += 1;
    stats.levels_changes += levels.size();
  }
};

BookStream::BookStream(std::shared_ptr<BookSink> sink, BookStreamOptions options)
    : state_(std::make_unique<State>()) {
  if (!sink) throw std::invalid_argument("a book stream needs a sink to send to");
  if (options.max_levels < 1) {
    throw std::invalid_argument("max_levels must be a positive integer, not " +
                                std::to_string(options.max_levels));
  }
  State& s = *state_;
  s.sink = std::move(sink);
  s.mode = options.mode;
  s.resync = options.resync;
  s.max_levels = options.max_levels;
  s.clock = options.clock ? std::move(options.clock) : [] {
    return std::chrono::duration<double>(std::chrono::steady_clock::now().time_since_epoch())
        .count();
  };
  s.wall_clock_ns = options.wall_clock_ns ? std::move(options.wall_clock_ns) : [] {
    return static_cast<std::int64_t>(std::chrono::duration_cast<std::chrono::nanoseconds>(
                                         std::chrono::system_clock::now().time_since_epoch())
                                         .count());
  };
}

BookStream::~BookStream() = default;
BookStream::BookStream(BookStream&&) noexcept = default;
BookStream& BookStream::operator=(BookStream&&) noexcept = default;

BookStreamMode BookStream::mode() const noexcept { return state_->mode; }

const StreamStats& BookStream::stats() const noexcept { return state_->stats; }

std::vector<BookLevel> BookStream::book(BookSide side) const {
  return levels_of(side, state_->sides[index_of(side)].book);
}

void BookStream::update(BookSide side, const std::vector<BookLevel>& levels,
                        std::optional<std::int64_t> timestamp_ns) {
  State& s = *state_;
  const Exclusive one(s.busy);
  SideBook now = whole_side(side, levels);
  if (now.size() > s.max_levels) {
    const std::string most = std::to_string(s.max_levels);
    throw std::invalid_argument("the " + std::string(to_string(side)) + " side has " +
                                std::to_string(now.size()) + " prices and the engine keeps " +
                                most + " a side: give its best " + most + ". Nothing was sent");
  }
  SideState& state = s.sides[index_of(side)];
  const std::int64_t at = s.stamp(side, state, timestamp_ns, true);
  s.stats.updates += 1;
  if (s.mode == BookStreamMode::full || s.due(state)) {
    s.send_whole(side, state, std::move(now), at);
    return;
  }
  std::vector<BookLevel> changes = difference(side, state.book, now);
  if (changes.size() > now.size()) {
    s.send_whole(side, state, std::move(now), at);
    return;
  }
  s.send_changes(side, state, std::move(now), changes, at);
}

void BookStream::apply(BookSide side, const std::vector<BookLevel>& changes,
                       std::optional<std::int64_t> timestamp_ns) {
  State& s = *state_;
  const Exclusive one(s.busy);
  if (s.mode != BookStreamMode::incremental) {
    throw std::invalid_argument(
        "apply() sends changes, which a full stream does not: it sends every side whole. Give it "
        "the whole side with update(), or open the stream incremental");
  }
  const std::vector<BookLevel> ordered = ordered_changes(side, changes);
  SideState& state = s.sides[index_of(side)];
  const std::int64_t at = s.stamp(side, state, timestamp_ns, true);
  s.stats.updates += 1;
  SideBook now = state.book;
  for (const BookLevel& level : ordered) {
    if (level.quantity == 0) {
      now.erase(level.price);
    } else {
      now[level.price] = {level.quantity, level.order_count};
    }
  }
  if (s.due(state) || ordered.size() > std::min(now.size(), s.max_levels)) {
    s.send_whole(side, state, std::move(now), at);
    return;
  }
  // Sent as given, a removal of a price the stream's book does not hold included: the engine's
  // book may hold it, and removing a price nothing holds changes nothing.
  s.send_changes(side, state, std::move(now), ordered, at);
}

void BookStream::resync(std::optional<BookSide> side, std::optional<std::int64_t> timestamp_ns) {
  State& s = *state_;
  const Exclusive one(s.busy);
  std::vector<BookSide> sides;
  if (side) {
    sides.push_back(*side);
  } else {
    sides = {BookSide::bid, BookSide::ask};
  }
  // Every time first, so that a refusal for the second side sends nothing for the first.
  std::vector<std::pair<BookSide, std::int64_t>> stamps;
  for (const BookSide each : sides) {
    SideState& state = s.sides[index_of(each)];
    if (!timestamp_ns && state.last_ns >= 0) {
      stamps.emplace_back(each, state.last_ns);
    } else {
      stamps.emplace_back(each, s.stamp(each, state, timestamp_ns, false));
    }
  }
  for (const auto& [each, at] : stamps) {
    SideState& state = s.sides[index_of(each)];
    state.last_ns = std::max(state.last_ns, at);
    s.send_whole(each, state, state.book, at);
  }
}

}  // namespace sde
