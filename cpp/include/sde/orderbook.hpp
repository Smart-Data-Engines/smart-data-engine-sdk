#pragma once

/// The orderbook engine adapter, dialect `orderbook`, over the engine's own text protocol on TCP or
/// TLS 1.3. This library speaks the protocol itself and never links the engine's library: it
/// links as `sde::orderbook`, over OpenSSL, so an application that places nothing on this engine
/// carries neither. Like the other adapters it executes decisions and makes none.
///
/// This engine stores L2 depth in one shape fixed in its own source (`ORDERBOOK_SHAPE`), so a group
/// either is that shape or cannot be placed here. Each difference from a general-purpose store is
/// stated rather than smoothed over, as the reference states it:
///
/// - **No transactions.** `transaction` refuses before the body runs: a callback run without one
///   would turn a declared atomicity into a comment.
/// - **No key enforcement.** Two writes with one key both persist (measured), so `get` and a page
///   refuse two rows with one key rather than answer with either.
/// - **Writes are updates of N levels.** `level` is a price's position within one update, so
///   `insert` stores level 0 or refuses, and `insert_many` - `Session::save_many` - writes whole
///   updates: the rows of one side of one book at one instant, levels 0 to n-1.
/// - **The sequence number is the server's.** It numbers every update per book and refuses one the
///   client chose, so a row carries `sequence_number` as null and reads the server's back.
/// - **The engine answers in arrival order.** Its `LIMIT` keeps the first rows to arrive, so a page
///   is assembled here from windows of time, each read whole and sorted.
/// - **It counts nothing over its history.** Its aggregates read the live book, so
///   `Session::count` and `Session::summarize` are refused by name.
///
/// A write is invisible to a read until `flush`, and every read flushes first when something was
/// written. An exchange that does not finish closes the connection, and every later call says why
/// until `connect` opens another. One thread uses an engine at a time: a second is refused
/// (`ResourceBusy`), and after a `fork` the engine refuses everything (`ResourceClosed`).

#include <atomic>
#include <cstdint>
#include <functional>
#include <memory>
#include <optional>
#include <string>
#include <string_view>
#include <thread>
#include <vector>

#include "sde/engine.hpp"
#include "sde/log.hpp"

namespace sde {

namespace detail::orderbook {
class Connection;
struct Target;
/// A scan's two bounds: the most rows one query may bring into this process, and the first window
/// of time it reads.
struct ScanLimits {
  std::size_t chunk_rows = 100000;
  std::uint64_t first_window_ns = 1000000000;
};
/// The test suite's way to lower the bounds, as the reference's tests patch its module constants.
struct Testing;
}  // namespace detail::orderbook

struct OrderbookOptions {
  /// The events of the closed vocabulary this adapter writes (`sde.schema.applied`,
  /// `sde.write.failed`, `sde.orderbook.flushed`); none of them carries a value of the client's.
  LogSink log;
};

/// One level of an update, from the top of the book down: its price, quantity and order count.
struct OrderbookLevel {
  std::int64_t price = 0;
  std::int64_t quantity = 0;
  std::int64_t order_count = 0;
};

class OrderbookEngine final : public Engine,
                              public Queryable,
                              public BulkWritable,
                              public SchemaValidator {
 public:
  /// The deepest level an update may have: the engine stores at most 1000 levels per side.
  static constexpr std::int64_t MAX_LEVEL = 999;
  /// Updates per pipelined round trip: the engine's rate stopped improving past it.
  static constexpr std::size_t BATCH_UPDATES = 64;

  /// Parses `orderbook://[identity:secret@]host:port[?tls=on&ca=PATH&verify=off&timeout=S]` and
  /// refuses (`EngineError`) one that breaks a rule, in the reference's words, never repeating the
  /// DSN, which carries the secret. Nothing is opened until `connect`.
  explicit OrderbookEngine(std::string_view dsn, OrderbookOptions options = {});
  /// Closes, saying `QUIT`.
  ~OrderbookEngine() override;
  OrderbookEngine(const OrderbookEngine&) = delete;
  OrderbookEngine& operator=(const OrderbookEngine&) = delete;

  /// Connects, completes TLS, answers the server's challenge and checks that the server stores a
  /// write's event time, refusing one that cannot. A connection an exchange left out of step is
  /// replaced; an open one is kept.
  void connect();
  void close() noexcept;
  [[nodiscard]] bool connected() const noexcept;
  /// `OrderbookEngine(tcp host:port, identity='desk', tls=on)`: never the secret.
  [[nodiscard]] std::string describe() const;

  /// Makes everything written so far queryable. Reads do it themselves when something was written;
  /// it is here because the cost is real and a client ingesting a feed may want to choose when.
  void flush();
  /// One update: `levels` from the top of the book down, of one side of one book at one instant -
  /// a level is its position here.
  void insert_levels(const std::string& table, const std::string& symbol,
                     const std::string& exchange, const std::string& side,
                     std::int64_t timestamp_ns, const std::vector<OrderbookLevel>& levels);
  /// Every stored level of one book, within an inclusive range of time if one is given, in the
  /// order the updates arrived - not their event time; `Session::scan` answers in key order. A book
  /// nothing was written to is empty. Without `limit`, more than 100,000 rows are refused.
  [[nodiscard]] std::vector<Row> levels(const std::string& symbol, const std::string& exchange,
                                        std::optional<std::uint64_t> start_ns = std::nullopt,
                                        std::optional<std::uint64_t> end_ns = std::nullopt,
                                        std::optional<std::int64_t> limit = std::nullopt);

  // Engine
  [[nodiscard]] std::string_view dialect() const noexcept override { return "orderbook"; }
  /// Checks the layout against the engine's fixed shape and key, and creates nothing: the storage
  /// exists once the engine opens its data directory. No findings: there is no design to differ.
  std::vector<PhysicalFinding> ensure_schema(const PhysicalLayout& layout,
                                             const Keys& keys) override;
  /// One row, at level 0, or a refusal: a price's level is its position within an update.
  void insert(const std::string& table, const Row& values) override;
  /// The row with this key; nothing if there is none, a refusal if there are two.
  std::optional<Row> get(const std::string& table, const Row& key) override;
  /// Refuses (`EngineError`) before `body` runs: this engine has no multi-statement transactions.
  void transaction(const std::function<void()>& body) override;
  [[nodiscard]] Capabilities capabilities() noexcept override;

  // Queryable: one page of one book in key order.
  std::vector<Row> select_rows(const std::string& table, const ReadPlan& plan) override;

  // BulkWritable: rows as the engine's updates, pipelined 64 at a time. Not a transaction: the
  // server may refuse some updates and store others, and the refusal names both.
  void insert_many(const std::string& table, const std::vector<Row>& rows) override;

  // SchemaValidator: the same check as `ensure_schema`.
  std::vector<PhysicalFinding> validate_schema(const PhysicalLayout& layout,
                                               const Keys& keys) override;

  /// Why `Session::count` is refused for a group on this engine.
  static constexpr std::string_view COUNT_REFUSAL =
      "this engine counts nothing over its history: its aggregates read the live book, not the "
      "stored rows, and its query language refuses a time filter on an aggregate. Refused rather "
      "than computed from a full scan, which is what a count here would have to be.";
  /// Why `Session::summarize` is refused for a group on this engine.
  static constexpr std::string_view SUMMARY_REFUSAL =
      "this engine summarizes nothing over its history: its aggregates read the live book, not "
      "the stored rows. Refused rather than computed from a full scan.";

 private:
  friend struct detail::orderbook::Testing;
  class Use;
  struct Update;
  struct Scan;

  /// The connection, ready to send on; or a refusal that says why there is none.
  detail::orderbook::Connection& ready();
  void check_layout(const PhysicalLayout& layout, const Keys& keys) const;
  void flush_now();
  /// The rows of one query, as the engine's client reads them; an unknown book is an empty one.
  std::vector<std::vector<std::string>> query(const std::string& statement);
  std::vector<Update> updates(const std::vector<Row>& rows) const;
  void write_failed(const std::string& table, const std::string& error) const;
  std::vector<Row> book(const std::string& symbol, const std::string& exchange,
                        std::optional<std::uint64_t> start_ns, std::optional<std::uint64_t> end_ns,
                        std::optional<std::int64_t> limit);

  std::unique_ptr<detail::orderbook::Target> target_;
  OrderbookOptions options_;
  std::unique_ptr<detail::orderbook::Connection> connection_;
  std::int64_t unflushed_ = 0;
  detail::orderbook::ScanLimits limits_;
  std::atomic<std::thread::id> owner_{};
  int depth_ = 0;
  long process_ = 0;
};

}  // namespace sde
