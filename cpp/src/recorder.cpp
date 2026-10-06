#include <algorithm>
#include <chrono>
#include <set>
#include <thread>

#include "sde/telemetry.hpp"

namespace sde {

namespace {

/// A histogram of atomic counters: written by any thread without a lock, read when no writer is
/// left on its block.
struct AtomicHistogram {
  std::array<std::atomic<std::uint64_t>, BUCKET_COUNT> buckets{};
  std::atomic<std::uint64_t> count{0};
  std::atomic<std::uint64_t> total{0};

  void record(std::int64_t nanoseconds) noexcept {
    count.fetch_add(1, std::memory_order_relaxed);
    total.fetch_add(static_cast<std::uint64_t>(std::max<std::int64_t>(nanoseconds, 0)),
                    std::memory_order_relaxed);
    buckets[static_cast<std::size_t>(Histogram::bucket_of(nanoseconds))].fetch_add(
        1, std::memory_order_relaxed);
  }

  [[nodiscard]] Histogram snapshot() const noexcept {
    Histogram out;
    for (std::size_t i = 0; i < buckets.size(); ++i) {
      out.buckets[i] = buckets[i].load(std::memory_order_relaxed);
    }
    out.count = count.load(std::memory_order_relaxed);
    out.total = total.load(std::memory_order_relaxed);
    return out;
  }

  void reset() noexcept {
    for (auto& bucket : buckets) bucket.store(0, std::memory_order_relaxed);
    count.store(0, std::memory_order_relaxed);
    total.store(0, std::memory_order_relaxed);
  }
};

struct FilterNode {
  Predicates key;
  std::atomic<std::uint64_t> calls{0};
  FilterNode* next = nullptr;
};

struct FanNode {
  std::string group;
  std::string materialization;
  std::atomic<std::uint64_t> writes{0};
  std::atomic<std::uint64_t> failures{0};
  AtomicHistogram latency;
  FanNode* next = nullptr;
};

struct SecondNode {
  std::int64_t second = 0;
  std::atomic<std::uint64_t> rows{0};
  SecondNode* next = nullptr;
};

/// Find the node `match` accepts in an append-only list, or put the one `make` builds at its head.
/// Lock-free: a lost race rescans and either finds the winner's node or tries again.
template <class Node, class Match, class Make>
Node* find_or_insert(std::atomic<Node*>& head, Match match, Make make) {
  Node* first = head.load(std::memory_order_acquire);
  for (Node* node = first; node != nullptr; node = node->next) {
    if (match(*node)) return node;
  }
  std::unique_ptr<Node> fresh = make();
  for (;;) {
    fresh->next = first;
    if (head.compare_exchange_weak(first, fresh.get(), std::memory_order_acq_rel,
                                   std::memory_order_acquire)) {
      return fresh.release();
    }
    for (Node* node = first; node != nullptr; node = node->next) {
      if (match(*node)) return node;
    }
  }
}

template <class Node>
void free_list(std::atomic<Node*>& head) noexcept {
  Node* node = head.exchange(nullptr, std::memory_order_acq_rel);
  while (node != nullptr) {
    Node* next = node->next;
    delete node;
    node = next;
  }
}

std::int64_t steady_now() {
  return std::chrono::duration_cast<std::chrono::nanoseconds>(
             std::chrono::steady_clock::now().time_since_epoch())
      .count();
}

}  // namespace

struct Recorder::Block {
  struct Slot {
    std::atomic<std::uint64_t> calls{0};
    std::atomic<std::uint64_t> rows{0};
    std::atomic<std::uint64_t> errors{0};
    AtomicHistogram latency;
    std::atomic<FilterNode*> filters{nullptr};
  };

  Block(std::size_t shapes, std::size_t groups)
      : slots(std::make_unique<Slot[]>(shapes)),
        seconds(std::make_unique<std::atomic<SecondNode*>[]>(groups)),
        shape_count(shapes),
        group_count(groups) {
    for (std::size_t i = 0; i < groups; ++i) seconds[i].store(nullptr);
  }
  ~Block() { reset(); }
  Block(const Block&) = delete;
  Block& operator=(const Block&) = delete;

  /// Writers inside this block right now. A roll waits for zero before reading it.
  std::atomic<std::int64_t> writers{0};
  /// Whether anything was recorded since the block became current.
  std::atomic<bool> touched{false};
  std::atomic<std::int64_t> started_ns{0};
  std::unique_ptr<Slot[]> slots;
  std::unique_ptr<std::atomic<SecondNode*>[]> seconds;  // per group of the model
  std::atomic<FanNode*> fanned{nullptr};
  std::size_t shape_count;
  std::size_t group_count;

  /// Only with no writer inside: after the roll that swapped it out has waited for them.
  void reset() noexcept {
    for (std::size_t i = 0; i < shape_count; ++i) {
      Slot& slot = slots[i];
      slot.calls.store(0, std::memory_order_relaxed);
      slot.rows.store(0, std::memory_order_relaxed);
      slot.errors.store(0, std::memory_order_relaxed);
      slot.latency.reset();
      free_list(slot.filters);
    }
    for (std::size_t i = 0; i < group_count; ++i) free_list(seconds[i]);
    free_list(fanned);
    touched.store(false, std::memory_order_relaxed);
  }
};

namespace {

/// Leaves the block a write entered, whatever the write did.
class Inside {
 public:
  explicit Inside(std::atomic<std::int64_t>& writers) noexcept : writers_(writers) {}
  ~Inside() { writers_.fetch_sub(1, std::memory_order_release); }
  Inside(const Inside&) = delete;
  Inside& operator=(const Inside&) = delete;

 private:
  std::atomic<std::int64_t>& writers_;
};

}  // namespace

Recorder::Recorder(const Model& model, std::size_t max_windows, Clock clock)
    : model_version_(model.version()), clock_(std::move(clock)), max_windows_(max_windows) {
  if (!clock_) clock_ = steady_now;
  for (const Group& group : model.groups()) groups_.push_back(group.name);
  for (const OperationShape& shape : model.shapes()) {
    const auto group = std::find(groups_.begin(), groups_.end(), shape.group);
    shape_index_.emplace(shape.id, shapes_.size());
    shapes_.push_back(ShapeInfo{shape.id, shape.group, shape.entity, shape.kind,
                                static_cast<std::size_t>(group - groups_.begin()),
                                is_write_kind(shape.kind)});
  }
  for (auto& block : blocks_) block = std::make_unique<Block>(shapes_.size(), groups_.size());
  blocks_[0]->started_ns.store(now());
  current_.store(blocks_[0].get());
}

Recorder::~Recorder() = default;

std::int64_t Recorder::now() const { return clock_(); }

Recorder::Block* Recorder::enter() noexcept {
  // Count ourselves in, then check the block is still the current one: a roll swaps the pointer
  // and then waits for the count to reach zero, so either it sees us and waits, or we see the swap
  // and move to the new block. Both are sequentially consistent, which is what makes "either"
  // exhaustive. Blocks live as long as the recorder, so a stale pointer is never freed memory.
  //
  // Recorded as unmutatable rather than left to look like coverage: dropping the re-check survives
  // every test. The write it misplaces needs a roll between the first load and the increment, and
  // is lost only if no later roll reads the spare block - a window a few instructions wide, which
  // the threaded test crosses without hitting. The argument above is what holds it.
  for (;;) {
    Block* block = current_.load(std::memory_order_seq_cst);
    block->writers.fetch_add(1, std::memory_order_seq_cst);
    if (current_.load(std::memory_order_seq_cst) == block) return block;
    block->writers.fetch_sub(1, std::memory_order_release);
  }
}

void Recorder::record(const OperationShape& shape, std::int64_t nanoseconds, std::uint64_t rows,
                      bool failed, const Filter* filter) noexcept {
  try {
    const auto found = shape_index_.find(shape.id);
    if (found == shape_index_.end()) {
      rejected_.fetch_add(1, std::memory_order_relaxed);
      return;
    }
    const ShapeInfo& info = shapes_[found->second];
    std::optional<Predicates> predicates;
    if (filter != nullptr) {
      std::set<std::string> equal(filter->equal.begin(), filter->equal.end());
      predicates = Predicates{{equal.begin(), equal.end()}, filter->range.value_or("")};
    }
    // A write's second is read before entering the block: the clock is the caller's code.
    const bool counts_rows = rows > 0 && !failed && info.write;
    const std::int64_t at = counts_rows ? now() : 0;

    Block* block = enter();
    const Inside inside(block->writers);
    Block::Slot& slot = block->slots[found->second];
    slot.calls.fetch_add(1, std::memory_order_relaxed);
    slot.rows.fetch_add(rows, std::memory_order_relaxed);
    if (failed) slot.errors.fetch_add(1, std::memory_order_relaxed);
    slot.latency.record(nanoseconds);
    if (predicates) {
      FilterNode* node = find_or_insert(
          slot.filters, [&](const FilterNode& candidate) { return candidate.key == *predicates; },
          [&] {
            auto fresh = std::make_unique<FilterNode>();
            fresh->key = *predicates;
            return fresh;
          });
      node->calls.fetch_add(1, std::memory_order_relaxed);
    }
    if (counts_rows) {
      const std::int64_t second =
          std::max<std::int64_t>(0, (at - block->started_ns.load(std::memory_order_relaxed)) /
                                        SECOND_NS);
      SecondNode* node = find_or_insert(
          block->seconds[info.group_index],
          [&](const SecondNode& candidate) { return candidate.second == second; },
          [&] {
            auto fresh = std::make_unique<SecondNode>();
            fresh->second = second;
            return fresh;
          });
      node->rows.fetch_add(rows, std::memory_order_relaxed);
    }
    block->touched.store(true, std::memory_order_release);
  } catch (...) {
    // An allocation that failed, or a clock that threw: telemetry is what gets lost, never the
    // caller's operation.
    rejected_.fetch_add(1, std::memory_order_relaxed);
  }
}

void Recorder::record_fan_out(std::string_view group, std::string_view materialization,
                              std::int64_t nanoseconds, bool failed) noexcept {
  try {
    Block* block = enter();
    const Inside inside(block->writers);
    FanNode* node = find_or_insert(
        block->fanned,
        [&](const FanNode& candidate) {
          return candidate.group == group && candidate.materialization == materialization;
        },
        [&] {
          auto fresh = std::make_unique<FanNode>();
          fresh->group = std::string(group);
          fresh->materialization = std::string(materialization);
          return fresh;
        });
    node->writes.fetch_add(1, std::memory_order_relaxed);
    if (failed) node->failures.fetch_add(1, std::memory_order_relaxed);
    // Recorded either way: a failed fan-out took time too.
    node->latency.record(nanoseconds);
    block->touched.store(true, std::memory_order_release);
  } catch (...) {
    rejected_.fetch_add(1, std::memory_order_relaxed);
  }
}

void Recorder::record_storage(std::string_view group, std::int64_t total_bytes,
                              std::int64_t secondary_index_bytes) noexcept {
  try {
    if (group.empty() || total_bytes < 0 || secondary_index_bytes < 0 ||
        secondary_index_bytes > total_bytes) {
      rejected_.fetch_add(1, std::memory_order_relaxed);
      return;
    }
    const std::lock_guard<std::mutex> lock(mutex_);
    const StorageSample sample{std::string(group), now(), total_bytes, secondary_index_bytes};
    auto& kept = storage_[sample.group];
    kept.erase(std::remove_if(kept.begin(), kept.end(),
                              [&](const StorageSample& old) {
                                return old.at_ns < sample.at_ns - DAY_NS;
                              }),
               kept.end());
    kept.push_back(sample);
    storage_window_.push_back(sample);
  } catch (...) {
    rejected_.fetch_add(1, std::memory_order_relaxed);
  }
}

std::optional<Window> Recorder::roll() {
  const std::lock_guard<std::mutex> lock(mutex_);
  Block* old = current_.load(std::memory_order_seq_cst);
  // A window holding only fan-out records is still a window: a backfill replaying rows records no
  // shape, and dropping its window would make a copy look healthier than it is.
  if (!old->touched.load(std::memory_order_acquire)) return std::nullopt;

  Window window;
  window.model_version = model_version_;
  window.started_ns = old->started_ns.load(std::memory_order_relaxed);
  window.ended_ns = now();
  Block* next = old == blocks_[0].get() ? blocks_[1].get() : blocks_[0].get();
  next->started_ns.store(now(), std::memory_order_relaxed);
  current_.store(next, std::memory_order_seq_cst);
  while (old->writers.load(std::memory_order_seq_cst) != 0) std::this_thread::yield();

  for (std::size_t i = 0; i < shapes_.size(); ++i) {
    Block::Slot& slot = old->slots[i];
    const std::uint64_t calls = slot.calls.load(std::memory_order_relaxed);
    if (calls == 0) continue;
    const ShapeInfo& info = shapes_[i];
    ShapeStats stats;
    stats.shape_id = info.id;
    stats.group = info.group;
    stats.entity = info.entity;
    stats.kind = info.kind;
    stats.calls = calls;
    stats.rows = slot.rows.load(std::memory_order_relaxed);
    stats.errors = slot.errors.load(std::memory_order_relaxed);
    stats.latency = slot.latency.snapshot();
    for (FilterNode* node = slot.filters.load(std::memory_order_acquire); node != nullptr;
         node = node->next) {
      stats.filtered[node->key] += node->calls.load(std::memory_order_relaxed);
    }
    window.shapes.push_back(std::move(stats));
  }
  for (FanNode* node = old->fanned.load(std::memory_order_acquire); node != nullptr;
       node = node->next) {
    window.fanned.push_back(FanOutStats{node->group, node->materialization,
                                        node->writes.load(std::memory_order_relaxed),
                                        node->failures.load(std::memory_order_relaxed),
                                        node->latency.snapshot()});
  }
  for (std::size_t g = 0; g < groups_.size(); ++g) {
    for (SecondNode* node = old->seconds[g].load(std::memory_order_acquire); node != nullptr;
         node = node->next) {
      window.write_seconds[groups_[g]][node->second] += node->rows.load(std::memory_order_relaxed);
    }
  }
  window.storage = std::move(storage_window_);
  storage_window_.clear();
  for (const auto& [group, kept] : storage_) {
    window.storage_history.insert(window.storage_history.end(), kept.begin(), kept.end());
  }
  window.complete = !incomplete_.exchange(false);
  window.dropped_windows = dropped_;
  old->reset();

  // A full buffer drops the oldest window and says so in the next one. Telemetry is what gets lost
  // when there is no room - never a write, never an operation.
  if (windows_.size() >= max_windows_) {
    if (!windows_.empty()) windows_.pop_front();
    ++dropped_;
  }
  if (max_windows_ > 0) windows_.push_back(window);
  return window;
}

std::vector<Window> Recorder::pending() const {
  const std::lock_guard<std::mutex> lock(mutex_);
  return {windows_.begin(), windows_.end()};
}

void Recorder::mark_incomplete() noexcept { incomplete_.store(true); }

void Recorder::acknowledge(std::size_t count) {
  const std::lock_guard<std::mutex> lock(mutex_);
  for (std::size_t i = 0; i < count && !windows_.empty(); ++i) windows_.pop_front();
}

}  // namespace sde
