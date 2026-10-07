/// The orderbook protocol's pure parts, read as the engine's own Python client reads them - the
/// client the reference talks to its server through. Every expected outcome was captured from that
/// client at the pinned engine commit: `_parse_tcp_response` for an answer, `_parse_status_fields`
/// and `status()` for STATUS, and the AUTH digest `_authenticate` computes.

#include <map>
#include <set>
#include <string>
#include <vector>

#include <gtest/gtest.h>

#include "engines/orderbook/wire.hpp"

namespace {

namespace ob = sde::detail::orderbook;

struct AnswerCase {
  const char* raw;
  bool error;
  const char* message;
  std::vector<std::string> header;
  std::vector<std::vector<std::string>> rows;
};

const std::vector<AnswerCase>& answer_cases() {
  static const std::vector<AnswerCase> all = {
    {"ERR OB_ERR_NOT_FOUND: symbol 'X' exchange 'Y' not found\n", true, "OB_ERR_NOT_FOUND: symbol 'X' exchange 'Y' not found", {}, {}},
    {"ERR \n", true, "", {}, {}},
    {"ERR a\n\n\n", true, "a", {}, {}},
    {"ERR tab\there\n", true, "tab\there", {}, {}},
    {"PONG\n", false, "", {}, {}},
    {"PONGish\n", false, "", {}, {}},
    {"OK\n\n", false, "", {}, {}},
    {"OK\ntimestamp_ns\tprice\n1\t2\n\n", false, "", {"timestamp_ns", "price"}, {{"1", "2"}}},
    {"OK\n\n\nheader\nrow1\n\nrow2\n\n", false, "", {"header"}, {{"row1"}}},
    {"OK\na\tb\t\n\t\n\n", false, "", {"a", "b", ""}, {{"", ""}}},
    {"OK AUTH x\n\n", true, "unexpected response: OK AUTH x\n\n", {}, {}},
    {" OK \n", false, "", {}, {}},
    {"OK\n", false, "", {}, {}},
    {"OK", false, "", {}, {}},
    {"STANDALONE\n", true, "unexpected response: STANDALONE\n", {}, {}},
    {"nothing\n", true, "unexpected response: nothing\n", {}, {}},
    {"xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\n", true, "unexpected response: xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", {}, {}},
    {"\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9", true, "unexpected response: \xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9\xc3\xa9", {}, {}},
    {"OK\nh\nr\nr\nr\n\n", false, "", {"h"}, {{"r"}, {"r"}, {"r"}}},
  };
  return all;
}

struct StatusCase {
  const char* raw;
  std::map<std::string, std::string> fields;
  bool readable;
  std::set<std::string> capabilities;
  const char* failure;  ///< `Class: message`, as the client raises it
};

const std::vector<StatusCase>& status_cases() {
  static const std::vector<StatusCase> all = {
    {"OK\nsessions\tqueries\tinserts\n1\t0\t0\ncapabilities: insert_event_time,strict_args,backup\nreplicas: 0\nrole: standalone\n\n", {{"capabilities", "insert_event_time,strict_args,backup"}, {"replicas", "0"}, {"role", "standalone"}}, true, {"backup", "insert_event_time", "strict_args"}, ""},
    {"OK\ncapabilities:a,,b,\n\n", {{"capabilities", "a,,b,"}}, true, {"a", "b"}, ""},
    {"OK\n  capabilities :  x  \n\n", {{"capabilities", "x"}}, true, {"x"}, ""},
    {"OK\ncap abilities: x\ncapabilities: y\n\n", {{"capabilities", "y"}}, true, {"y"}, ""},
    {"OK\n[section]\ncapabilities: z\n\n", {}, true, {}, ""},
    {"OK\ncapabilities: 0_1\n\n", {{"capabilities", "1"}}, true, {"1"}, ""},
    {"OK\ncapabilities:\n\n", {{"capabilities", ""}}, true, {}, ""},
    {"OK\nnothing here\n\n", {}, true, {}, ""},
    {"OK\ncapabilities: one\ncapabilities: two\n\n", {{"capabilities", "two"}}, true, {"two"}, ""},
    {"OK\nsessions\tqueries\tinserts\nx\t0\t0\ncapabilities: a\n\n", {{"capabilities", "a"}}, false, {}, "ValueError: invalid literal for int() with base 10: 'x'"},
    {"OK\nsessions\tqueries\n1\nx\n\n", {}, true, {}, ""},
    {"ERR busy\n", {}, false, {}, "OrderbookError: STATUS error: busy"},
    {"OK\n\xe3\x80\x80""capabilities: \xc3\xa9t\xc3\xa9 \n\n", {{"capabilities", "\xc3\xa9t\xc3\xa9"}}, true, {"\xc3\xa9t\xc3\xa9"}, ""},
    {"OK\nk: v: w\n:x\n\n", {{"k", "v: w"}}, true, {}, ""},
  };
  return all;
}

struct DigestCase {
  const char* identity;
  const char* secret;
  const char* nonce;
  const char* digest;
};

TEST(OrderbookWire, AnAnswerIsReadAsTheEnginesClientReadsIt) {
  for (const AnswerCase& expected : answer_cases()) {
    SCOPED_TRACE(expected.raw);
    const ob::Answer got = ob::parse_answer(expected.raw);
    EXPECT_EQ(got.error, expected.error);
    EXPECT_EQ(got.message, expected.message);
    EXPECT_EQ(got.header, expected.header);
    EXPECT_EQ(got.rows, expected.rows);
  }
}

TEST(OrderbookWire, StatusFieldsAndCapabilitiesAreReadAsTheEnginesClientReadsThem) {
  for (const StatusCase& expected : status_cases()) {
    SCOPED_TRACE(expected.raw);
    EXPECT_EQ(ob::status_fields(expected.raw), expected.fields);
    try {
      const std::set<std::string> got = ob::server_capabilities(expected.raw);
      EXPECT_TRUE(expected.readable);
      EXPECT_EQ(got, expected.capabilities);
    } catch (const ob::WireError& error) {
      EXPECT_FALSE(expected.readable);
      EXPECT_EQ(error.python_class() + ": " + error.what(), expected.failure);
    }
  }
}

TEST(OrderbookWire, TheAuthDigestIsTheServersHmacOverFiveFields) {
  const std::vector<DigestCase> cases = {
    {"desk", "k", "abc", "407330901c30148b420d9a88e941e17acc511f65983a8eda2013ced82daf9164"},
    {"sde-local", "s3cr+t", "0000000000000000000000000000000000000000000000000000000000000000", "436a9476a8435a66ff2559b0b81a852c3b4422af572d4f2fb9ed06ce57f7af2a"},
    {"\xc3\xbcn\xc3\xaf", "\xc5\x9b\xe2\x82\xac""cret", "ff", "27ab4a8c32c9f2979e563a20a230425f57848166ed3c9ed8b05299082bda4e69"},
  };
  for (const DigestCase& expected : cases) {
    SCOPED_TRACE(expected.identity);
    EXPECT_EQ(ob::auth_digest(expected.identity, expected.secret, expected.nonce), expected.digest);
  }
}

}  // namespace
