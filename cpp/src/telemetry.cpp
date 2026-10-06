#include "sde/telemetry.hpp"

#include <algorithm>
#include <bit>
#include <cmath>
#include <stdexcept>

#include "python_compat.hpp"

namespace sde {

namespace {

using detail::python_repr;

constexpr std::string_view kTimeTypes[] = {"date", "timestamp", "timestamptz"};
/// The kinds of read that take a `where` and so report what they filtered on.
constexpr std::string_view kFilteredKinds[] = {"aggregate", "full_scan", "range_read"};

bool contains(const std::string_view (&set)[3], std::string_view value) {
  return std::find(std::begin(set), std::end(set), value) != std::end(set);
}

Json optional_number(const std::optional<double>& value) {
  return value ? Json(*value) : Json(nullptr);
}

/// Nearest-rank: the smallest sample at least `fraction` of the data is not above - the same rank
/// rule as `Histogram::percentile_ms` (`telemetry/007`).
std::optional<double> at_rank(const std::vector<double>& ordered, double fraction) {
  if (ordered.empty()) return std::nullopt;
  const double product = fraction * static_cast<double>(ordered.size());
  const auto ceiling = static_cast<std::size_t>(std::ceil(product));
  const std::size_t rank = std::max<std::size_t>(1, ceiling) - 1;
  return ordered[std::min(ordered.size() - 1, rank)];
}

/// `a / b` for two non-negative integers, as the reference's `int / int` rounds it. Exact for every
/// operand below 2^53, which is every count a window can hold.
double ratio(std::uint64_t a, std::uint64_t b) {
  return static_cast<double>(a) / static_cast<double>(b);
}

/// `floor(a * b / c)`, exactly, without a 128-bit type: the product as two 64-bit halves, then
/// long division. A terabyte's change times a day's nanoseconds is past 64 bits, and the reference's
/// integers have no width at all. `c` is below 2^63 and the quotient fits 64 bits.
std::uint64_t mul_div(std::uint64_t a, std::uint64_t b, std::uint64_t c) {
  const std::uint64_t a_lo = a & 0xFFFFFFFFU;
  const std::uint64_t a_hi = a >> 32U;
  const std::uint64_t b_lo = b & 0xFFFFFFFFU;
  const std::uint64_t b_hi = b >> 32U;
  const std::uint64_t lo_lo = a_lo * b_lo;
  const std::uint64_t hi_lo = a_hi * b_lo;
  const std::uint64_t lo_hi = a_lo * b_hi;
  const std::uint64_t hi_hi = a_hi * b_hi;
  const std::uint64_t cross = (lo_lo >> 32U) + (hi_lo & 0xFFFFFFFFU) + lo_hi;
  const std::uint64_t high = hi_hi + (hi_lo >> 32U) + (cross >> 32U);
  const std::uint64_t low = (cross << 32U) | (lo_lo & 0xFFFFFFFFU);
  std::uint64_t quotient = 0;
  std::uint64_t remainder = 0;
  for (int bit = 127; bit >= 0; --bit) {
    const std::uint64_t word = bit >= 64 ? high : low;
    remainder = (remainder << 1U) | ((word >> static_cast<unsigned>(bit % 64)) & 1U);
    if (remainder >= c) {
      remainder -= c;
      if (bit < 64) quotient |= std::uint64_t{1} << static_cast<unsigned>(bit);
    }
  }
  return quotient;
}

GroupFeatures with_missing(GroupFeatures features, std::initializer_list<const char*> also = {}) {
  for (const char* reason : also) features.missing.insert(reason);
  const auto unknown = [&](std::string_view name, bool absent) {
    if (absent) features.missing.emplace(name);
  };
  unknown("read_write_ratio", !features.read_write_ratio);
  unknown("latency_p50_ms", !features.latency_p50_ms);
  unknown("latency_p99_ms", !features.latency_p99_ms);
  unknown("result_cardinality_p50", !features.result_cardinality_p50);
  unknown("result_cardinality_p99", !features.result_cardinality_p99);
  unknown("total_bytes", !features.total_bytes);
  unknown("daily_growth_bytes", !features.daily_growth_bytes);
  unknown("index_to_table_ratio", !features.index_to_table_ratio);
  unknown("pk_access_share", !features.pk_access_share);
  unknown("time_filtered_share", !features.time_filtered_share);
  unknown("write_burstiness", !features.write_burstiness);
  unknown("error_share", !features.error_share);
  return features;
}

}  // namespace

// ── Histogram ───────────────────────────────────────────────────────────────────────────────────

int Histogram::bucket_of(std::int64_t nanoseconds) noexcept {
  if (nanoseconds < BUCKET_BASE_NS) return 0;
  const auto micros = static_cast<std::uint64_t>(nanoseconds / BUCKET_BASE_NS);
  const int length = std::bit_width(micros);
  return std::min(BUCKET_COUNT - 1, length);
}

double Histogram::upper_edge_ms(int bucket) noexcept {
  // Both operands are exact doubles, so the quotient is the correctly rounded one the reference's
  // `int / int` gives.
  const auto edge_ns = static_cast<std::uint64_t>(BUCKET_BASE_NS) << static_cast<unsigned>(bucket);
  return static_cast<double>(edge_ns) / 1'000'000.0;
}

void Histogram::record(std::int64_t nanoseconds) noexcept {
  ++count;
  total += static_cast<std::uint64_t>(std::max<std::int64_t>(nanoseconds, 0));
  ++buckets[static_cast<std::size_t>(bucket_of(nanoseconds))];
}

std::optional<double> Histogram::percentile_ms(double fraction) const noexcept {
  if (count == 0) return std::nullopt;
  const double target = fraction * static_cast<double>(count);
  std::uint64_t seen = 0;
  for (int index = 0; index < BUCKET_COUNT; ++index) {
    seen += buckets[static_cast<std::size_t>(index)];
    if (static_cast<double>(seen) >= target) return upper_edge_ms(index);
  }
  return std::nullopt;
}

void Histogram::merge(const Histogram& other) noexcept {
  for (std::size_t i = 0; i < buckets.size(); ++i) buckets[i] += other.buckets[i];
  count += other.count;
  total += other.total;
}

// ── Records ─────────────────────────────────────────────────────────────────────────────────────

Json CopyFreshness::as_record() const {
  Json record = Json::object();
  record.set("group", group);
  record.set("materialization", materialization);
  record.set("writes", writes);
  record.set("failures", failures);
  record.set("lag_p50_ms", optional_number(lag_p50_ms));
  record.set("lag_p99_ms", optional_number(lag_p99_ms));
  record.set("complete", complete());
  return record;
}

Json GroupFeatures::as_record() const {
  Json body = Json::object();
  body.set("calls", calls);
  if (read_write_ratio) body.set("read_write_ratio", *read_write_ratio);
  Json mix = Json::object();
  for (const auto& [kind, share] : shape_mix) mix.set(kind, share);
  body.set("shape_mix", std::move(mix));
  if (latency_p50_ms) body.set("latency_p50_ms", *latency_p50_ms);
  if (latency_p99_ms) body.set("latency_p99_ms", *latency_p99_ms);
  if (result_cardinality_p50) body.set("result_cardinality_p50", *result_cardinality_p50);
  if (result_cardinality_p99) body.set("result_cardinality_p99", *result_cardinality_p99);
  if (total_bytes) body.set("total_bytes", *total_bytes);
  if (daily_growth_bytes) body.set("daily_growth_bytes", *daily_growth_bytes);
  if (index_to_table_ratio) body.set("index_to_table_ratio", *index_to_table_ratio);
  if (pk_access_share) body.set("pk_access_share", *pk_access_share);
  body.set("has_time_dimension", has_time_dimension);
  if (time_filtered_share) body.set("time_filtered_share", *time_filtered_share);
  body.set("distinct_shapes", distinct_shapes);
  if (write_burstiness) body.set("write_burstiness", *write_burstiness);
  if (error_share) body.set("error_share", *error_share);
  Json names = Json::array();
  for (const std::string& name : missing) names.as_array().emplace_back(name);
  body.set("missing", std::move(names));
  body.set("complete", complete);
  return body;
}

// ── The window ──────────────────────────────────────────────────────────────────────────────────

std::vector<CopyFreshness> Window::copies(std::string_view group) const {
  std::vector<const FanOutStats*> mine;
  for (const FanOutStats& stats : fanned) {
    if (stats.group == group) mine.push_back(&stats);
  }
  std::sort(mine.begin(), mine.end(), [](const FanOutStats* a, const FanOutStats* b) {
    return a->materialization < b->materialization;
  });
  std::vector<CopyFreshness> out;
  for (const FanOutStats* stats : mine) {
    out.push_back(CopyFreshness{stats->group, stats->materialization, stats->writes,
                                stats->failures, stats->latency.percentile_ms(0.50),
                                stats->latency.percentile_ms(0.99)});
  }
  return out;
}

GroupFeatures Window::features(std::string_view group, bool time_dimension,
                               const std::map<std::string, std::set<std::string>>* time_fields_of,
                               const std::map<std::string, std::string>* range_fields) const {
  std::vector<const ShapeStats*> records;
  for (const ShapeStats& stats : shapes) {
    if (stats.group == group) records.push_back(&stats);
  }
  if (records.empty()) {
    // `no_traffic` is the reason, and the unknown fields are named too, by the same derivation.
    GroupFeatures idle;
    idle.complete = complete;
    return with_missing(idle, {"no_traffic"});
  }

  std::uint64_t writes = 0;
  std::uint64_t reads = 0;
  Histogram latency;
  std::map<std::string, std::uint64_t> by_kind;
  std::uint64_t point_reads = 0;
  std::uint64_t errors = 0;
  std::vector<double> cardinalities;
  for (const ShapeStats* record : records) {
    const bool write = is_write_kind(record->kind);
    (write ? writes : reads) += record->calls;
    latency.merge(record->latency);
    by_kind[record->kind] += record->calls;
    if (record->kind == "point_read") point_reads += record->calls;
    errors += record->errors;
    // A failed call took time and counts in the histogram; it returned no rows because it failed,
    // so it does not count here, and a shape whose every call failed contributes nothing
    // (`telemetry/008`).
    if (!write && record->calls > record->errors) {
      cardinalities.push_back(ratio(record->rows, record->calls - record->errors));
    }
  }
  const std::uint64_t calls = writes + reads;
  std::sort(cardinalities.begin(), cardinalities.end());

  GroupFeatures out;
  out.calls = calls;
  if (writes != 0) out.read_write_ratio = ratio(reads, writes);
  if (calls != 0) {
    for (const auto& [kind, hits] : by_kind) out.shape_mix.emplace(kind, ratio(hits, calls));
  }
  out.latency_p50_ms = latency.percentile_ms(0.5);
  out.latency_p99_ms = latency.percentile_ms(0.99);
  out.result_cardinality_p50 = at_rank(cardinalities, 0.5);
  out.result_cardinality_p99 = at_rank(cardinalities, 0.99);
  if (calls != 0) out.pk_access_share = ratio(point_reads, calls);
  out.has_time_dimension = time_dimension;
  out.distinct_shapes = records.size();
  if (calls != 0) out.error_share = ratio(errors, calls);
  out.complete = complete;

  // A call filtered on time when its filter bounded a time field by a range or compared one by
  // equality. A read that takes a `where` and did not report its filters leaves the share unknown -
  // zero would claim to know - except a range read, whose shape names the field it bounded, and
  // except in an entity with no time field, where nothing could have filtered on time.
  if (time_fields_of != nullptr && calls != 0) {
    std::uint64_t filtered = 0;
    bool known = true;
    for (const ShapeStats* record : records) {
      const auto found = time_fields_of->find(record->entity);
      const std::set<std::string> none;
      const std::set<std::string>& times = found != time_fields_of->end() ? found->second : none;
      std::uint64_t reported = 0;
      for (const auto& [predicates, hits] : record->filtered) {
        reported += hits;
        const bool ranged_on_time = times.count(predicates.range) != 0;
        const bool equal_on_time =
            std::any_of(predicates.equal.begin(), predicates.equal.end(),
                        [&](const std::string& name) { return times.count(name) != 0; });
        if (ranged_on_time || equal_on_time) filtered += hits;
      }
      if (record->calls <= reported || !contains(kFilteredKinds, record->kind) || times.empty()) {
        continue;
      }
      const std::uint64_t unreported = record->calls - reported;
      std::optional<std::string> bounded;
      if (range_fields != nullptr) {
        if (const auto ranged = range_fields->find(record->shape_id); ranged != range_fields->end()) {
          bounded = ranged->second;
        }
      }
      if (record->kind == "range_read" && bounded) {
        if (times.count(*bounded) != 0) filtered += unreported;
      } else {
        known = false;
      }
    }
    if (known) out.time_filtered_share = ratio(filtered, calls);
  }

  // Storage: the latest sample taken in this window, growth from the oldest kept, projected to a
  // day only from at least an hour, truncated toward zero, and negative when the group shrank.
  const StorageSample* latest = nullptr;
  for (const StorageSample& sample : storage) {
    if (sample.group == group && (latest == nullptr || sample.at_ns >= latest->at_ns)) {
      latest = &sample;
    }
  }
  if (latest != nullptr) {
    out.total_bytes = latest->total_bytes;
    const std::int64_t rest = latest->total_bytes - latest->secondary_index_bytes;
    if (rest > 0) {
      out.index_to_table_ratio = ratio(static_cast<std::uint64_t>(latest->secondary_index_bytes),
                                       static_cast<std::uint64_t>(rest));
    }
    const StorageSample* oldest = latest;
    for (const StorageSample& sample : storage_history) {
      if (sample.group == group && sample.at_ns < oldest->at_ns) oldest = &sample;
    }
    const std::int64_t elapsed = latest->at_ns - oldest->at_ns;
    if (elapsed >= GROWTH_MIN_NS) {
      const std::int64_t delta = latest->total_bytes - oldest->total_bytes;
      const std::uint64_t magnitude =
          delta < 0 ? std::uint64_t{0} - static_cast<std::uint64_t>(delta) : static_cast<std::uint64_t>(delta);
      const auto projected = static_cast<std::int64_t>(mul_div(
          magnitude, static_cast<std::uint64_t>(DAY_NS), static_cast<std::uint64_t>(elapsed)));
      out.daily_growth_bytes = delta >= 0 ? projected : -projected;
    }
  }

  // Burstiness: the busiest second's rows, over the window's mean rate. `M * S / W`.
  if (const auto seconds = write_seconds.find(std::string(group)); seconds != write_seconds.end()) {
    std::uint64_t written = 0;
    std::uint64_t busiest = 0;
    std::int64_t last = 0;
    for (const auto& [second, rows] : seconds->second) {
      written += rows;
      busiest = std::max(busiest, rows);
      last = std::max(last, second);
    }
    if (written > 0) {
      const std::int64_t span_ns = ended_ns - started_ns;
      const std::int64_t duration = span_ns <= 0 ? 0 : (span_ns + SECOND_NS - 1) / SECOND_NS;
      const std::int64_t span = std::max<std::int64_t>({1, duration, last + 1});
      out.write_burstiness = ratio(busiest * static_cast<std::uint64_t>(span), written);
    }
  }
  return with_missing(out);
}

Json Window::shape_records(const Model& model, std::string_view group) const {
  std::map<std::string, const ShapeStats*> recorded;
  for (const ShapeStats& stats : shapes) {
    if (stats.group == group) recorded.emplace(stats.shape_id, &stats);
  }
  std::set<std::string> enumerated;
  Json out = Json::array();
  for (const OperationShape& shape : model.shapes()) {
    const auto found = recorded.find(shape.id);
    if (found == recorded.end()) continue;
    enumerated.insert(shape.id);
    const ShapeStats& stats = *found->second;
    Json entry = Json::object();
    entry.set("id", shape.id);
    entry.set("entity", shape.entity);
    entry.set("kind", shape.kind);
    Json fields = Json::array();
    for (const std::string& name : shape.fields) fields.as_array().emplace_back(name);
    entry.set("fields", std::move(fields));
    if (shape.target) entry.set("target", *shape.target);
    entry.set("calls", stats.calls);
    entry.set("errors", stats.errors);
    entry.set("rows", stats.rows);
    entry.set("latency_p50_ms", optional_number(stats.latency.percentile_ms(0.5)));
    entry.set("latency_p99_ms", optional_number(stats.latency.percentile_ms(0.99)));
    if (!stats.filtered.empty()) {
      Json filtered = Json::array();
      for (const auto& [predicates, calls] : stats.filtered) {
        Json item = Json::object();
        Json equal = Json::array();
        for (const std::string& name : predicates.equal) equal.as_array().emplace_back(name);
        item.set("equal", std::move(equal));
        if (!predicates.range.empty()) item.set("range", predicates.range);
        item.set("calls", calls);
        filtered.as_array().push_back(std::move(item));
      }
      entry.set("filtered_on", std::move(filtered));
    }
    out.as_array().push_back(std::move(entry));
  }
  std::vector<std::string> unknown;
  for (const auto& [id, stats] : recorded) {
    if (enumerated.count(id) == 0) unknown.push_back(id);
  }
  if (!unknown.empty()) {
    throw std::invalid_argument(
        "this window has records for shapes this model does not enumerate: " +
        python_repr(unknown) +
        ". A shape is described from the model's own enumeration, never from what the recorder "
        "was told, so a record for an identifier the model does not produce has no fields to "
        "report.");
  }
  return out;
}

Json Window::as_record(const Model& model) const {
  if (model.version() != model_version) {
    throw std::invalid_argument(
        "this window measured model version " + model_version +
        " and it is being serialised against " + model.version() +
        ". Only one fact is read from the model here - whether a group has a time dimension - "
        "and reading it from another model would attach it to the wrong groups. Keep the model "
        "the recorder was created for, or drop the window: a window measured against a model "
        "that no longer exists cannot be scored against the one that does.");
  }
  std::set<std::string> named;
  for (const ShapeStats& stats : shapes) named.insert(stats.group);
  for (const FanOutStats& stats : fanned) named.insert(stats.group);
  std::vector<std::string> unknown;
  for (const std::string& name : named) {
    const bool known = std::any_of(model.groups().begin(), model.groups().end(),
                                   [&](const Group& group) { return group.name == name; });
    if (!known) unknown.push_back(name);
  }
  if (!unknown.empty()) {
    throw std::invalid_argument(
        "this window has records for groups this model does not have: " + python_repr(unknown) +
        ". The recorder is given a model version and the groups come from the operations it "
        "observed, so this is a session routing a model other than the one measured.");
  }

  std::map<std::string, std::string> ranges;
  for (const OperationShape& shape : model.shapes()) {
    if (shape.kind == "range_read" && !shape.fields.empty()) ranges.emplace(shape.id, shape.fields[0]);
  }
  Json groups = Json::object();
  for (const std::string& name : named) {
    const Group* group = nullptr;
    for (const Group& candidate : model.groups()) {
      if (candidate.name == name) group = &candidate;
    }
    const auto times = time_fields(model, *group);
    Json record = features(name, has_time_dimension(model, *group), &times, &ranges).as_record();
    Json shape_list = shape_records(model, name);
    // Absent rather than empty, like `copies`: a group here saw traffic.
    if (!shape_list.as_array().empty()) record.set("shapes", std::move(shape_list));
    Json copy_list = Json::array();
    for (const CopyFreshness& copy : copies(name)) copy_list.as_array().push_back(copy.as_record());
    if (!copy_list.as_array().empty()) record.set("copies", std::move(copy_list));
    groups.set(name, std::move(record));
  }

  Json document = Json::object();
  document.set("model_version", model_version);
  document.set("complete", complete);
  document.set("dropped_windows", dropped_windows);
  document.set("groups", std::move(groups));
  return document;
}

bool has_time_dimension(const Model& model, const Group& group) {
  for (const std::string& member : group.members) {
    for (const Field& field : model.entity(member).fields) {
      if (contains(kTimeTypes, field.type)) return true;
    }
  }
  return false;
}

std::map<std::string, std::set<std::string>> time_fields(const Model& model, const Group& group) {
  std::map<std::string, std::set<std::string>> out;
  for (const std::string& member : group.members) {
    auto& names = out[member];
    for (const Field& field : model.entity(member).fields) {
      if (contains(kTimeTypes, field.type)) names.insert(field.name);
    }
  }
  return out;
}

}  // namespace sde
