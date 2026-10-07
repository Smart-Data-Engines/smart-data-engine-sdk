#pragma once

/// What the ClickHouse live tests share: a database of its own per test, dropped with it; the DSN
/// that works in it; statements a test sets up and reads back with, on the adapter's transport but
/// never through the adapter under test; and a restricted runtime login beside the administrator.

#include <string>
#include <utility>
#include <vector>

#include "engines/clickhouse/dsn.hpp"
#include "engines/clickhouse/http.hpp"
#include "live/live.hpp"
#include "sde/json.hpp"
#include "sde/layout.hpp"
#include "sde/model.hpp"
#include "sde/placement.hpp"

namespace sde::live {

/// The DSN with another database: the path segment replaced, everything else as it was.
inline std::string with_database(const std::string& dsn, const std::string& database) {
  const std::size_t authority = dsn.find("://");
  const std::size_t path = dsn.find('/', authority == std::string::npos ? 0 : authority + 3);
  const std::size_t query = dsn.find('?', path);
  return dsn.substr(0, path + 1) + database + (query == std::string::npos ? "" : dsn.substr(query));
}

/// The DSN with other credentials.
inline std::string with_login(const std::string& dsn, const std::string& user,
                              const std::string& password) {
  const std::size_t authority = dsn.find("://") + 3;
  const std::size_t at = dsn.find('@', authority);
  const std::size_t host = at == std::string::npos ? authority : at + 1;
  return dsn.substr(0, authority) + user + ":" + password + "@" + dsn.substr(host);
}

/// Statements on a connection of the test's own.
class ClickHouseAdmin {
 public:
  explicit ClickHouseAdmin(const std::string& dsn) : http_(detail::clickhouse::parse_dsn(dsn)) {}
  void run(const std::string& sql) const { (void)http_.post(sql); }
  /// Each row of the answer, as JSON.
  [[nodiscard]] std::vector<Json> rows(const std::string& sql) const {
    const std::string body = http_.post(sql, "JSONEachRow");
    std::vector<Json> out;
    std::size_t start = 0;
    while (start < body.size()) {
      const std::size_t end = body.find('\n', start);
      const std::string line = body.substr(start, end == std::string::npos ? std::string::npos : end - start);
      if (!line.empty()) out.push_back(parse_json(line));
      if (end == std::string::npos) break;
      start = end + 1;
    }
    return out;
  }

 private:
  detail::clickhouse::Http http_;
};

/// A database of its own, Atomic, dropped with the object.
class ClickHouseScope {
 public:
  explicit ClickHouseScope(std::string dsn, const std::string& prefix = "sde_live_")
      : dsn_(std::move(dsn)), root_(dsn_), name_(prefix + fresh(12)) {
    root_.run("CREATE DATABASE `" + name_ + "` ENGINE = Atomic");
  }
  ~ClickHouseScope() {
    try {
      root_.run("DROP DATABASE IF EXISTS `" + name_ + "` SYNC");
    } catch (...) {
      // Reported by the test that failed; the name is never reused.
    }
  }
  ClickHouseScope(const ClickHouseScope&) = delete;
  ClickHouseScope& operator=(const ClickHouseScope&) = delete;

  [[nodiscard]] const std::string& name() const noexcept { return name_; }
  [[nodiscard]] std::string dsn() const { return with_database(dsn_, name_); }
  [[nodiscard]] ClickHouseAdmin admin() const { return ClickHouseAdmin(dsn()); }
  [[nodiscard]] const ClickHouseAdmin& root() const noexcept { return root_; }

 private:
  std::string dsn_;
  ClickHouseAdmin root_;
  std::string name_;
};

/// A layout written out as a map's JSON: tables, columns, and the design it carries.
inline Json layout_json(const PhysicalLayout& layout) {
  Json raw = Json::object();
  Json tables = Json::object();
  for (const auto& [entity, table] : layout.tables) tables.set(entity, table);
  raw.set("tables", std::move(tables));
  Json columns = Json::object();
  for (const auto& [entity, types] : layout.columns) {
    Json typed = Json::object();
    for (const auto& [column, type] : types) typed.set(column, type);
    columns.set(entity, std::move(typed));
  }
  raw.set("columns", std::move(columns));
  return raw;
}

/// A contract-3 map of every group of the model in one engine, each in the dialect's default
/// layout: the reference's `default_layout(model, group, dialect=...)`, written out.
inline PlacementMap default_map(const Model& model, std::string_view dialect,
                                const std::string& engine = "ch") {
  Json groups = Json::object();
  for (const Group& group : model.groups()) {
    Json source = Json::object();
    source.set("id", group.name + "@" + engine);
    source.set("engine", engine);
    source.set("layout", layout_json(default_layout(model, group, dialect)));
    Json placed = Json::object();
    placed.set("source", std::move(source));
    groups.set(group.name, std::move(placed));
  }
  Json document = Json::object();
  document.set("contract", std::int64_t{3});
  document.set("model_version", model.version());
  document.set("map_version", std::int64_t{1});
  document.set("groups", std::move(groups));
  LoadOptions options;
  options.model = &model;
  return load_map(document, options);
}

}  // namespace sde::live
