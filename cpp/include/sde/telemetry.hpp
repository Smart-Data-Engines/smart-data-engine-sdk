#pragma once

/// Telemetry (format contract section 6a, Tier 1): measuring what the application does without
/// ever seeing what it does it to.
///
/// **It carries no values.** A record is keyed by an operation shape, which is the structure of a
/// call and never its arguments; a filter is reported by the names of the fields it named. There is
/// no path by which a row reaches a window.
///
/// **It cannot cost anything.** `Recorder::record` takes no lock: a window is a block of per-shape
/// slots of atomic counters, and closing it swaps the block. The one histogram is exponential, so a
/// percentile read out of it is approximate within one bucket - ample for a decision between
/// microseconds and milliseconds, and fixed memory where exact samples would not be.
///
/// **It cannot fail the caller.** Recording never throws and never blocks; what it cannot record it
/// drops and counts.

#include <array>
#include <atomic>
#include <cstddef>
#include <cstdint>
#include <deque>
#include <functional>
#include <map>
#include <memory>
#include <mutex>
#include <optional>
#include <set>
#include <string>
#include <string_view>
#include <unordered_map>
#include <vector>

#include "sde/json.hpp"
#include "sde/model.hpp"

namespace sde {

/// 1 us to about 17 s, doubling: enough to tell a cache hit from a full scan.
inline constexpr int BUCKET_COUNT = 25;
inline constexpr std::int64_t BUCKET_BASE_NS = 1000;
/// The bucket a write's rows are counted in for `write_burstiness`.
inline constexpr std::int64_t SECOND_NS = 1'000'000'000;
/// The shortest span a daily growth is projected from: an hour, so a day is a measurement times 24
/// rather than times thousands.
inline constexpr std::int64_t GROWTH_MIN_NS = 3'600'000'000'000;
/// The unit growth is projected to, and how long the recorder keeps a group's storage samples.
inline constexpr std::int64_t DAY_NS = 86'400'000'000'000;

/// Exponential-bucket histogram: fixed memory, O(1) record, approximate percentiles.
struct Histogram {
  std::array<std::uint64_t, BUCKET_COUNT> buckets{};
  std::uint64_t count = 0;
  std::uint64_t total = 0;

  /// The bucket a duration lands in: 0 below one microsecond, else the bit length of the whole
  /// microseconds, capped at the last. Integer arithmetic only - a logarithm is not required to be
  /// correctly rounded, and one bit at a power of two is a different bucket (`telemetry/002`).
  [[nodiscard]] static int bucket_of(std::int64_t nanoseconds) noexcept;
  /// The upper edge of a bucket in milliseconds: `1000 * 2^bucket` ns over a million.
  [[nodiscard]] static double upper_edge_ms(int bucket) noexcept;

  void record(std::int64_t nanoseconds) noexcept;
  /// The upper edge of the bucket the percentile falls in - rounding up, because a placement made
  /// on an optimistic latency is the wrong kind of wrong. Empty when nothing was recorded.
  [[nodiscard]] std::optional<double> percentile_ms(double fraction) const noexcept;
  void merge(const Histogram& other) noexcept;
};

/// What filtered calls named: the fields compared by equality, sorted and distinct, and the field a
/// range bounded (empty for none). Names only.
struct Predicates {
  std::vector<std::string> equal;
  std::string range;

  friend auto operator<=>(const Predicates&, const Predicates&) = default;
  friend bool operator==(const Predicates&, const Predicates&) = default;
};

/// What one call that takes a `where` filtered on: absent from a call that does not filter at all.
struct Filter {
  std::vector<std::string> equal;
  std::optional<std::string> range;
};

/// What was observed for one operation shape in one window.
struct ShapeStats {
  std::string shape_id;
  std::string group;
  std::string entity;
  std::string kind;
  std::uint64_t calls = 0;
  std::uint64_t rows = 0;
  std::uint64_t errors = 0;
  Histogram latency;
  std::map<Predicates, std::uint64_t> filtered;
};

/// What the fan-out to one derived copy did. Not a shape: a fan-out is the library keeping a copy
/// current, and counting it as a write would make a group with a copy look twice as write-heavy.
struct FanOutStats {
  std::string group;
  std::string materialization;
  std::uint64_t writes = 0;
  std::uint64_t failures = 0;
  Histogram latency;
};

/// How far behind one derived copy ran: late by at most one write, or the row is absent.
struct CopyFreshness {
  std::string group;
  std::string materialization;
  std::uint64_t writes = 0;
  std::uint64_t failures = 0;
  std::optional<double> lag_p50_ms;
  std::optional<double> lag_p99_ms;

  [[nodiscard]] bool complete() const noexcept { return failures == 0; }
  [[nodiscard]] Json as_record() const;
};

/// One group's size on its source materialisation at one moment, from the engine's catalogue.
struct StorageSample {
  std::string group;
  std::int64_t at_ns = 0;
  std::int64_t total_bytes = 0;
  std::int64_t secondary_index_bytes = 0;
};

/// The contract between telemetry and the planner. An empty optional is *unknown*, which is not
/// zero, and every unknown field is named in `missing` too.
struct GroupFeatures {
  std::uint64_t calls = 0;
  std::optional<double> read_write_ratio;
  std::map<std::string, double> shape_mix;
  std::optional<double> latency_p50_ms;
  std::optional<double> latency_p99_ms;
  std::optional<double> result_cardinality_p50;
  std::optional<double> result_cardinality_p99;
  std::optional<std::int64_t> total_bytes;
  std::optional<std::int64_t> daily_growth_bytes;
  std::optional<double> index_to_table_ratio;
  std::optional<double> pk_access_share;
  bool has_time_dimension = false;
  std::optional<double> time_filtered_share;
  std::uint64_t distinct_shapes = 0;
  std::optional<double> write_burstiness;
  std::optional<double> error_share;
  std::set<std::string> missing;
  bool complete = true;

  /// The feature vector as it crosses to the control plane: a field with no value is omitted, and
  /// `missing` says so.
  [[nodiscard]] Json as_record() const;
};

/// Every measured field of the feature vector, in the reference's order. `missing` is derived from
/// this list and the values, so the two cannot disagree.
inline constexpr std::string_view MEASURED_FIELDS[] = {
    "calls",          "read_write_ratio",       "shape_mix",
    "latency_p50_ms", "latency_p99_ms",         "result_cardinality_p50",
    "result_cardinality_p99", "total_bytes",    "daily_growth_bytes",
    "index_to_table_ratio",   "pk_access_share", "has_time_dimension",
    "time_filtered_share",    "distinct_shapes", "write_burstiness",
    "error_share"};

/// One aggregation period, ready to send. `complete` is false when part of it was not recorded or
/// the buffer dropped windows: still sent, so the planner knows traffic existed, never used to
/// justify a migration.
struct Window {
  std::string model_version;
  std::int64_t started_ns = 0;
  std::int64_t ended_ns = 0;
  std::vector<ShapeStats> shapes;
  bool complete = true;
  std::uint64_t dropped_windows = 0;
  std::vector<FanOutStats> fanned;
  /// Samples taken in this window, and every sample kept when it closed (up to a day old).
  std::vector<StorageSample> storage;
  std::vector<StorageSample> storage_history;
  /// Rows written by successful writes, per group, per whole second of the window.
  std::map<std::string, std::map<std::int64_t, std::uint64_t>> write_seconds;

  /// How far behind each of the group's derived copies ran, sorted by materialisation.
  [[nodiscard]] std::vector<CopyFreshness> copies(std::string_view group) const;

  /// One group's records folded into the planner's feature vector. `time_fields` (entity to its
  /// fields of a time type) is what `time_filtered_share` counts against, and without it the share
  /// stays unknown; `range_fields` maps a range read's shape id to the field it ranges over.
  [[nodiscard]] GroupFeatures features(
      std::string_view group, bool has_time_dimension = false,
      const std::map<std::string, std::set<std::string>>* time_fields = nullptr,
      const std::map<std::string, std::string>* range_fields = nullptr) const;

  /// What each shape of one group measured, in the model's enumeration order.
  [[nodiscard]] Json shape_records(const Model& model, std::string_view group) const;

  /// This window as the document the control plane reads (section 6a). Refuses
  /// (`std::invalid_argument`) a model other than the one measured: one fact is read from it, and
  /// read from another model it would be attached to the wrong groups.
  [[nodiscard]] Json as_record(const Model& model) const;
};

/// Whether any entity of the group has a field of a time type - by type, never by name.
[[nodiscard]] bool has_time_dimension(const Model& model, const Group& group);
/// Each entity of the group and its fields of a time type.
[[nodiscard]] std::map<std::string, std::set<std::string>> time_fields(const Model& model,
                                                                       const Group& group);

/// Accumulates records and rolls windows, and drops telemetry rather than anything else.
///
/// Shared between threads. `record` and `record_fan_out` take no lock: a window is a block of slots
/// of atomic counters, one per shape of the model, and `roll` swaps the current block for a spare
/// one, waits until no write begun on the old block is still running, and reads it. A write that
/// raced the roll lands in the new window rather than being lost or torn. `record_storage`,
/// `roll`, `pending`, `acknowledge` take a mutex: they are not on an operation's path.
class Recorder {
 public:
  /// Nanoseconds from a monotonic clock; a test passes its own.
  using Clock = std::function<std::int64_t()>;

  explicit Recorder(const Model& model, std::size_t max_windows = 64, Clock clock = {});
  ~Recorder();
  Recorder(const Recorder&) = delete;
  Recorder& operator=(const Recorder&) = delete;

  /// Records one operation of a shape of this recorder's model. A shape of any other model is
  /// dropped and counted (`rejected`). `filter` is for an operation that takes a `where`: absent
  /// for one that does not, and then nothing about filters is recorded.
  void record(const OperationShape& shape, std::int64_t nanoseconds, std::uint64_t rows = 0,
              bool failed = false, const Filter* filter = nullptr) noexcept;
  /// Records one write to one derived copy.
  void record_fan_out(std::string_view group, std::string_view materialization,
                      std::int64_t nanoseconds, bool failed = false) noexcept;
  /// Records one group's size as the engine's catalogue gave it. A sample that is not two
  /// non-negative integers with the index part inside the total is dropped, never guessed.
  void record_storage(std::string_view group, std::int64_t total_bytes,
                      std::int64_t secondary_index_bytes) noexcept;

  /// Closes the current period and queues it; empty when nothing was recorded in it.
  std::optional<Window> roll();
  [[nodiscard]] std::vector<Window> pending() const;
  /// Part of the current period was not recorded: the window says so, and is not scored as whole.
  void mark_incomplete() noexcept;
  /// Drops the oldest `count` windows once the application has taken them.
  void acknowledge(std::size_t count);

  [[nodiscard]] const std::string& model_version() const noexcept { return model_version_; }
  /// Records dropped because their shape is not one of this model's, or a sample was malformed.
  [[nodiscard]] std::uint64_t rejected() const noexcept { return rejected_.load(); }

 private:
  struct Block;
  struct ShapeInfo {
    std::string id;
    std::string group;
    std::string entity;
    std::string kind;
    std::size_t group_index;
    bool write;
  };

  Block* enter() noexcept;
  [[nodiscard]] std::int64_t now() const;

  std::string model_version_;
  std::vector<ShapeInfo> shapes_;
  std::unordered_map<std::string, std::size_t> shape_index_;
  std::vector<std::string> groups_;
  Clock clock_;
  std::size_t max_windows_;

  std::array<std::unique_ptr<Block>, 2> blocks_;
  std::atomic<Block*> current_{nullptr};
  std::atomic<bool> incomplete_{false};
  std::atomic<std::uint64_t> rejected_{0};

  mutable std::mutex mutex_;
  std::deque<Window> windows_;
  std::uint64_t dropped_ = 0;
  std::map<std::string, std::vector<StorageSample>> storage_;
  std::vector<StorageSample> storage_window_;
};

}  // namespace sde
