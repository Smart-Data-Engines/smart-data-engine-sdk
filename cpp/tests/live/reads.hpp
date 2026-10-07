#pragma once

/// What the logical-read tests of both engines share: the reference's six events, the order the
/// reads promise, and an engine wrapper that records every read plan it is handed, so a test can
/// render the very statement the adapter ran and ask the engine's planner about it.

#include <algorithm>
#include <cstdint>
#include <functional>
#include <optional>
#include <string>
#include <utility>
#include <variant>
#include <vector>

#include <gtest/gtest.h>

#include "sde/engine.hpp"
#include "sde/query.hpp"
#include "sde/value.hpp"

namespace sde::live {

/// Six ids whose order as text and as numbers differ: the reference's `IDS`.
inline const std::vector<std::string> kEventIds = {
    "00000000-0000-0001-0000-000000000000", "00000000-0000-0000-ffff-ffffffffffff",
    "ffffffff-ffff-ffff-0000-000000000000", "00000000-0000-0000-0000-000000000001",
    "00000000-0000-0000-0000-000000000002", "00000000-0000-0000-0000-000000000003"};

/// The reference's `BASE` plus some microseconds.
inline Value event_at(int micros_after_base) {
  return *TimestampTz::parse("2026-09-14T00:00:00." + std::to_string(123456 + micros_after_base) +
                             "Z");
}

/// The reference's six rows: labels with two nulls and a tie, times in microsecond ties, amounts at
/// scale 2.
inline std::vector<Row> event_rows() {
  const std::vector<std::optional<std::string>> labels = {"z", std::nullopt, "é", "a",
                                                          std::nullopt, "a"};
  std::vector<Row> rows;
  for (std::size_t i = 0; i < kEventIds.size(); ++i) {
    rows.push_back(Row{{"id", *Uuid::parse(kEventIds[i])},
                       {"label", labels[i] ? Value(*labels[i]) : Value(Null{})},
                       {"at", event_at(static_cast<int>(i / 2))},
                       {"amount", Decimal(std::to_string(i) + ".25")}});
  }
  return rows;
}

/// Whether `left` sorts before `right` in the order the reads promise: text by code point (UTF-8
/// bytes), a UUID by its bytes, an instant in time, a decimal by value.
inline bool before(const Value& left, const Value& right) {
  if (const auto* text = std::get_if<std::string>(&left)) return *text < std::get<std::string>(right);
  if (const auto* id = std::get_if<Uuid>(&left)) {
    return id->to_string() < std::get<Uuid>(right).to_string();
  }
  if (const auto* instant = std::get_if<TimestampTz>(&left)) {
    return instant->micros() < std::get<TimestampTz>(right).micros();
  }
  if (const auto* amount = std::get_if<Decimal>(&left)) return *amount < std::get<Decimal>(right);
  ADD_FAILURE() << "no order for this value here";
  return false;
}

/// Sorted by these fields, in the order the reads promise.
inline std::vector<Row> sorted(std::vector<Row> rows, const std::vector<std::string>& fields,
                               bool descending = false) {
  std::stable_sort(rows.begin(), rows.end(), [&](const Row& left, const Row& right) {
    for (const std::string& field : fields) {
      if (left.at(field) == right.at(field)) continue;
      return descending ? before(right.at(field), left.at(field))
                        : before(left.at(field), right.at(field));
    }
    return false;
  });
  return rows;
}

/// An adapter with every read plan it is handed recorded: the reference's spy on `read_sql`.
template <class Inner>
class Spy final : public Engine, public Queryable, public Countable {
 public:
  struct Read {
    std::string table;
    ReadPlan plan;
    bool count;
  };

  explicit Spy(Inner& inner) : inner_(inner) {}
  std::vector<Read> reads;

  [[nodiscard]] std::string_view dialect() const noexcept override { return inner_.dialect(); }
  std::vector<PhysicalFinding> ensure_schema(const PhysicalLayout& layout,
                                             const Keys& keys) override {
    return inner_.ensure_schema(layout, keys);
  }
  void insert(const std::string& table, const Row& values) override { inner_.insert(table, values); }
  std::optional<Row> get(const std::string& table, const Row& key) override {
    return inner_.get(table, key);
  }
  void transaction(const std::function<void()>& body) override { inner_.transaction(body); }
  [[nodiscard]] Capabilities capabilities() noexcept override {
    Capabilities offered = inner_.capabilities();
    offered.query = this;
    offered.count = this;
    return offered;
  }
  std::vector<Row> select_rows(const std::string& table, const ReadPlan& plan) override {
    reads.push_back({table, plan, false});
    return inner_.select_rows(table, plan);
  }
  std::uint64_t count_rows(const std::string& table, const ReadPlan& plan) override {
    reads.push_back({table, plan, true});
    return inner_.count_rows(table, plan);
  }

 private:
  Inner& inner_;
};

}  // namespace sde::live
