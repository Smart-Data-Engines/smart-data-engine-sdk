#pragma once

/// The ClickHouse adapter, dialect `clickhouse`, over the server's HTTP interface through libcurl.
/// It links as `sde::clickhouse`, so an application that places nothing in ClickHouse carries no
/// libcurl. Like the PostgreSQL one it executes decisions and makes none.
///
/// Three things differ from PostgreSQL, and each is stated rather than smoothed over, as the
/// reference states them. **There are no transactions**: `transaction` refuses before the body
/// runs, because a context that silently did nothing would turn a declared atomicity into a
/// comment. **Keys are not enforced**: a table is a `ReplacingMergeTree` and every read that must
/// see one row per key reads `FINAL`, so a second save of a key is an overwrite here where it is an
/// error there. **A moment is UTC on the wire, always**: every exchange runs in a UTC session and
/// reads times back in ISO form with their `Z`, whatever the server's or a profile's time zone.
///
/// An exchange is one HTTP POST on a connection of its own, never reused and so never resent: a
/// failed exchange is reported with its outcome unknown and nothing is retried. Opening is bounded
/// by the URI's `connect_timeout` (10 s), and every exchange by `send_receive_timeout` (15 s) of
/// silence - a running query keeps the connection alive with progress headers.
///
/// One thread uses an engine at a time: a second is refused (`ResourceBusy`), and after a `fork`
/// the engine refuses everything (`ResourceClosed`).

#include <atomic>
#include <cstdint>
#include <functional>
#include <map>
#include <memory>
#include <optional>
#include <string>
#include <string_view>
#include <thread>
#include <utility>
#include <vector>

#include "sde/engine.hpp"
#include "sde/log.hpp"
#include "sde/write_fence.hpp"

namespace sde {

namespace detail::clickhouse {
class Http;
class Fences;
struct Target;
}  // namespace detail::clickhouse

struct ClickHouseOptions {
  /// The events of the closed vocabulary this adapter writes (`sde.schema.applied`,
  /// `sde.schema.extra_columns`, `sde.write.failed`); none of them carries a value of the client's.
  LogSink log;
};

class ClickHouseEngine final : public Engine,
                               public Queryable,
                               public Countable,
                               public Summarizable,
                               public BulkWritable,
                               public WatermarkStore,
                               public Migratable,
                               public Fencable,
                               public SchemaValidator,
                               public StorageMeasurable {
 public:
  /// Parses the URI - `clickhouse`, `clickhouses`, `http` or `https`, one database segment, and the
  /// options `secure`, `verify`, `ca_cert`, `connect_timeout` and `send_receive_timeout` - and
  /// refuses (`EngineError`) one that breaks a rule, naming the rule and never a value. Nothing is
  /// opened until `connect`.
  explicit ClickHouseEngine(std::string_view dsn, ClickHouseOptions options = {});
  ~ClickHouseEngine() override;
  ClickHouseEngine(const ClickHouseEngine&) = delete;
  ClickHouseEngine& operator=(const ClickHouseEngine&) = delete;

  /// Reads the CA the URI names, once, and asks the server its version, bounded by the URI's
  /// timeouts. Its later exchanges trust exactly those CA bytes.
  void connect();
  void close() noexcept;
  [[nodiscard]] bool connected() const noexcept;
  /// What the server said it is; empty before `connect`.
  [[nodiscard]] const std::string& server_version() const noexcept { return version_; }

  // Engine
  [[nodiscard]] std::string_view dialect() const noexcept override { return "clickhouse"; }
  /// Creates what is missing and changes nothing that exists; returns how sort keys, partitions and
  /// data-skipping indexes differ from the design. Refuses a table missing a column or holding one
  /// as another type.
  std::vector<PhysicalFinding> ensure_schema(const PhysicalLayout& layout,
                                             const Keys& keys) override;
  void insert(const std::string& table, const Row& values) override;
  std::optional<Row> get(const std::string& table, const Row& key) override;
  /// Refuses (`EngineError`) before `body` runs: ClickHouse has no multi-statement transactions.
  void transaction(const std::function<void()>& body) override;
  [[nodiscard]] Capabilities capabilities() noexcept override;

  // Queryable, Countable, Summarizable
  std::vector<Row> select_rows(const std::string& table, const ReadPlan& plan) override;
  std::uint64_t count_rows(const std::string& table, const ReadPlan& plan) override;
  SummaryRecord summarize_rows(const std::string& table, const ReadPlan& plan,
                               const ReadColumn& column) override;

  // BulkWritable
  void insert_many(const std::string& table, const std::vector<Row>& rows) override;

  // WatermarkStore
  std::optional<std::int64_t> map_watermark() override;
  void record_map_version(std::int64_t version, const std::string& model_version) override;

  // Migratable
  std::vector<Row> key_range(const std::string& table, const std::vector<std::string>& order,
                             const std::optional<std::vector<Value>>& after,
                             const std::optional<std::vector<Value>>& upto,
                             std::optional<std::size_t> limit) override;
  std::optional<std::vector<Value>> nth_key(const std::string& table,
                                            const std::vector<std::string>& order,
                                            std::int64_t position) override;
  void copy_in(const std::string& table, const std::vector<Row>& rows) override;
  std::uint64_t count(const std::string& table) override;
  std::int64_t backfill_marker(const std::string& materialization,
                               const std::string& entity) override;
  void record_backfill_marker(const std::string& materialization, const std::string& entity,
                              std::int64_t rows) override;

  // Fencable: DDL capability - give it a connection of its own, apart from application traffic.
  WriteFence write_fence(const std::string& table, const std::string& project_id) override;

  // SchemaValidator
  std::vector<PhysicalFinding> validate_schema(const PhysicalLayout& layout,
                                               const Keys& keys) override;

  // StorageMeasurable: needs SELECT on the system.parts columns this adapter reads; without the
  // grant a size is refused, and a session reports it as unknown.
  std::map<std::string, std::pair<std::int64_t, std::int64_t>> storage_sizes(
      const std::vector<std::string>& tables) override;

 private:
  friend class detail::clickhouse::Fences;
  class Use;

  /// An answer's columns in the server's order, and its rows in that order.
  struct Answer {
    std::vector<std::string> names;
    std::vector<std::vector<Value>> rows;
  };

  /// The open transport, or a refusal to use one.
  const detail::clickhouse::Http& http() const;
  /// One statement whose answer is rows, each cell read by its column's type; `settings` are more
  /// URL parameters (`&join_use_nulls=0`).
  Answer answer(const std::string& sql, std::string_view settings = {}) const;
  /// The same rows as maps from column to value.
  std::vector<Row> query(const std::string& sql) const;
  void command(const std::string& sql) const;
  /// One INSERT of rows with these columns, as `FORMAT JSONEachRow`.
  void insert_rows(const std::string& table, const std::vector<std::string>& columns,
                   const std::vector<Row>& rows, std::string_view settings = {}) const;
  void verify_schema(const PhysicalLayout& layout);
  std::vector<PhysicalFinding> physical_findings(const PhysicalLayout& layout, const Keys& keys);
  void write_failed(const std::string& table, const std::exception& error) const;

  std::unique_ptr<detail::clickhouse::Target> target_;
  ClickHouseOptions options_;
  std::unique_ptr<detail::clickhouse::Http> http_;
  std::unique_ptr<detail::clickhouse::Fences> fences_;
  std::string version_;
  std::atomic<std::thread::id> owner_{};
  int depth_ = 0;
  long process_ = 0;
};

}  // namespace sde
