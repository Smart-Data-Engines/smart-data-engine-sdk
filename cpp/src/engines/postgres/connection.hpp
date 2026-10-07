#pragma once

/// One libpq connection and its results, owned and freed here, so that nothing outside the
/// PostgreSQL adapter sees a libpq type.

#include <libpq-fe.h>

#include <memory>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

namespace sde::detail::postgres {

/// Seconds opening a connection may take when the DSN does not say: the reference's default.
inline constexpr int kConnectTimeoutSeconds = 10;

/// A statement's result, freed with it.
class Result {
 public:
  explicit Result(PGresult* result) noexcept : result_(result) {}

  [[nodiscard]] int rows() const noexcept { return PQntuples(result_.get()); }
  [[nodiscard]] int columns() const noexcept { return PQnfields(result_.get()); }
  [[nodiscard]] std::string_view name(int column) const noexcept {
    return PQfname(result_.get(), column);
  }
  [[nodiscard]] unsigned type(int column) const noexcept { return PQftype(result_.get(), column); }
  [[nodiscard]] bool is_null(int row, int column) const noexcept {
    return PQgetisnull(result_.get(), row, column) == 1;
  }
  [[nodiscard]] std::string_view text(int row, int column) const noexcept {
    return {PQgetvalue(result_.get(), row, column),
            static_cast<std::size_t>(PQgetlength(result_.get(), row, column))};
  }
  /// The command tag (`INSERT 0 1`, `COMMIT`, `ROLLBACK`).
  [[nodiscard]] std::string_view tag() const noexcept { return PQcmdStatus(result_.get()); }
  [[nodiscard]] PGresult* raw() const noexcept { return result_.get(); }

 private:
  struct Clear {
    void operator()(PGresult* result) const noexcept { PQclear(result); }
  };
  std::unique_ptr<PGresult, Clear> result_;
};

/// The server's message as psycopg writes it: libpq's text without its severity prefix, with the
/// DETAIL, CONTEXT and LINE parts the server sent, and no trailing newline.
[[nodiscard]] std::string server_message(std::string_view libpq_text);

class Connection {
 public:
  /// Opens a connection. The DSN is expanded after a default `connect_timeout`, so a timeout the
  /// DSN sets wins, and `client_encoding=UTF8` comes after it, so text is UTF-8 whatever the DSN
  /// says. `EngineError` when it cannot be opened, with libpq's reason.
  explicit Connection(const std::string& dsn);
  ~Connection();
  Connection(const Connection&) = delete;
  Connection& operator=(const Connection&) = delete;

  /// Runs one statement with text parameters - an empty one is SQL NULL - and the server inferring
  /// every parameter's type. Throws `ServerError` with the server's message when it fails.
  Result execute(const std::string& sql,
                 const std::vector<std::optional<std::string>>& parameters = {});

  [[nodiscard]] PGTransactionStatusType transaction_status() const noexcept {
    return PQtransactionStatus(connection_);
  }
  /// The connection is gone: the server closed it, or the network did.
  [[nodiscard]] bool lost() const noexcept { return PQstatus(connection_) == CONNECTION_BAD; }
  /// A setting the server reports on its own (`DateStyle`, `server_version`, ...), or empty.
  [[nodiscard]] std::string parameter_status(const char* name) const;

 private:
  PGconn* connection_ = nullptr;
};

/// A statement the server refused, with its message as psycopg writes it and its SQLSTATE.
class ServerError : public std::exception {
 public:
  ServerError(std::string message, std::string sqlstate)
      : message_(std::move(message)), sqlstate_(std::move(sqlstate)) {}
  [[nodiscard]] const char* what() const noexcept override { return message_.c_str(); }
  [[nodiscard]] const std::string& sqlstate() const noexcept { return sqlstate_; }
  /// The class psycopg raises for this SQLSTATE (`UniqueViolation`), which the reference logs.
  [[nodiscard]] std::string_view class_name() const noexcept;

 private:
  std::string message_;
  std::string sqlstate_;
};

}  // namespace sde::detail::postgres
