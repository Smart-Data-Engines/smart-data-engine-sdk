#pragma once

/// What the ClickHouse live tests share: a database of its own per test, dropped with it; the DSN
/// that works in it; statements a test sets up and reads back with, on the adapter's transport but
/// never through the adapter under test; and a restricted runtime login beside the administrator.

#include <algorithm>
#include <exception>
#include <string>
#include <utility>
#include <vector>

#include "engines/clickhouse/dsn.hpp"
#include "engines/clickhouse/http.hpp"
#include "live/live.hpp"
#include "live/maps.hpp"
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

/// `http://host:port`, as the reference's driver names the server in a message.
inline std::string url_of(const std::string& dsn) {
  return detail::clickhouse::Http(detail::clickhouse::parse_dsn(dsn)).url();
}

/// Statements on a connection of the test's own.
class ClickHouseAdmin {
 public:
  explicit ClickHouseAdmin(const std::string& dsn) : http_(detail::clickhouse::parse_dsn(dsn)) {}
  explicit ClickHouseAdmin(detail::clickhouse::Target target) : http_(std::move(target)) {}
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

/// A database of its own, Atomic unless another engine is named, dropped with the object.
class ClickHouseScope {
 public:
  explicit ClickHouseScope(std::string dsn, const std::string& prefix = "sde_live_",
                           const std::string& engine = "Atomic")
      : dsn_(std::move(dsn)), root_(dsn_), name_(prefix + fresh(12)) {
    root_.run("CREATE DATABASE `" + name_ + "` ENGINE = " + engine);
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

/// A database of its own with a restricted runtime login on it, beside the environment's login,
/// which provisions: the reference's `runtime_roles("clickhouse")`. The runtime login can read
/// `system.settings` and whatever a test grants. With `operator_login` the operator is a login of
/// its own as well - everything on the database with grant option, and SELECT on the system
/// tables - as a deployment with an administrator beside it has it. Every login and the database
/// are dropped with the object, on a connection whose silence bound is at least five minutes: a
/// large run can leave many parts to delete, and that is outside what the tests measure.
class ClickHouseRoles {
 public:
  explicit ClickHouseRoles(const std::string& dsn, bool operator_login = false)
      : dsn_(dsn), root_(dsn), name_("sde_roles_" + fresh(16)), username_(name_ + "_app"),
        password_(fresh(48)) {
    try {
      root_.run("CREATE USER `" + username_ + "` IDENTIFIED WITH sha256_password BY '" +
                password_ + "'");
      created_ = true;
      root_.run("CREATE DATABASE `" + name_ + "` ENGINE = Atomic");
      root_.run("GRANT SELECT ON system.settings TO `" + username_ + "`");
      operator_dsn_ = with_database(dsn_, name_);
      if (operator_login) {
        operator_user_ = name_ + "_op";
        const std::string secret = fresh(48);
        root_.run("CREATE USER `" + operator_user_ + "` IDENTIFIED WITH sha256_password BY '" +
                  secret + "'");
        root_.run("GRANT ALL ON `" + name_ + "`.* TO `" + operator_user_ + "` WITH GRANT OPTION");
        root_.run("GRANT SELECT ON system.* TO `" + operator_user_ + "`");
        operator_dsn_ = with_login(operator_dsn_, operator_user_, secret);
      }
    } catch (...) {
      drop();
      throw;
    }
  }
  ~ClickHouseRoles() { drop(); }
  ClickHouseRoles(const ClickHouseRoles&) = delete;
  ClickHouseRoles& operator=(const ClickHouseRoles&) = delete;

  [[nodiscard]] const std::string& database() const noexcept { return name_; }
  [[nodiscard]] const std::string& username() const noexcept { return username_; }
  /// The provisioning login - the environment's, or the operator's own - in this database.
  [[nodiscard]] const std::string& operator_dsn() const noexcept { return operator_dsn_; }
  /// The runtime login, in this database.
  [[nodiscard]] std::string runtime_dsn() const {
    return with_login(with_database(dsn_, name_), username_, password_);
  }
  /// SELECT and INSERT on one table of this database, or with `revoke` SELECT taken back.
  void grant(const std::string& table, bool revoke = false) const {
    const std::string target = "`" + name_ + "`.`" + table + "`";
    command(revoke ? "REVOKE SELECT ON " + target + " FROM `" + username_ + "`"
                   : "GRANT SELECT, INSERT ON " + target + " TO `" + username_ + "`");
  }
  /// One statement as the operator, or as the runtime login.
  void command(const std::string& sql, bool runtime = false) const {
    ClickHouseAdmin(runtime ? runtime_dsn() : operator_dsn_).run(sql);
  }
  [[nodiscard]] std::vector<Json> rows(const std::string& sql) const {
    return ClickHouseAdmin(operator_dsn_).rows(sql);
  }

 private:
  void drop() noexcept {
    try {
      detail::clickhouse::Target target = detail::clickhouse::parse_dsn(dsn_);
      target.send_receive_timeout = std::max(300.0, target.send_receive_timeout);
      const ClickHouseAdmin cleanup(std::move(target));
      cleanup.run("DROP DATABASE IF EXISTS `" + name_ + "` SYNC");
      if (created_) cleanup.run("DROP USER IF EXISTS `" + username_ + "`");
      if (!operator_user_.empty()) cleanup.run("DROP USER IF EXISTS `" + operator_user_ + "`");
    } catch (...) {
      // Reported by the test that failed; the names are never reused.
    }
  }

  std::string dsn_;
  ClickHouseAdmin root_;
  std::string name_;
  std::string username_;
  std::string password_;
  std::string operator_user_;
  std::string operator_dsn_;
  bool created_ = false;
};

}  // namespace sde::live
