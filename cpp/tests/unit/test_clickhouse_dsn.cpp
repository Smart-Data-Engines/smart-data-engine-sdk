/// The ClickHouse connection URI, with nothing opened: the shared vectors every library runs
/// (`testdata/clickhouse-dsns.json`), the reference's message for each rule, and addresses written
/// as Python's `ipaddress` writes them. The rest of the agreement with the reference is a
/// differential run, recorded in `docs/implementing.md`.

#include <cmath>
#include <filesystem>
#include <fstream>
#include <optional>
#include <sstream>
#include <string>
#include <utility>
#include <vector>

#include <gtest/gtest.h>

#include "engines/clickhouse/dsn.hpp"
#include "sde/errors.hpp"
#include "sde/json.hpp"

namespace {

using sde::detail::clickhouse::parse_dsn;
using sde::detail::clickhouse::Target;

sde::Json shared_vectors() {
  std::ifstream in(std::filesystem::path(SDE_TESTDATA_DIR) / "clickhouse-dsns.json",
                   std::ios::binary);
  std::ostringstream text;
  text << in.rdbuf();
  return sde::parse_json(text.str());
}

std::string refusal(std::string_view dsn) {
  try {
    (void)parse_dsn(dsn);
  } catch (const sde::EngineError& error) {
    return error.what();
  }
  return "accepted";
}

TEST(ClickHouseDsn, EverySharedVectorParsesOrIsRefusedWithoutEchoingIt) {
  const sde::Json document = shared_vectors();
  ASSERT_EQ(sde::dump_json(*document.find("protocol")), "1");
  const sde::Json::Array& cases = document.find("cases")->as_array();
  ASSERT_FALSE(cases.empty());
  int accepted = 0;
  int refused = 0;
  for (const sde::Json& entry : cases) {
    const std::string& id = entry.find("id")->as_string();
    const std::string& dsn = entry.find("dsn")->as_string();
    SCOPED_TRACE(id);
    const sde::Json* reject = entry.find("reject");
    if (reject != nullptr && reject->as_bool()) {
      const std::string message = refusal(dsn);
      EXPECT_NE(message, "accepted");
      EXPECT_EQ(message.find("synthetic-secret"), std::string::npos) << message;
      EXPECT_EQ(message.find(dsn), std::string::npos) << message;
      ++refused;
      continue;
    }
    const Target parsed = parse_dsn(dsn);
    const sde::Json& expected = *entry.find("expected");
    EXPECT_EQ(parsed.host, expected.find("host")->as_string());
    EXPECT_EQ(sde::dump_json(*expected.find("port")), std::to_string(parsed.port));
    EXPECT_EQ(parsed.username, expected.find("username")->as_string());
    EXPECT_EQ(parsed.password, expected.find("password")->as_string());
    EXPECT_EQ(parsed.database, expected.find("database")->as_string());
    EXPECT_EQ(parsed.secure, expected.find("secure")->as_bool());
    const sde::Json& ca = *expected.find("ca_cert");
    EXPECT_EQ(parsed.ca_cert, ca.is_null() ? std::nullopt : std::optional(ca.as_string()));
    EXPECT_DOUBLE_EQ(parsed.connect_timeout,
                     std::stod(sde::dump_json(*expected.find("connect_timeout"))));
    EXPECT_DOUBLE_EQ(parsed.send_receive_timeout,
                     std::stod(sde::dump_json(*expected.find("send_receive_timeout"))));
    EXPECT_EQ(parsed.interface(), parsed.secure ? "https" : "http");
    ++accepted;
  }
  EXPECT_GT(accepted, 0);
  EXPECT_GT(refused, 0);
}

TEST(ClickHouseDsn, EachRuleIsRefusedInTheReferencesWords) {
  // Every message is the reference's, for the first rule the input breaks, in its order.
  const std::vector<std::pair<std::string, std::string>> cases = {
      {"", "Provide a ClickHouse connection URI"},
      {"clickhouse://h/d b", "Encode whitespace in connection URIs"},
      {"clickhouse://h/db\x1c", "Encode whitespace in connection URIs"},
      {"clickhouse://h/db\x01", "ClickHouse connection fields cannot contain control characters"},
      {"clickhouse://h/db#x",
       "ClickHouse URI fragments, raw backslashes and malformed percent escapes are refused"},
      {"clickhouse://h/db%zz",
       "ClickHouse URI fragments, raw backslashes and malformed percent escapes are refused"},
      {"postgres://h/db", "Unsupported ClickHouse URI scheme or authority"},
      {"clickhouse:h/db", "Unsupported ClickHouse URI scheme or authority"},
      {"clickhouse://u@:8123/db", "ClickHouse URI needs one unambiguous host"},
      {"clickhouse://a@b@h/db", "ClickHouse URI needs one unambiguous host"},
      {"clickhouse://[::1]x/db", "Invalid ClickHouse URI encoding, authority or connection setting"},
      {"clickhouse://::1/db", "ClickHouse URI needs one unambiguous host"},
      {"clickhouse://h::1/db", "Enclose IPv6 hosts in URI brackets"},
      {"clickhouse://h:/db", "ClickHouse port must be an explicit positive integer"},
      {"clickhouse://h:0/db", "ClickHouse port is outside the valid range"},
      {"clickhouse://h:65536/db", "ClickHouse port is outside the valid range"},
      {"clickhouse://h\xc3\xa9/db", "ClickHouse host must be ASCII DNS or an IP address"},
      {"clickhouse://0x7f.1/db", "Legacy numeric IPv4 forms are not supported"},
      {"clickhouse://1.2.3.04/db", "Legacy numeric IPv4 forms are not supported"},
      {"clickhouse://-h/db", "ClickHouse host is not a valid DNS name or IP address"},
      {"clickhouse://:p@h/db", "ClickHouse username must be nonempty and contain no colon"},
      {"clickhouse://u%3Ax@h/db", "ClickHouse username must be nonempty and contain no colon"},
      {"clickhouse://u%01@h/db", "ClickHouse connection fields cannot contain control characters"},
      {"clickhouse://h", "ClickHouse URI needs one explicit database path segment"},
      {"clickhouse://h/a/b", "ClickHouse URI needs one explicit database path segment"},
      {"clickhouse://h/a%2Fb", "ClickHouse database must be a nonempty single segment"},
      {"clickhouse://h/..", "ClickHouse database must be a nonempty single segment"},
      {"clickhouse://h/db?x=1", "ClickHouse query parameters must be known and appear once"},
      {"clickhouse://h/db?secure=true&secure=true",
       "ClickHouse query parameters must be known and appear once"},
      {"clickhouse://h/db?secure=TRUE", "ClickHouse secure must be literal true or false"},
      {"clickhouse://h:8443/db",
       "Select TLS or explicit plain transport for an ambiguous ClickHouse port"},
      {"https://h/db?secure=false", "ClickHouse scheme and secure setting disagree"},
      {"https://h/db?verify=false",
       "ClickHouse verification must remain enabled; use literal true or omit verify"},
      {"http://h/db?verify=true", "ClickHouse TLS options cannot apply to plain HTTP"},
      {"https://h/db?ca_cert=ca.pem", "ClickHouse CA must be an absolute local file path"},
      {"http://h/db?connect_timeout=0", "ClickHouse timeout must be positive finite seconds"},
      {"http://h/db?send_receive_timeout=1e999",
       "ClickHouse timeout must be positive finite seconds"},
      {"http://h/db?connect_timeout=1&", "Invalid ClickHouse URI encoding, authority or connection setting"},
      {"clickhouse://h/db?secure=%C3%28",
       "Invalid ClickHouse URI encoding, authority or connection setting"},
      {"clickhouse://[1.2.3.4]/db",
       "Invalid ClickHouse URI encoding, authority or connection setting"},
      {"clickhouse://h\xff/db", "Invalid ClickHouse URI encoding, authority or connection setting"},
  };
  for (const auto& [dsn, message] : cases) {
    EXPECT_EQ(refusal(dsn), message) << dsn;
  }
}

TEST(ClickHouseDsn, AHostIsWrittenAsTheReferenceWritesIt) {
  // Python's full lowercase: the Kelvin sign is a k to it, and so this is the host `k`.
  EXPECT_EQ(parse_dsn("clickhouse://\xe2\x84\xaa/db").host, "k");
  EXPECT_EQ(parse_dsn("clickhouse://Example.COM./db").host, "example.com.");
  EXPECT_EQ(parse_dsn("clickhouse://[FE80::A]:9000/db").host, "fe80::a");
  EXPECT_EQ(parse_dsn("clickhouse://[v1.x]/db").host, "v1.x");
  const std::vector<std::pair<std::string, std::string>> addresses = {
      {"::ffff:1.2.3.4", "::ffff:102:304"}, {"::1.2.3.4", "::102:304"},
      {"0:0:0:0:0:0:0:1", "::1"},           {"1:0:0:2:0:0:0:3", "1:0:0:2::3"},
      {"1:0:2:0:3:0:4:0", "1:0:2:0:3:0:4:0"}, {"1:2:3:4:5:6:7::", "1:2:3:4:5:6:7:0"},
      {"::1:2:3:4:5:6:7", "0:1:2:3:4:5:6:7"}, {"1::2:3:4:5:6:7", "1:0:2:3:4:5:6:7"},
      {"1:0:0:2:0:0:3:4", "1::2:0:0:3:4"},   {"::", "::"},
  };
  for (const auto& [text, written] : addresses) {
    EXPECT_EQ(sde::detail::clickhouse::ipv6_text(text), written) << text;
  }
  for (const std::string text : {"1:2:3:4:5:6:7:8:9", "1::2::3", ":1::", "12345::", "::ffff:01.2.3.4",
                                 "1:2:3:4:5:6:7:8::", "1.2.3.4"}) {
    EXPECT_EQ(sde::detail::clickhouse::ipv6_text(text), std::nullopt) << text;
  }
}

TEST(ClickHouseDsn, TheDefaultsAndTheOptions) {
  const Target plain = parse_dsn("clickhouse://localhost/db");
  EXPECT_EQ(plain.username, "default");
  EXPECT_EQ(plain.password, "");
  EXPECT_EQ(plain.port, 8123);
  EXPECT_FALSE(plain.secure);
  EXPECT_DOUBLE_EQ(plain.connect_timeout, 10.0);
  EXPECT_DOUBLE_EQ(plain.send_receive_timeout, 15.0);
  const Target chosen = parse_dsn(
      "clickhouse://u:p%40ss@[::1]:9440/my%20db?secure=true&verify=true&ca_cert=%2Fetc%2Fca.pem"
      "&connect_timeout=.5&send_receive_timeout=2e1");
  EXPECT_EQ(chosen.host, "::1");
  EXPECT_EQ(chosen.port, 9440);
  EXPECT_EQ(chosen.password, "p@ss");
  EXPECT_EQ(chosen.database, "my db");
  EXPECT_TRUE(chosen.secure);
  EXPECT_EQ(chosen.ca_cert, "/etc/ca.pem");
  EXPECT_DOUBLE_EQ(chosen.connect_timeout, 0.5);
  EXPECT_DOUBLE_EQ(chosen.send_receive_timeout, 20.0);
}

}  // namespace
