#pragma once

/// What the PostgreSQL live tests share: a connection of their own, for setting up and for reading
/// the catalogue with SQL written in the test rather than with the adapter under test; a schema per
/// test, dropped with it; and a restricted runtime login beside the provisioning one, as a
/// deployment has them.

#include <libpq-fe.h>

#include <map>
#include <optional>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

#include "engines/postgres/connection.hpp"
#include "live/live.hpp"

namespace sde::live {

/// The DSN with some parameters replaced, as a keyword/value connection string. libpq parses it
/// (`PQconninfoParse`), so a URI and a keyword string are both read as the server's client reads
/// them, and a password with a quote in it is escaped rather than cut.
inline std::string conninfo(const std::string& dsn,
                            const std::map<std::string, std::string>& overrides) {
  char* error = nullptr;
  PQconninfoOption* options = PQconninfoParse(dsn.c_str(), &error);
  if (options == nullptr) {
    const std::string why = error != nullptr ? error : "out of memory";
    PQfreemem(error);
    throw std::runtime_error("the DSN does not parse: " + why);
  }
  std::map<std::string, std::string> values;
  for (const PQconninfoOption* option = options; option->keyword != nullptr; ++option) {
    if (option->val != nullptr) values[option->keyword] = option->val;
  }
  PQconninfoFree(options);
  for (const auto& [keyword, value] : overrides) values[keyword] = value;
  std::string out;
  for (const auto& [keyword, value] : values) {
    std::string quoted;
    for (const char c : value) {
      if (c == '\\' || c == '\'') quoted += '\\';
      quoted += c;
    }
    out += (out.empty() ? "" : " ") + keyword + "='" + quoted + "'";
  }
  return out;
}

/// Statements a test sets up and reads back with, on a connection of its own.
class Admin {
 public:
  using Rows = std::vector<std::vector<std::optional<std::string>>>;

  explicit Admin(const std::string& dsn) : connection_(dsn) {}

  detail::postgres::Result run(const std::string& sql,
                               const std::vector<std::optional<std::string>>& parameters = {}) {
    return connection_.execute(sql, parameters);
  }
  /// Every row as text, a null as nothing.
  Rows rows(const std::string& sql, const std::vector<std::optional<std::string>>& parameters = {}) {
    const detail::postgres::Result result = run(sql, parameters);
    Rows out;
    for (int row = 0; row < result.rows(); ++row) {
      std::vector<std::optional<std::string>> cells;
      for (int column = 0; column < result.columns(); ++column) {
        cells.push_back(result.is_null(row, column)
                            ? std::nullopt
                            : std::optional<std::string>(std::string(result.text(row, column))));
      }
      out.push_back(std::move(cells));
    }
    return out;
  }
  void drop(const std::string& table) { (void)run("DROP TABLE IF EXISTS \"" + table + "\""); }

 private:
  detail::postgres::Connection connection_;
};

/// A schema of its own, dropped with the object, and the DSN whose search path is that schema.
class Scope {
 public:
  explicit Scope(std::string dsn, const std::string& prefix = "sde_live_")
      : dsn_(std::move(dsn)), admin_(dsn_), name_(prefix + fresh(12)) {
    (void)admin_.run("CREATE SCHEMA \"" + name_ + "\"");
  }
  ~Scope() {
    try {
      (void)admin_.run("DROP SCHEMA IF EXISTS \"" + name_ + "\" CASCADE");
    } catch (...) {
      // A test that failed has said so already; a leftover schema has a name nobody reuses.
    }
  }
  Scope(const Scope&) = delete;
  Scope& operator=(const Scope&) = delete;

  [[nodiscard]] const std::string& name() const noexcept { return name_; }
  /// The environment's DSN, working in this schema.
  [[nodiscard]] std::string dsn() const {
    return conninfo(dsn_, {{"options", "-csearch_path=" + name_}});
  }

 private:
  std::string dsn_;
  Admin admin_;
  std::string name_;
};

/// A schema with a restricted runtime login on it: USAGE on the schema and whatever a test grants,
/// beside the environment's login, which provisions. The login is dropped with the object.
class Roles {
 public:
  explicit Roles(const std::string& dsn)
      : dsn_(dsn), root_(dsn), scope_(dsn, "sde_roles_"), username_(scope_.name() + "_app"),
        password_(fresh(32)) {
    (void)root_.run("CREATE ROLE \"" + username_ + "\" LOGIN PASSWORD '" + password_ + "'");
    (void)root_.run("GRANT USAGE ON SCHEMA \"" + scope_.name() + "\" TO \"" + username_ + "\"");
  }
  ~Roles() {
    try {
      (void)root_.run("DROP OWNED BY \"" + username_ + "\"");
      (void)root_.run("DROP ROLE IF EXISTS \"" + username_ + "\"");
    } catch (...) {
      // As for the schema: reported by the test that failed, and named so nothing reuses it.
    }
  }
  Roles(const Roles&) = delete;
  Roles& operator=(const Roles&) = delete;

  [[nodiscard]] const std::string& schema() const noexcept { return scope_.name(); }
  [[nodiscard]] const std::string& username() const noexcept { return username_; }
  /// The provisioning login, in this schema.
  [[nodiscard]] std::string operator_dsn() const { return scope_.dsn(); }
  /// The runtime login, in this schema.
  [[nodiscard]] std::string runtime_dsn() const {
    return conninfo(dsn_, {{"user", username_},
                           {"password", password_},
                           {"options", "-csearch_path=" + scope_.name()}});
  }
  void grant(const std::string& table, bool revoke = false) {
    const std::string target = "\"" + scope_.name() + "\".\"" + table + "\"";
    (void)root_.run(revoke ? "REVOKE SELECT ON " + target + " FROM \"" + username_ + "\""
                           : "GRANT SELECT, INSERT ON " + target + " TO \"" + username_ + "\"");
  }

 private:
  std::string dsn_;
  Admin root_;
  Scope scope_;
  std::string username_;
  std::string password_;
};

}  // namespace sde::live
