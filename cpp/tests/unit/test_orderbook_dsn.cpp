/// The orderbook engine's DSN, read as the reference reads it: `OrderbookEngine.from_dsn` and its
/// constructor, rule for rule and message for message. Every case's expected outcome was captured
/// from the reference, the reference's own test cases among them, with the edges Python's parsers
/// decide: a port of 0 or 00080, a scheme in capitals, a scoped IPv6 host, an escape that is not
/// UTF-8, and a timeout of `1_0`, `inf`, an Arabic-Indic digit, `1e400` or `5e-324` - or one ending
/// in U+001C to U+001F, which `str.isspace()` calls whitespace and `float()` does not strip.

#include <cmath>
#include <cstdlib>
#include <string>
#include <vector>

#include <gtest/gtest.h>

#include "engines/orderbook/dsn.hpp"
#include "sde/errors.hpp"

namespace {

using sde::detail::orderbook::parse_dsn;
using sde::detail::orderbook::Target;

struct Case {
  const char* dsn;
  const char* error;  ///< the reference's message, or null when it accepts
  const char* host;
  int port;
  const char* identity;
  const char* secret;
  bool tls;
  const char* ca;
  bool verify;
  const char* timeout;  ///< Python's repr of the float
};

const std::vector<Case> kCases = {
    {"orderbook://app%40desk:p%3Ass%20w@db.internal:9443?tls=on&ca=/etc/ca.pem&timeout=2.5", nullptr, "db.internal", 9443, "app@desk", "p:ss w", true, "/etc/ca.pem", true, "2.5"},
    {"orderbook://127.0.0.1:9090", nullptr, "127.0.0.1", 9090, nullptr, nullptr, false, nullptr, true, "10.0"},
    {"postgresql://a:b@h:1", "an orderbook DSN starts with orderbook://", nullptr, 0, nullptr, nullptr, false, nullptr, true, nullptr},
    {"orderbook://h", "an orderbook DSN names a host and a port: orderbook://host:port", nullptr, 0, nullptr, nullptr, false, nullptr, true, nullptr},
    {"orderbook://h:notaport", "the orderbook DSN's port is not a number", nullptr, 0, nullptr, nullptr, false, nullptr, true, nullptr},
    {"orderbook://h:1/var/data", "an orderbook DSN has no path or fragment; local mode takes data_dir", nullptr, 0, nullptr, nullptr, false, nullptr, true, nullptr},
    {"orderbook://app@h:1", "an orderbook DSN's identity needs its secret: identity:secret@host", nullptr, 0, nullptr, nullptr, false, nullptr, true, nullptr},
    {"orderbook://h:1?sslmode=require", "the orderbook DSN parameter 'sslmode' is unknown; it takes ['ca', 'timeout', 'tls', 'verify']", nullptr, 0, nullptr, nullptr, false, nullptr, true, nullptr},
    {"orderbook://h:1?tls=on&tls=off", "the orderbook DSN gives 'tls' twice", nullptr, 0, nullptr, nullptr, false, nullptr, true, nullptr},
    {"orderbook://h:1?tls=yes", "the orderbook DSN parameter 'tls' is on or off", nullptr, 0, nullptr, nullptr, false, nullptr, true, nullptr},
    {"orderbook://h:1?verify=off", "tls_verify=False without tls=True: there is no certificate to decline to check", nullptr, 0, nullptr, nullptr, false, nullptr, true, nullptr},
    {"orderbook://h:1?timeout=soon", "the orderbook DSN parameter 'timeout' is a number of seconds", nullptr, 0, nullptr, nullptr, false, nullptr, true, nullptr},
    {"orderbook://app:TOPSECRET@h:1?mode=x", "the orderbook DSN parameter 'mode' is unknown; it takes ['ca', 'timeout', 'tls', 'verify']", nullptr, 0, nullptr, nullptr, false, nullptr, true, nullptr},
    {"orderbook://h:0", nullptr, "h", 0, nullptr, nullptr, false, nullptr, true, "10.0"},
    {"ORDERBOOK://H.Example:1", nullptr, "h.example", 1, nullptr, nullptr, false, nullptr, true, "10.0"},
    {"orderbook://[::1]:9090", nullptr, "::1", 9090, nullptr, nullptr, false, nullptr, true, "10.0"},
    {"orderbook://[FE80::1%25ETH0]:1", nullptr, "fe80::1%25ETH0", 1, nullptr, nullptr, false, nullptr, true, "10.0"},
    {"orderbook://h:65536", "the orderbook DSN's port is not a number", nullptr, 0, nullptr, nullptr, false, nullptr, true, nullptr},
    {"orderbook://h:00080", nullptr, "h", 80, nullptr, nullptr, false, nullptr, true, "10.0"},
    {"orderbook://h:1#frag", "an orderbook DSN has no path or fragment; local mode takes data_dir", nullptr, 0, nullptr, nullptr, false, nullptr, true, nullptr},
    {"orderbook://h:1/", nullptr, "h", 1, nullptr, nullptr, false, nullptr, true, "10.0"},
    {"orderbook://h:1?", nullptr, "h", 1, nullptr, nullptr, false, nullptr, true, "10.0"},
    {"orderbook://h:1?&&tls=on&", nullptr, "h", 1, nullptr, nullptr, true, nullptr, true, "10.0"},
    {"orderbook://h:1?tls", "the orderbook DSN parameter 'tls' is on or off", nullptr, 0, nullptr, nullptr, false, nullptr, true, nullptr},
    {"orderbook://h:1?timeout=1_0", nullptr, "h", 1, nullptr, nullptr, false, nullptr, true, "10.0"},
    {"orderbook://h:1?timeout=inf", nullptr, "h", 1, nullptr, nullptr, false, nullptr, true, "inf"},
    {"orderbook://h:1?timeout=nan", "timeout is a positive number of seconds", nullptr, 0, nullptr, nullptr, false, nullptr, true, nullptr},
    {"orderbook://h:1?timeout=%20%202.5%20", nullptr, "h", 1, nullptr, nullptr, false, nullptr, true, "2.5"},
    {"orderbook://h:1?timeout=%D9%A3", nullptr, "h", 1, nullptr, nullptr, false, nullptr, true, "3.0"},
    {"orderbook://h:1?timeout=1e400", nullptr, "h", 1, nullptr, nullptr, false, nullptr, true, "inf"},
    {"orderbook://h:1?timeout=-1", "timeout is a positive number of seconds", nullptr, 0, nullptr, nullptr, false, nullptr, true, nullptr},
    {"orderbook://h:1?timeout=0", "timeout is a positive number of seconds", nullptr, 0, nullptr, nullptr, false, nullptr, true, nullptr},
    {"orderbook://h:1?timeout=1__0", "the orderbook DSN parameter 'timeout' is a number of seconds", nullptr, 0, nullptr, nullptr, false, nullptr, true, nullptr},
    {"orderbook://h:1?timeout=0x10", "the orderbook DSN parameter 'timeout' is a number of seconds", nullptr, 0, nullptr, nullptr, false, nullptr, true, nullptr},
    {"orderbook://a+b:s@h:1", nullptr, "h", 1, "a+b", "s", false, nullptr, true, "10.0"},
    {"orderbook://a%FF:s@h:1", nullptr, "h", 1, "a\xef\xbf\xbd", "s", false, nullptr, true, "10.0"},
    {"orderbook://:s@h:1", "an orderbook DSN's identity needs its secret: identity:secret@host", nullptr, 0, nullptr, nullptr, false, nullptr, true, nullptr},
    {"orderbook://a:@h:1", "auth is (identity, secret): two non-empty strings, the identity without whitespace, as in the server's --auth-secret-file", nullptr, 0, nullptr, nullptr, false, nullptr, true, nullptr},
    {"orderbook://a%20b:s@h:1", "auth is (identity, secret): two non-empty strings, the identity without whitespace, as in the server's --auth-secret-file", nullptr, 0, nullptr, nullptr, false, nullptr, true, nullptr},
    {"orderbook://h:1?ca=", "tls_ca_file without tls=True verifies nothing: the connection would be plain text", nullptr, 0, nullptr, nullptr, false, nullptr, true, nullptr},
    {"orderbook://h:1?tls=on&ca=", nullptr, "h", 1, nullptr, nullptr, true, "", true, "10.0"},
    {"orderbook://h:1?tls=on&verify=off&ca=/x", nullptr, "h", 1, nullptr, nullptr, true, "/x", false, "10.0"},
    {"  orderbook://h:1", nullptr, "h", 1, nullptr, nullptr, false, nullptr, true, "10.0"},
    {"orderbook://h\t:1", nullptr, "h", 1, nullptr, nullptr, false, nullptr, true, "10.0"},
    {"orderbook://h:1?Tls=on", "the orderbook DSN parameter 'Tls' is unknown; it takes ['ca', 'timeout', 'tls', 'verify']", nullptr, 0, nullptr, nullptr, false, nullptr, true, nullptr},
    {"orderbook://h:1?tls=ON", "the orderbook DSN parameter 'tls' is on or off", nullptr, 0, nullptr, nullptr, false, nullptr, true, nullptr},
    {"orderbook://[::1:1", "the orderbook DSN's port is not a number", nullptr, 0, nullptr, nullptr, false, nullptr, true, nullptr},
    {"orderbook://x[::1]:1", "the orderbook DSN's port is not a number", nullptr, 0, nullptr, nullptr, false, nullptr, true, nullptr},
    {"orderbook://[1.2.3.4]:1", "the orderbook DSN's port is not a number", nullptr, 0, nullptr, nullptr, false, nullptr, true, nullptr},
    {"orderbook://h:1?timeout=+Infinity", nullptr, "h", 1, nullptr, nullptr, false, nullptr, true, "inf"},
    {"orderbook://u:p@q@h:1", nullptr, "h", 1, "u", "p@q", false, nullptr, true, "10.0"},
    {"orderbook://h:+1", "the orderbook DSN's port is not a number", nullptr, 0, nullptr, nullptr, false, nullptr, true, nullptr},
    {"orderbook://h: 1", "the orderbook DSN's port is not a number", nullptr, 0, nullptr, nullptr, false, nullptr, true, nullptr},
    {"orderbook://h:1:2", "the orderbook DSN's port is not a number", nullptr, 0, nullptr, nullptr, false, nullptr, true, nullptr},
    {"orderbook:h:1", "an orderbook DSN names a host and a port: orderbook://host:port", nullptr, 0, nullptr, nullptr, false, nullptr, true, nullptr},
    {"orderbook:///h:1", "an orderbook DSN names a host and a port: orderbook://host:port", nullptr, 0, nullptr, nullptr, false, nullptr, true, nullptr},
    {"orderbook://h:1?a=1=2", "the orderbook DSN parameter 'a' is unknown; it takes ['ca', 'timeout', 'tls', 'verify']", nullptr, 0, nullptr, nullptr, false, nullptr, true, nullptr},
    {"orderbook://h:1?%74ls=on", nullptr, "h", 1, nullptr, nullptr, true, nullptr, true, "10.0"},
    {"orderbook://h:1?tls=o%6E", nullptr, "h", 1, nullptr, nullptr, true, nullptr, true, "10.0"},
    {"orderbook://\xe2\x84\xaa:1", nullptr, "k", 1, nullptr, nullptr, false, nullptr, true, "10.0"},
    {"orderbook://h:1?timeout=%1C1", "the orderbook DSN parameter 'timeout' is a number of seconds", nullptr, 0, nullptr, nullptr, false, nullptr, true, nullptr},
    {"orderbook://h:1?timeout=1%1F", "the orderbook DSN parameter 'timeout' is a number of seconds", nullptr, 0, nullptr, nullptr, false, nullptr, true, nullptr},
    {"orderbook://h:1?timeout=%C2%851", nullptr, "h", 1, nullptr, nullptr, false, nullptr, true, "1.0"},
    {"orderbook://h:1?timeout=%E3%80%801%E3%80%80", nullptr, "h", 1, nullptr, nullptr, false, nullptr, true, "1.0"},
    {"orderbook://h:1?timeout=%0B1%0C", nullptr, "h", 1, nullptr, nullptr, false, nullptr, true, "1.0"},
    {"orderbook://h:1?timeout=1%7F", "the orderbook DSN parameter 'timeout' is a number of seconds", nullptr, 0, nullptr, nullptr, false, nullptr, true, nullptr},
    {"orderbook://h:1?timeout=%E2%80%8B1", "the orderbook DSN parameter 'timeout' is a number of seconds", nullptr, 0, nullptr, nullptr, false, nullptr, true, nullptr},
    {"orderbook://h:1?timeout=1_0.0_1e1_0", nullptr, "h", 1, nullptr, nullptr, false, nullptr, true, "100100000000.0"},
    {"orderbook://h:1?timeout=1_.0", "the orderbook DSN parameter 'timeout' is a number of seconds", nullptr, 0, nullptr, nullptr, false, nullptr, true, nullptr},
    {"orderbook://h:1?timeout=0x1p3", "the orderbook DSN parameter 'timeout' is a number of seconds", nullptr, 0, nullptr, nullptr, false, nullptr, true, nullptr},
    {"orderbook://h:1?timeout=infinit", "the orderbook DSN parameter 'timeout' is a number of seconds", nullptr, 0, nullptr, nullptr, false, nullptr, true, nullptr},
    {"orderbook://h:1?timeout=%F0%9D%9F%8F", nullptr, "h", 1, nullptr, nullptr, false, nullptr, true, "1.0"},
    {"orderbook://h:1?timeout=%C2%B2", "the orderbook DSN parameter 'timeout' is a number of seconds", nullptr, 0, nullptr, nullptr, false, nullptr, true, nullptr},
    {"orderbook://h:1?timeout=4.9e-324", nullptr, "h", 1, nullptr, nullptr, false, nullptr, true, "5e-324"},
    {"orderbook://h:1?timeout=2.4e-324", "timeout is a positive number of seconds", nullptr, 0, nullptr, nullptr, false, nullptr, true, nullptr},
    {"orderbook://h:1?timeout=-0", "timeout is a positive number of seconds", nullptr, 0, nullptr, nullptr, false, nullptr, true, nullptr},
    {"orderbook://u%1C:p@h:1", "auth is (identity, secret): two non-empty strings, the identity without whitespace, as in the server's --auth-secret-file", nullptr, 0, nullptr, nullptr, false, nullptr, true, nullptr},
    {"orderbook://u%E2%80%8B:p@h:1", nullptr, "h", 1, "u\xe2\x80\x8b", "p", false, nullptr, true, "10.0"},
};

TEST(OrderbookDsn, EveryCaseIsReadAsTheReferenceReadsIt) {
  for (const Case& expected : kCases) {
    SCOPED_TRACE(expected.dsn);
    if (expected.error != nullptr) {
      try {
        (void)parse_dsn(expected.dsn);
        ADD_FAILURE() << "accepted";
      } catch (const sde::EngineError& error) {
        EXPECT_EQ(std::string(error.what()), expected.error);
      }
      continue;
    }
    const Target got = parse_dsn(expected.dsn);
    EXPECT_EQ(got.host, expected.host);
    EXPECT_EQ(got.port, expected.port);
    if (expected.identity == nullptr) {
      EXPECT_FALSE(got.auth.has_value());
    } else if (got.auth.has_value()) {
      EXPECT_EQ(got.auth->first, expected.identity);
      EXPECT_EQ(got.auth->second, expected.secret);
    } else {
      ADD_FAILURE() << "no credentials";
    }
    EXPECT_EQ(got.tls, expected.tls);
    EXPECT_EQ(got.ca.value_or("<none>"), expected.ca == nullptr ? "<none>" : expected.ca);
    EXPECT_EQ(got.verify, expected.verify);
    // Python's repr is the shortest text that reads back as the same double: compared as doubles.
    EXPECT_EQ(got.timeout, std::strtod(expected.timeout, nullptr)) << expected.timeout;
  }
}

TEST(OrderbookDsn, TheSecretAppearsInNoRefusal) {
  for (const char* dsn : {"orderbook://app:TOPSECRET@h:1?mode=x", "orderbook://app:TOPSECRET@h:x",
                          "orderbook://app:TOPSECRET@h:1/path", "orderbook://app:TOPSECRET@h:1?tls=yes",
                          "orderbook://app:TOPSECRET@h:1?timeout=soon", "orderbook://ap p:TOPSECRET@h:1"}) {
    SCOPED_TRACE(dsn);
    try {
      (void)parse_dsn(dsn);
      ADD_FAILURE() << "accepted";
    } catch (const sde::EngineError& error) {
      EXPECT_EQ(std::string(error.what()).find("TOPSECRET"), std::string::npos);
    }
  }
}

TEST(OrderbookDsn, TextThatIsNotUtf8IsNotADsn) {
  // Unreachable in the reference, whose DSN is a str: refused before any rule.
  try {
    (void)parse_dsn("orderbook://h\xff:1");
    ADD_FAILURE() << "accepted";
  } catch (const sde::EngineError& error) {
    EXPECT_EQ(std::string(error.what()), "an orderbook DSN is a string");
  }
}

}  // namespace
