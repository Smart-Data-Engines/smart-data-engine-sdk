#pragma once

/// One test body for both engines, as the reference parametrises its own: a side is an engine in a
/// namespace of its own - a PostgreSQL schema or a ClickHouse database - with the environment's
/// login provisioning it and a restricted runtime login beside it, all dropped with the object. A
/// side is made only for an engine whose adapter is built, and only when its DSN is set.

#include <map>
#include <memory>
#include <optional>
#include <ostream>
#include <string>
#include <utility>
#include <vector>

#include <gtest/gtest.h>

#include "live/live.hpp"
#include "sde/engine.hpp"
#include "sde/json.hpp"

#ifdef SDE_LIVE_POSTGRES
#include "live/postgres.hpp"
#include "sde/postgres.hpp"
#endif
#ifdef SDE_LIVE_CLICKHOUSE
#include "live/clickhouse.hpp"
#include "sde/clickhouse.hpp"
#endif

namespace sde::live {

class Side {
 public:
  virtual ~Side() = default;
  [[nodiscard]] virtual std::string dialect() const = 0;
  /// The provisioning login's adapter.
  [[nodiscard]] virtual Engine& engine() = 0;
  /// The runtime login's adapter, which holds what `grant` gives it and nothing else.
  [[nodiscard]] virtual Engine& runtime() = 0;
  [[nodiscard]] virtual std::string runtime_dsn() const = 0;
  virtual void grant(const std::string& table) = 0;
  /// Fresh adapters on the same namespace, as an operator re-running a job has.
  virtual void reconnect() = 0;
  /// One statement as the administrator, in the namespace.
  virtual void run(const std::string& sql) = 0;
  /// The first cell of the answer, as text.
  [[nodiscard]] virtual std::string scalar(const std::string& sql) = 0;
  /// Rows out of a table by hand, waited for: deliberate damage is the only way to test a gate.
  virtual void remove(const std::string& table, const std::string& where) = 0;
  virtual void drop(const std::string& table) = 0;

  [[nodiscard]] Migratable& migration() { return *engine().capabilities().migration; }
};

#ifdef SDE_LIVE_POSTGRES
class PostgresSide final : public Side {
 public:
  explicit PostgresSide(const std::string& dsn) : roles_(dsn) { reconnect(); }
  [[nodiscard]] std::string dialect() const override { return "postgres"; }
  [[nodiscard]] Engine& engine() override { return *engine_; }
  [[nodiscard]] Engine& runtime() override { return *runtime_; }
  [[nodiscard]] std::string runtime_dsn() const override { return roles_.runtime_dsn(); }
  void grant(const std::string& table) override { roles_.grant(table); }
  void reconnect() override {
    engine_ = std::make_unique<PostgresEngine>(roles_.operator_dsn());
    engine_->connect();
    runtime_ = std::make_unique<PostgresEngine>(roles_.runtime_dsn());
    runtime_->connect();
  }
  void run(const std::string& sql) override { (void)Admin(roles_.operator_dsn()).run(sql); }
  [[nodiscard]] std::string scalar(const std::string& sql) override {
    const Admin::Rows rows = Admin(roles_.operator_dsn()).rows(sql);
    return rows.empty() || rows[0].empty() || !rows[0][0] ? std::string() : *rows[0][0];
  }
  void remove(const std::string& table, const std::string& where) override {
    run("DELETE FROM \"" + table + "\" WHERE " + where);
  }
  void drop(const std::string& table) override { run("DROP TABLE \"" + table + "\""); }

 private:
  Roles roles_;
  std::unique_ptr<PostgresEngine> engine_;
  std::unique_ptr<PostgresEngine> runtime_;
};
#endif

#ifdef SDE_LIVE_CLICKHOUSE
class ClickHouseSide final : public Side {
 public:
  explicit ClickHouseSide(const std::string& dsn) : roles_(dsn) { reconnect(); }
  [[nodiscard]] std::string dialect() const override { return "clickhouse"; }
  [[nodiscard]] Engine& engine() override { return *engine_; }
  [[nodiscard]] Engine& runtime() override { return *runtime_; }
  [[nodiscard]] std::string runtime_dsn() const override { return roles_.runtime_dsn(); }
  void grant(const std::string& table) override { roles_.grant(table); }
  void reconnect() override {
    engine_ = std::make_unique<ClickHouseEngine>(roles_.operator_dsn());
    engine_->connect();
    runtime_ = std::make_unique<ClickHouseEngine>(roles_.runtime_dsn());
    runtime_->connect();
  }
  void run(const std::string& sql) override { roles_.command(sql); }
  [[nodiscard]] std::string scalar(const std::string& sql) override {
    const std::vector<Json> rows = roles_.rows(sql);
    if (rows.empty() || rows[0].as_object().empty()) return "";
    const Json& cell = rows[0].as_object().begin()->second;
    return cell.is_string() ? cell.as_string() : dump_json(cell);
  }
  void remove(const std::string& table, const std::string& where) override {
    run("DELETE FROM `" + table + "` WHERE " + where + " SETTINGS mutations_sync = 2");
  }
  void drop(const std::string& table) override { run("DROP TABLE `" + table + "` SYNC"); }

 private:
  ClickHouseRoles roles_;
  std::unique_ptr<ClickHouseEngine> engine_;
  std::unique_ptr<ClickHouseEngine> runtime_;
};
#endif

/// A side of this engine, or nothing when its adapter is not built or its DSN is not set.
inline std::unique_ptr<Side> side_of(const std::string& dialect) {
#ifdef SDE_LIVE_POSTGRES
  if (dialect == "postgres") {
    const std::optional<std::string> dsn = dsn_from("SDE_POSTGRES_DSN");
    return dsn ? std::make_unique<PostgresSide>(*dsn) : nullptr;
  }
#endif
#ifdef SDE_LIVE_CLICKHOUSE
  if (dialect == "clickhouse") {
    const std::optional<std::string> dsn = dsn_from("SDE_CLICKHOUSE_DSN");
    return dsn ? std::make_unique<ClickHouseSide>(*dsn) : nullptr;
  }
#endif
  return nullptr;
}

/// The engines whose adapters are built.
inline std::vector<std::string> dialects() {
  std::vector<std::string> out;
#ifdef SDE_LIVE_CLICKHOUSE
  out.emplace_back("clickhouse");
#endif
#ifdef SDE_LIVE_POSTGRES
  out.emplace_back("postgres");
#endif
  return out;
}

inline std::string engine_name(const std::string& dialect) {
  return dialect == "postgres" ? std::string("Postgres") : std::string("ClickHouse");
}

/// Where a group goes: from `source` to `target`, through one adapter or two.
struct Direction {
  std::string source;
  std::string target;
  bool two = false;
};

inline void PrintTo(const Direction& direction, std::ostream* out) {
  *out << direction.source << " to " << direction.target
       << (direction.two ? ", two adapters" : ", one adapter");
}

inline std::string direction_name(const ::testing::TestParamInfo<Direction>& info) {
  std::string name = engine_name(info.param.source) + "To" + engine_name(info.param.target);
  if (info.param.source == info.param.target) name += info.param.two ? "TwoAdapters" : "OneAdapter";
  return name;
}

/// Every direction the built adapters allow: within each engine through one adapter and through
/// two, and between the engines both ways.
inline std::vector<Direction> directions() {
  std::vector<Direction> out;
  for (const std::string& dialect : dialects()) {
    out.push_back({dialect, dialect, false});
    out.push_back({dialect, dialect, true});
  }
  if (dialects().size() == 2) {
    out.push_back({"postgres", "clickhouse", true});
    out.push_back({"clickhouse", "postgres", true});
  }
  return out;
}

/// Between the engines only.
inline std::vector<Direction> across() {
  std::vector<Direction> out;
  if (dialects().size() == 2) {
    out.push_back({"postgres", "clickhouse", true});
    out.push_back({"clickhouse", "postgres", true});
  }
  return out;
}

inline std::string dialect_name(const ::testing::TestParamInfo<std::string>& info) {
  return engine_name(info.param);
}

}  // namespace sde::live

/// The skip, or in CI the failure, for an engine whose DSN is not set.
#define SDE_REQUIRE_SIDE(side, dialect)                                                     \
  do {                                                                                      \
    if (!(side)) {                                                                          \
      if (::sde::live::in_ci()) FAIL() << "the " << (dialect) << " DSN is not set in CI";  \
      GTEST_SKIP() << "set the " << (dialect) << " DSN to run this direction";            \
    }                                                                                       \
  } while (false)
