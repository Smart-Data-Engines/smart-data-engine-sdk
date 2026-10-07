#include "engines/orderbook/dsn.hpp"

#include <cmath>
#include <map>
#include <string>
#include <vector>

#include "python_compat.hpp"
#include "python_url.hpp"
#include "sde/errors.hpp"
#include "unicode_internal.hpp"

namespace sde::detail::orderbook {

namespace {

using python_url::PythonValueError;

/// The query parameters this DSN takes, sorted as the reference lists them.
constexpr const char* kParameters = "['ca', 'timeout', 'tls', 'verify']";

bool known_parameter(const std::string& key) {
  return key == "tls" || key == "ca" || key == "verify" || key == "timeout";
}

std::optional<bool> switch_of(const std::string& value) {
  if (value == "on") return true;
  if (value == "off") return false;
  return std::nullopt;
}

}  // namespace

void check_target(const Target& target) {
  if (target.host.empty()) {
    throw EngineError(
        "give either a data directory, for in-process access through the shared library, or a "
        "host and port, for a running ob_tcp_server. Not both and not neither: the two are "
        "different deployments with different durability, and defaulting to one of them would "
        "pick a durability guarantee on the client's behalf.");
  }
  if (target.auth) {
    const auto& [identity, secret] = *target.auth;
    bool spaced = false;
    if (const auto code_points = decode_utf8(identity)) {
      for (const char32_t c : *code_points) spaced = spaced || python_space(c);
    }
    if (identity.empty() || secret.empty() || spaced) {
      throw EngineError(
          "auth is (identity, secret): two non-empty strings, the identity without whitespace, as "
          "in the server's --auth-secret-file");
    }
  }
  if (target.ca && !target.tls) {
    throw EngineError(
        "tls_ca_file without tls=True verifies nothing: the connection would be plain text");
  }
  if (!target.verify && !target.tls) {
    throw EngineError(
        "tls_verify=False without tls=True: there is no certificate to decline to check");
  }
  if (!(target.timeout > 0)) throw EngineError("timeout is a positive number of seconds");
}

Target parse_dsn(std::string_view dsn) {
  // A Python str holds only text: bytes that are not UTF-8 never reach the reference's parser.
  if (!decode_utf8(dsn)) throw EngineError("an orderbook DSN is a string");
  python_url::Split parts;
  std::optional<int> port;
  try {
    parts = python_url::urlsplit(dsn);
    port = python_url::port(parts.netloc);
  } catch (const PythonValueError&) {
    throw EngineError("the orderbook DSN's port is not a number");
  }
  if (parts.scheme != "orderbook") throw EngineError("an orderbook DSN starts with orderbook://");
  const std::optional<std::string> host = python_url::hostname(parts.netloc);
  if (!host || !port) {
    throw EngineError("an orderbook DSN names a host and a port: orderbook://host:port");
  }
  if ((!parts.path.empty() && parts.path != "/") || !parts.fragment.empty()) {
    throw EngineError("an orderbook DSN has no path or fragment; local mode takes data_dir");
  }
  const python_url::UserInfo user = python_url::userinfo(parts.netloc);
  const std::optional<std::string> identity =
      user.username ? std::optional<std::string>(
                          python_url::unquote(*user.username, python_url::Errors::replace))
                    : std::nullopt;
  const std::optional<std::string> secret =
      user.password ? std::optional<std::string>(
                          python_url::unquote(*user.password, python_url::Errors::replace))
                    : std::nullopt;
  if (identity.has_value() != secret.has_value() || (identity && identity->empty())) {
    throw EngineError("an orderbook DSN's identity needs its secret: identity:secret@host");
  }
  std::map<std::string, std::string> options;
  for (const auto& [key, value] :
       python_url::parse_qsl(parts.query, true, false, python_url::Errors::replace)) {
    if (!known_parameter(key)) {
      throw EngineError("the orderbook DSN parameter " + python_repr(key) + " is unknown; it takes " +
                        kParameters);
    }
    if (options.contains(key)) {
      throw EngineError("the orderbook DSN gives " + python_repr(key) + " twice");
    }
    options[key] = value;
  }
  for (const char* key : {"tls", "verify"}) {
    if (const auto found = options.find(key); found != options.end() && !switch_of(found->second)) {
      throw EngineError(std::string("the orderbook DSN parameter ") + python_repr(std::string(key)) +
                        " is on or off");
    }
  }
  Target out;
  try {
    const auto timeout = options.find("timeout");
    out.timeout = timeout == options.end() ? kDefaultTimeoutSeconds
                                           : python_url::float_of(timeout->second);
  } catch (const PythonValueError&) {
    throw EngineError("the orderbook DSN parameter 'timeout' is a number of seconds");
  }
  out.host = *host;
  out.port = *port;
  if (identity && secret) out.auth = std::make_pair(*identity, *secret);
  out.tls = options.contains("tls") && *switch_of(options.at("tls"));
  if (const auto ca = options.find("ca"); ca != options.end()) out.ca = ca->second;
  out.verify = !options.contains("verify") || *switch_of(options.at("verify"));
  check_target(out);
  return out;
}

}  // namespace sde::detail::orderbook
