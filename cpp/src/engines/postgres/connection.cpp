#include "engines/postgres/connection.hpp"

#include <array>
#include <string>
#include <string_view>
#include <utility>

#include "sde/errors.hpp"

namespace sde::detail::postgres {

namespace {

std::string trimmed(std::string_view text) {
  while (!text.empty() && (text.back() == '\n' || text.back() == ' ')) text.remove_suffix(1);
  return std::string(text);
}

}  // namespace

std::string server_message(std::string_view libpq_text) {
  // libpq writes "ERROR:  message\nDETAIL:  ...\n"; psycopg drops the severity of the first line.
  for (std::string_view severity : {"ERROR:  ", "FATAL:  ", "PANIC:  ", "WARNING:  "}) {
    if (libpq_text.starts_with(severity)) {
      libpq_text.remove_prefix(severity.size());
      break;
    }
  }
  return trimmed(libpq_text);
}

Connection::Connection(const std::string& dsn) {
  // Processed in order and the last value wins: our default timeout first, so the DSN's own
  // overrides it; the encoding last, so nothing overrides it.
  const std::string timeout = std::to_string(kConnectTimeoutSeconds);
  const std::array<const char*, 4> keywords = {"connect_timeout", "dbname", "client_encoding",
                                               nullptr};
  const std::array<const char*, 4> values = {timeout.c_str(), dsn.c_str(), "UTF8", nullptr};
  connection_ = PQconnectdbParams(keywords.data(), values.data(), 1);
  if (connection_ == nullptr) {
    throw EngineError("could not connect to PostgreSQL: connection failed: out of memory");
  }
  if (PQstatus(connection_) != CONNECTION_OK) {
    const std::string reason = trimmed(PQerrorMessage(connection_));
    PQfinish(connection_);
    connection_ = nullptr;
    throw EngineError("could not connect to PostgreSQL: connection failed: " + reason);
  }
}

Connection::~Connection() {
  if (connection_ != nullptr) PQfinish(connection_);
}

Result Connection::execute(const std::string& sql,
                           const std::vector<std::optional<std::string>>& parameters) {
  // psycopg's words for a connection already gone, before anything is sent on it.
  if (lost()) throw ServerError("the connection is closed", "");
  std::vector<const char*> texts;
  std::vector<int> lengths;
  texts.reserve(parameters.size());
  lengths.reserve(parameters.size());
  for (const std::optional<std::string>& parameter : parameters) {
    texts.push_back(parameter ? parameter->c_str() : nullptr);
    lengths.push_back(parameter ? static_cast<int>(parameter->size()) : 0);
  }
  Result result(PQexecParams(connection_, sql.c_str(), static_cast<int>(parameters.size()),
                             nullptr, texts.data(), lengths.data(), nullptr, 0));
  const ExecStatusType status = PQresultStatus(result.raw());
  if (status != PGRES_COMMAND_OK && status != PGRES_TUPLES_OK) {
    const char* reported = PQresultErrorMessage(result.raw());
    std::string message = server_message(reported != nullptr && *reported != '\0'
                                             ? std::string_view(reported)
                                             : std::string_view(PQerrorMessage(connection_)));
    // When the server ended the connection it said why first ("FATAL:  terminating connection due
    // to administrator command"), and libpq keeps that on the connection rather than the result.
    // psycopg reports the server's line, so this does.
    if (lost()) {
      const std::string_view said = PQerrorMessage(connection_);
      for (std::string_view severity : {"FATAL:  ", "PANIC:  "}) {
        if (said.starts_with(severity)) {
          message = std::string(said.substr(severity.size(), said.find('\n') - severity.size()));
          break;
        }
      }
    }
    const char* sqlstate = PQresultErrorField(result.raw(), PG_DIAG_SQLSTATE);
    throw ServerError(std::move(message), sqlstate != nullptr ? sqlstate : "");
  }
  return result;
}

namespace {
#include "engines/postgres/sqlstate.inc"
}  // namespace

std::string_view ServerError::class_name() const noexcept {
  for (const auto& [code, name] : kExact) {
    if (code == sqlstate_) return name;
  }
  // psycopg's own fallback: the class of the first two characters, then of the first, then
  // DatabaseError; a statement that failed without a SQLSTATE is libpq's, an OperationalError.
  if (sqlstate_.empty()) return "OperationalError";
  for (const std::size_t width : {std::size_t{2}, std::size_t{1}}) {
    for (const auto& [prefix, name] : kPrefix) {
      if (prefix == std::string_view(sqlstate_).substr(0, width)) return name;
    }
  }
  return "DatabaseError";
}

std::string Connection::parameter_status(const char* name) const {
  const char* value = PQparameterStatus(connection_, name);
  return value != nullptr ? value : "";
}

}  // namespace sde::detail::postgres
