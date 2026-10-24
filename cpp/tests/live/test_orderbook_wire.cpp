/// The orderbook protocol as this library speaks it, against servers in this process that play a
/// script: the greeting, the framing of every kind of answer, PUSH lines in front of one, pipelined
/// answers split anywhere, an exchange that does not finish and the reason every later call gives,
/// the AUTH challenge, the silence that bounds every wait, and the words of each failure - the
/// engine's own Python client's, which the reference puts into its messages. Needs no DSN.

#include <chrono>
#include <string>
#include <thread>
#include <vector>

#include <gtest/gtest.h>

#include "engines/orderbook/dsn.hpp"
#include "engines/orderbook/wire.hpp"
#include "live/orderbook_fake.hpp"

namespace {

namespace ob = sde::detail::orderbook;
using Clock = std::chrono::steady_clock;
using sde::live::FakeOrderbook;
using sde::live::Peer;
using sde::live::ProtocolServer;
using std::chrono::milliseconds;

ob::Target target(int port, const std::string& query = "?timeout=2") {
  return ob::parse_dsn("orderbook://127.0.0.1:" + std::to_string(port) + query);
}

/// What opening a connection or one call on it raised: `Class: text`, or `accepted`.
template <typename Body>
std::string failure_of(const Body& body) {
  try {
    body();
  } catch (const ob::WireError& error) {
    return error.python_class() + ": " + error.what();
  }
  return "accepted";
}

double seconds_since(Clock::time_point start) {
  return std::chrono::duration<double>(Clock::now() - start).count();
}

/// A server that greets, then answers each line it reads from `answers`, in order.
ProtocolServer answering(std::vector<std::string> answers) {
  return ProtocolServer([answers](Peer& peer) {
    if (!peer.send(sde::live::kGreeting)) return;
    for (const std::string& answer : answers) {
      if (!peer.line()) return;
      if (!peer.send(answer)) return;
    }
    (void)peer.drain(milliseconds(200));
  });
}

TEST(OrderbookWire, AGreetingIsReadWhateverItsPiecesAndAnythingElseIsRefusedAsItArrives) {
  // In pieces, with the blank line split across two of them.
  ProtocolServer pieces([](Peer& peer) {
    for (const char* piece : {"O", "K ob_tcp", "_server v0.1.0\n", "\n"}) {
      if (!peer.send(piece)) return;
      std::this_thread::sleep_for(milliseconds(30));
    }
    if (peer.line()) (void)peer.send("PONG\n");
    (void)peer.drain(milliseconds(200));
  });
  ob::Connection connection(target(pieces.port()));
  EXPECT_EQ(connection.execute("PING"), "PONG\n");
  // Not an orderbook server: refused on its first bytes, where the engine's client waits for the
  // blank line until its timeout.
  ProtocolServer mail([](Peer& peer) {
    (void)peer.send("220 mail.example ESMTP\r\n");
    (void)peer.drain(milliseconds(3000));
  });
  const auto start = Clock::now();
  EXPECT_EQ(failure_of([&] { ob::Connection(target(mail.port())); }),
            "OrderbookError: unexpected greeting from 127.0.0.1:" + std::to_string(mail.port()) +
                ": 220 mail.example ESMTP\r\n");
  EXPECT_LT(seconds_since(start), 1.0);
}

TEST(OrderbookWire, AServerThatEndsOrFallsSilentBeforeItsGreetingIsSaidInTheClientsWords) {
  ProtocolServer closing([](Peer& peer) { peer.close(); });
  EXPECT_EQ(failure_of([&] { ob::Connection(target(closing.port())); }),
            "OrderbookError: Connection closed before banner");
  ProtocolServer silent([](Peer& peer) { (void)peer.drain(milliseconds(3000)); });
  const auto start = Clock::now();
  EXPECT_EQ(failure_of([&] { ob::Connection(target(silent.port(), "?timeout=0.3")); }),
            "TimeoutError: timed out");
  EXPECT_GE(seconds_since(start), 0.25);
  EXPECT_LT(seconds_since(start), 1.5);
}

TEST(OrderbookWire, EveryKindOfAnswerIsFramedAsTheClientFramesIt) {
  ProtocolServer server = answering({"ERR OB_ERR_NOT_FOUND: symbol 'A' exchange 'B' not found\n",
                                     "PONG\n", "STANDALONE\n",
                                     "OK\ntimestamp_ns\tprice\n1\t2\n3\t4\n\n", "OK\n\n",
                                     "OK AUTH desk\n\n"});
  ob::Connection connection(target(server.port()));
  EXPECT_EQ(connection.execute("SELECT 1"),
            "ERR OB_ERR_NOT_FOUND: symbol 'A' exchange 'B' not found\n");
  EXPECT_EQ(connection.execute("PING"), "PONG\n");
  EXPECT_EQ(connection.execute("ROLE"), "STANDALONE\n");
  EXPECT_EQ(connection.execute("SELECT 2"), "OK\ntimestamp_ns\tprice\n1\t2\n3\t4\n\n");
  EXPECT_EQ(connection.execute("FLUSH"), "OK\n\n");
  EXPECT_EQ(connection.execute("AUTH"), "OK AUTH desk\n\n");
}

TEST(OrderbookWire, PushedRowsInFrontOfAnAnswerAreSkippedEvenWhenOneArrivesInPieces) {
  ProtocolServer server([](Peer& peer) {
    if (!peer.send(sde::live::kGreeting) || !peer.line()) return;
    (void)peer.send("PUSH 1\t1000\t5\t1\t1\t0\t0\t7\nPUSH 2\t10");
    std::this_thread::sleep_for(milliseconds(50));
    (void)peer.send("00\t5\t1\t1\t0\t0\t8\nmalformed?\n");
    (void)peer.drain(milliseconds(200));
  });
  ob::Connection connection(target(server.port()));
  // Two whole pushes skipped; the third line is no answer at all and is refused as it arrives.
  EXPECT_EQ(failure_of([&] { (void)connection.execute("PING"); }),
            "OrderbookError: an answer from 127.0.0.1:" + std::to_string(server.port()) +
                " this client cannot read: malformed?\n");
  ProtocolServer then_pong([](Peer& peer) {
    if (!peer.send(sde::live::kGreeting) || !peer.line()) return;
    (void)peer.send("PUSH 1\t1000\t5\t1\t1\t0\t0\t7\nPONG\n");
    (void)peer.drain(milliseconds(200));
  });
  ob::Connection second(target(then_pong.port()));
  EXPECT_EQ(second.execute("PING"), "PONG\n");
}

TEST(OrderbookWire, PipelinedCommandsGoInOneWriteAndTheirAnswersComeBackInOrderSplitAnywhere) {
  std::string received;
  ProtocolServer server([&received](Peer& peer) {
    if (!peer.send(sde::live::kGreeting)) return;
    received = peer.drain(milliseconds(150));
    // Three answers, cut at the worst places: inside a blank line and inside a header.
    for (const char* piece : {"OK\n", "\nERR no\nOK\ntimes", "tamp_ns\n1\n\n"}) {
      (void)peer.send(piece);
      std::this_thread::sleep_for(milliseconds(30));
    }
    (void)peer.drain(milliseconds(200));
  });
  ob::Connection connection(target(server.port()));
  const std::vector<std::string> answers =
      connection.execute_pipelined({"INSERT A B bid 1 1 1 5", "MINSERT A B ask 2 6\n1 1 1\n2 2 2\n",
                                    "SELECT * FROM 'A'.'B' LIMIT 1"});
  EXPECT_EQ(answers, (std::vector<std::string>{"OK\n\n", "ERR no\n", "OK\ntimestamp_ns\n1\n\n"}));
  EXPECT_EQ(received,
            "INSERT A B bid 1 1 1 5\nMINSERT A B ask 2 6\n1 1 1\n2 2 2\nSELECT * FROM 'A'.'B' LIMIT 1\n");
  EXPECT_TRUE(connection.execute_pipelined({}).empty());
}

TEST(OrderbookWire, AnAnswerThatIsNotUtf8IsReadWithReplacementCharacters) {
  ProtocolServer server = answering({"ERR bad \xff\xfe byte\n"});
  ob::Connection connection(target(server.port()));
  EXPECT_EQ(connection.execute("PING"), "ERR bad \xEF\xBF\xBD\xEF\xBF\xBD byte\n");
}

TEST(OrderbookWire, AnExchangeThatDoesNotFinishClosesTheConnectionAndEveryLaterCallSaysWhy) {
  // The engine's #171: the rest of a reply may still be on the way, and the next command would
  // read it as its own. So the connection closes and nothing more is sent on it.
  std::string after;
  ProtocolServer server([&after](Peer& peer) {
    if (!peer.send(sde::live::kGreeting) || !peer.line()) return;
    (void)peer.send("OK\ntimestamp_ns\tprice\n1\t");
    after = peer.drain(milliseconds(1500));
  });
  ob::Connection connection(target(server.port(), "?timeout=0.3"));
  EXPECT_EQ(failure_of([&] { (void)connection.execute("SELECT * FROM 'A'.'B' LIMIT 1"); }),
            "OrderbookError: TCP recv timeout");
  EXPECT_FALSE(connection.open());
  const std::string why = "OrderbookError: the connection to 127.0.0.1:" +
                          std::to_string(server.port()) +
                          " was closed because an exchange (SELECT) did not finish: TCP recv "
                          "timeout. The rest of that exchange may still have been on its way, and "
                          "the next command would have read it as its own reply, so nothing more "
                          "is sent on this connection and nothing was retried. Open a new client; "
                          "a pool replaces the connection at its next health check.";
  EXPECT_EQ(failure_of([&] { (void)connection.execute("PING"); }), why);
  EXPECT_EQ(failure_of([&] { (void)connection.execute_pipelined({"PING"}); }), why);
  EXPECT_EQ(failure_of([&] { connection.ensure_open(); }), why);
  connection.close();
  std::this_thread::sleep_for(milliseconds(1600));
  EXPECT_EQ(after, "") << "nothing, not even QUIT, went on a connection out of step";
}

TEST(OrderbookWire, AServerThatEndsAnAnswerHalfWayIsSaidAndTheConnectionClosed) {
  ProtocolServer server([](Peer& peer) {
    if (!peer.send(sde::live::kGreeting) || !peer.line()) return;
    (void)peer.send("OK\nheader\n");
    peer.close();
  });
  ob::Connection connection(target(server.port()));
  EXPECT_EQ(failure_of([&] { (void)connection.execute_pipelined({"PING", "PING"}); }),
            "OrderbookError: TCP connection closed by server");
  EXPECT_NE(failure_of([&] { (void)connection.execute("PING"); })
                .find("was closed because an exchange (a pipelined batch of 2 command(s)) did not "
                      "finish: TCP connection closed by server."),
            std::string::npos);
}

TEST(OrderbookWire, AnExchangesLabelIsItsVerbAndNeverTheRestOfTheLine) {
  ProtocolServer server([](Peer& peer) {
    if (!peer.send(sde::live::kGreeting)) return;
    (void)peer.drain(milliseconds(1500));
  });
  ob::Connection auth(target(server.port(), "?timeout=0.2"));
  (void)failure_of([&] { (void)auth.execute("AUTH desk 0123456789abcdef"); });
  const std::string said = failure_of([&] { (void)auth.execute("PING"); });
  EXPECT_NE(said.find("an exchange (AUTH) did not finish"), std::string::npos) << said;
  EXPECT_EQ(said.find("0123456789abcdef"), std::string::npos) << "a digest left in a message";
  ob::Connection blank(target(server.port(), "?timeout=0.2"));
  (void)failure_of([&] { (void)blank.execute(" \t"); });
  EXPECT_NE(failure_of([&] { (void)blank.execute("PING"); })
                .find("an exchange (an empty command) did not finish"),
            std::string::npos);
}

TEST(OrderbookWire, CloseSaysQuitOnAConnectionInStep) {
  std::string received;
  ProtocolServer server([&received](Peer& peer) {
    if (!peer.send(sde::live::kGreeting)) return;
    received = peer.drain(milliseconds(500));
  });
  {
    ob::Connection connection(target(server.port()));
    connection.close();
    EXPECT_FALSE(connection.open());
    EXPECT_EQ(failure_of([&] { (void)connection.execute("PING"); }),
              "OrderbookError: the connection to 127.0.0.1:" + std::to_string(server.port()) +
                  " is closed");
    connection.close();  // twice is once
  }
  std::this_thread::sleep_for(milliseconds(600));
  EXPECT_EQ(received, "QUIT\n");
}

TEST(OrderbookWire, TheChallengeIsAnsweredAndARefusalIsSaidWithoutTheSecret) {
  FakeOrderbook server;
  server.require_auth("desk", "the-secret");
  ob::Connection connection(ob::parse_dsn("orderbook://desk:the-secret@127.0.0.1:" +
                                          std::to_string(server.port())));
  EXPECT_EQ(connection.execute("PING"), "PONG\n");
  EXPECT_EQ(connection.execute("FLUSH"), "OK\n\n") << "authenticated: a command past PING runs";
  const std::string wrong = failure_of([&] {
    ob::Connection(ob::parse_dsn("orderbook://desk:not-it@127.0.0.1:" + std::to_string(server.port())));
  });
  EXPECT_EQ(wrong, "OrderbookError: Authentication failed: ERR auth_failed");
  FakeOrderbook open;
  EXPECT_EQ(failure_of([&] {
              ob::Connection(ob::parse_dsn("orderbook://desk:x@127.0.0.1:" +
                                           std::to_string(open.port())));
            }),
            "OrderbookError: Server refused the authentication request: ERR auth_disabled");
  for (const std::string& command : server.commands()) {
    EXPECT_EQ(command.find("the-secret"), std::string::npos) << "the secret went on the wire";
  }
}

TEST(OrderbookWire, ANonceThatIsNotAsciiIsRefusedInPythonsWords) {
  ProtocolServer server([](Peer& peer) {
    if (!peer.send(sde::live::kGreeting) || !peer.line()) return;
    (void)peer.send("OK CHALLENGE ab\xC3\xA9\xE2\x82\xAC" "cd\n\n");
    (void)peer.drain(milliseconds(200));
  });
  EXPECT_EQ(failure_of([&] {
              ob::Connection(ob::parse_dsn("orderbook://desk:s@127.0.0.1:" +
                                           std::to_string(server.port())));
            }),
            "UnicodeEncodeError: 'ascii' codec can't encode characters in position 2-3: ordinal "
            "not in range(128)");
}

TEST(OrderbookWire, EveryWaitIsBoundedBySilenceNotByTheWholeAnswer) {
  // As `sock.settimeout` bounds the engine's client: an answer that keeps arriving is waited for,
  // however long it takes in all.
  ProtocolServer trickle([](Peer& peer) {
    if (!peer.send(sde::live::kGreeting) || !peer.line()) return;
    for (const char c : std::string("OK\n\n")) {
      std::this_thread::sleep_for(milliseconds(250));
      if (!peer.send(std::string(1, c))) return;
    }
    (void)peer.drain(milliseconds(200));
  });
  ob::Connection connection(target(trickle.port(), "?timeout=0.4"));
  const auto start = Clock::now();
  EXPECT_EQ(connection.execute("FLUSH"), "OK\n\n");
  EXPECT_GT(seconds_since(start), 0.9) << "longer in all than the bound";
}

TEST(OrderbookWire, AFailureToConnectIsSaidInPythonsWords) {
  int closed_port = 0;
  {
    ProtocolServer gone([](Peer&) {});
    closed_port = gone.port();
  }
  EXPECT_EQ(failure_of([&] { ob::Connection(target(closed_port)); }),
            "ConnectionRefusedError: [Errno 111] Connection refused");
  const std::string unknown =
      failure_of([] { ob::Connection(ob::parse_dsn("orderbook://no-such-host.invalid:1")); });
  EXPECT_TRUE(unknown.starts_with("gaierror: [Errno -")) << unknown;
}

TEST(OrderbookWire, ATimeoutCPythonCannotHoldIsRefusedBeforeAnySocket) {
  ProtocolServer server([](Peer& peer) { (void)peer.send(sde::live::kGreeting); });
  for (const char* timeout : {"?timeout=inf", "?timeout=9223372036.854776", "?timeout=1e300"}) {
    SCOPED_TRACE(timeout);
    EXPECT_EQ(failure_of([&] { ob::Connection(target(server.port(), timeout)); }),
              "OverflowError: timestamp out of range for platform time_t");
  }
  EXPECT_EQ(server.accepted(), 0);
  // The largest CPython keeps opens.
  ob::Connection largest(target(server.port(), "?timeout=9223372036.854774"));
  EXPECT_EQ(server.accepted(), 1);
}

TEST(OrderbookWire, AnIpv6AddressIsConnectedTo) {
  // Where the engine's client opens IPv4 sockets only, and fails here with `Address family for
  // hostname not supported`.
  ProtocolServer server([](Peer& peer) {
    if (!peer.send(sde::live::kGreeting) || !peer.line()) return;
    (void)peer.send("PONG\n");
    (void)peer.drain(milliseconds(200));
  },
                        std::nullopt, "::1");
  ob::Connection connection(ob::parse_dsn("orderbook://[::1]:" + std::to_string(server.port())));
  EXPECT_EQ(connection.execute("PING"), "PONG\n");
}

}  // namespace
