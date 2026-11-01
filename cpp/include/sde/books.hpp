#pragma once
/// A book stream (format contract section 8c): one side of an order book at a time, sent whole or as
/// its changes.
///
/// The orderbook engine reads an update by price (its #233): each level sets the quantity of its
/// price, a quantity of 0 removes the price, and a price the update does not name stays. So a writer
/// of books can send two things, and a `BookStream` sends either:
///
/// - `BookStreamMode::full`: every side the application gives is sent whole, as a side replacement
///   - the engine removes every price the side holds and the update does not name (`MREPLACE`);
/// - `BookStreamMode::incremental`: only what changed is sent, as an ordinary update. From whole
///   sides, through `update`, the stream computes the difference against the side it last sent;
///   from changes the application already has - an exchange's diff stream - through `apply`.
///
/// An incremental stream sends a side whole instead of its changes the first time it sends it and
/// after a send that failed, when it does not know what the engine holds; every `Resync` updates or
/// seconds, whichever comes first; and when the whole side is smaller than its changes.
///
/// A side holds at most `max_levels` prices, the engine's depth: `update` refuses a deeper side, and
/// a side `apply` grows deeper is sent whole as its best `max_levels`. A side's times never go back:
/// the engine's history applies updates in time order and its live book in the order they arrive.
/// A refused call (`std::invalid_argument`) sends nothing and changes nothing.
///
/// The shared vectors in `conformance/vectors/books/` hold `book_diff` and what a stream sends to
/// the reference's rules, so every library writing one book sends the same updates.

#include <cstddef>
#include <cstdint>
#include <functional>
#include <memory>
#include <optional>
#include <string_view>
#include <vector>

namespace sde {

/// One side of a book.
enum class BookSide { bid, ask };

/// `"bid"` or `"ask"`.
[[nodiscard]] std::string_view to_string(BookSide side) noexcept;

/// The side a name gives; `std::invalid_argument` for any other name.
[[nodiscard]] BookSide book_side(std::string_view name);

/// One level: price, quantity, order count - the engine's integers.
struct BookLevel {
  std::int64_t price = 0;
  std::int64_t quantity = 0;
  std::int64_t order_count = 0;

  friend bool operator==(const BookLevel&, const BookLevel&) = default;
};

/// A book as of one moment, each side best first: bids from the highest price, asks from the
/// lowest.
struct Book {
  std::vector<BookLevel> bid;
  std::vector<BookLevel> ask;

  friend bool operator==(const Book&, const Book&) = default;
};

/// The orderbook engine's depth: the prices a side holds, and the levels one update carries.
inline constexpr std::size_t ENGINE_DEPTH = 1000;

/// What changes `before` into `after`, one side of a book, as the engine applies an update: first a
/// quantity of 0 for every price of `before` that `after` does not hold, best first; then every
/// price of `after` that `before` does not hold, or holds with another quantity or count, best
/// first. Empty when the sides are the same. Each side lists a price once, none with a quantity of
/// 0, or it is refused.
[[nodiscard]] std::vector<BookLevel> book_diff(BookSide side, const std::vector<BookLevel>& before,
                                               const std::vector<BookLevel>& after);

/// Changes as the application has them - a quantity of 0 a removal - in the order a stream sends
/// them: the removals best first, then the rest best first. Each price once.
[[nodiscard]] std::vector<BookLevel> ordered_changes(BookSide side,
                                                     const std::vector<BookLevel>& changes);

/// When an incremental stream sends a side whole: after `updates` updates of the side since it last
/// did, or `seconds` since then, whichever comes first. `0` turns that clause off.
class Resync {
 public:
  /// Refuses a negative count, and a number of seconds that is negative or not finite.
  explicit Resync(std::int64_t updates = 1000, double seconds = 60.0);

  [[nodiscard]] std::int64_t updates() const noexcept { return updates_; }
  [[nodiscard]] double seconds() const noexcept { return seconds_; }

 private:
  std::int64_t updates_;
  double seconds_;
};

enum class BookStreamMode { full, incremental };

/// `"full"` or `"incremental"`.
[[nodiscard]] std::string_view to_string(BookStreamMode mode) noexcept;

/// The mode a name gives; `std::invalid_argument` for any other name.
[[nodiscard]] BookStreamMode book_stream_mode(std::string_view name);

/// Where a stream's updates go: the orderbook engine, through a session. A send that throws leaves
/// the side unsure, and the stream sends it whole next.
class BookSink {
 public:
  virtual ~BookSink() = default;
  virtual void send_whole(BookSide side, const std::vector<BookLevel>& levels,
                          std::int64_t timestamp_ns) = 0;
  virtual void send_changes(BookSide side, const std::vector<BookLevel>& levels,
                            std::int64_t timestamp_ns) = 0;
};

/// What a stream sent: updates and levels, whole and as changes - what a transfer is made of.
struct StreamStats {
  std::uint64_t updates = 0;    ///< `update` and `apply` calls accepted
  std::uint64_t whole = 0;      ///< sides sent whole
  std::uint64_t changes = 0;    ///< updates of changes sent
  std::uint64_t unchanged = 0;  ///< updates that changed nothing, and were not sent
  std::uint64_t levels_whole = 0;
  std::uint64_t levels_changes = 0;
};

struct BookStreamOptions {
  BookStreamMode mode = BookStreamMode::incremental;
  Resync resync{};
  std::size_t max_levels = ENGINE_DEPTH;
  /// The policy clock, in seconds: `std::chrono::steady_clock` when empty.
  std::function<double()> clock;
  /// This machine's clock, in nanoseconds since the epoch, for an update given no time:
  /// `std::chrono::system_clock` when empty.
  std::function<std::int64_t()> wall_clock_ns;
};

/// One book - one symbol on one exchange - sent whole or as its changes. See the header.
///
/// One call at a time: a call made while another is in progress - from another thread, or from the
/// sink - is refused with `ResourceBusy`, so the engine applies updates in the order the stream
/// computed them.
class BookStream {
 public:
  /// Refuses (`std::invalid_argument`) a `max_levels` of 0, and a sink that is not there.
  explicit BookStream(std::shared_ptr<BookSink> sink, BookStreamOptions options = {});
  ~BookStream();
  BookStream(BookStream&&) noexcept;
  BookStream& operator=(BookStream&&) noexcept;
  BookStream(const BookStream&) = delete;
  BookStream& operator=(const BookStream&) = delete;

  [[nodiscard]] BookStreamMode mode() const noexcept;
  [[nodiscard]] const StreamStats& stats() const noexcept;

  /// The side as the stream holds it, best first: what it last sent, with every change `apply` was
  /// given - deeper than `max_levels` if they made it so.
  [[nodiscard]] std::vector<BookLevel> book(BookSide side) const;

  /// The whole side as it is now: sent whole in `full` mode, its difference in `incremental`.
  /// Without a time, this machine's clock when it is called, never earlier than the side's last.
  void update(BookSide side, const std::vector<BookLevel>& levels,
              std::optional<std::int64_t> timestamp_ns = std::nullopt);

  /// Changes to the side - a quantity of 0 removes a price - applied to the stream's book and sent
  /// as given, removals first; or the side whole, when the header says so. Incremental only.
  void apply(BookSide side, const std::vector<BookLevel>& changes,
             std::optional<std::int64_t> timestamp_ns = std::nullopt);

  /// Sends the side - both, without one - whole now, whatever the policy says. Without a time, the
  /// time of the side's last update, so the side is the book as of it; this machine's clock for a
  /// side the stream has not sent.
  void resync(std::optional<BookSide> side = std::nullopt,
              std::optional<std::int64_t> timestamp_ns = std::nullopt);

 private:
  struct State;
  std::unique_ptr<State> state_;
};

}  // namespace sde
