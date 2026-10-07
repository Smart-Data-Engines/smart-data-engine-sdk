// A trading firm's application on Smart Data Engine, in C++: book depth, market trades, orders and
// fills. The counterpart of examples/trading/trading.py - the same commands, arguments, engines
// file and traffic - so a run written by either one is verified by the other.
//
//   sde_example_trading declare
//   sde_example_trading provision COMMON
//   sde_example_trading run       COMMON --run RUN [--books 4] [--updates 200] [--window WINDOW]
//                                        [--workload feed|accounts]
//   sde_example_trading verify    COMMON --run RUN [--books 4] [--updates 200]
//
// where COMMON is `--map MAP --keys KEYS --project ID --engines ENGINES`.
//
// The application names entities and fields - `DepthLevel`, `MarketTrade`, `Order`, `Fill` - and
// never an engine or a table. Where each group lives is in the signed placement map; this program
// reads it from a file, and the engines' credentials from its own environment, and connects to each
// engine itself. The model is declared here, in C++, and `declare` prints it in the neutral form for
// the control plane: the declaration examples/trading/model.json holds for the Python and TypeScript
// programs, so the three compute one model version, which is what lets each verify the others' runs.
//
// It exits 0 on success, 1 when `verify` found a difference, and 2 on anything else, with the
// reason on stderr.

#include <openssl/evp.h>

#include <algorithm>
#include <array>
#include <charconv>
#include <cstdint>
#include <cstdlib>
#include <fstream>
#include <functional>
#include <iostream>
#include <map>
#include <memory>
#include <optional>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <tuple>
#include <utility>
#include <vector>

#include "sde/clickhouse.hpp"
#include "sde/errors.hpp"
#include "sde/json.hpp"
#include "sde/model.hpp"
#include "sde/orderbook.hpp"
#include "sde/placement.hpp"
#include "sde/postgres.hpp"
#include "sde/provisioning.hpp"
#include "sde/session.hpp"
#include "sde/telemetry.hpp"
#include "sde/value.hpp"

namespace {

/// The desk's model: book depth in exactly the orderbook engine's shape, the market's trades, and
/// orders with their fills, which commit together. `Fill.order_id` comes from the relation.
sde::Model trading_model() {
  return sde::ModelBuilder{}
      .entity({"DepthLevel",
               {{"symbol", "string"},
                {"exchange", "string"},
                {"timestamp_ns", "int64"},
                {"side", "string"},
                {"level", "int32"},
                {"price", "int64"},
                {"quantity", "int64"},
                {"order_count", "int32"},
                {"sequence_number", "int64", true}},  // assigned by the orderbook server
               {"symbol", "exchange", "timestamp_ns", "side", "level"}})
      .entity({"MarketTrade",
               {{"symbol", "string"},
                {"exchange", "string"},
                {"trade_id", "int64"},
                {"price", "int64"},
                {"quantity", "int64"},
                {"at_ns", "int64"}},
               {"symbol", "exchange", "trade_id"}})
      .entity({"Order",
               {{"id", "uuid"},
                {"account", "string"},
                {"symbol", "string"},
                {"side", "string"},
                {"qty", "decimal(18,8)"},
                {"price", "decimal(18,8)"},
                {"placed_at", "timestamptz"}},
               {"id"}})
      .entity({"Fill",
               {{"id", "uuid"},
                {"qty", "decimal(18,8)"},
                {"price", "decimal(18,8)"},
                {"at", "timestamptz"}},
               {"id"}})
      .relation("order", "Fill", "Order")
      .atomic({"Order", "Fill"})
      .cost_ceiling("2000.00", "EUR")
      .build();
}

constexpr std::string_view EXCHANGE = "sde";
constexpr std::int64_t T0 = 1'790'000'000'000'000'000;
constexpr int LEVELS = 5;
constexpr int ORDER_EVERY = 10;
constexpr int READ_EVERY = 20;
constexpr int ACCOUNTS = 3;

/// A refusal of this program's own, as `trading.py` raises `SystemExit`: said, and exit 2.
struct Usage : std::runtime_error {
  using std::runtime_error::runtime_error;
};

std::string read_file(const std::string& path) {
  std::ifstream in(path, std::ios::binary);
  if (!in) throw Usage("cannot read " + path);
  std::ostringstream text;
  text << in.rdbuf();
  return text.str();
}

/// Standard base64 with padding, as `keys.json` holds the public keys; anything else is refused.
std::string base64_decode(std::string_view text) {
  constexpr std::string_view alphabet =
      "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
  if (text.size() % 4 != 0) throw Usage("a public key in --keys is not base64");
  std::string out;
  std::uint32_t buffer = 0;
  int bits = 0;
  std::size_t padding = 0;
  for (std::size_t i = 0; i < text.size(); ++i) {
    if (text[i] == '=') {
      if (i + 2 < text.size() || ++padding > 2) throw Usage("a public key in --keys is not base64");
      continue;
    }
    const std::size_t digit = alphabet.find(text[i]);
    if (digit == std::string_view::npos || padding != 0) {
      throw Usage("a public key in --keys is not base64");
    }
    buffer = (buffer << 6) | static_cast<std::uint32_t>(digit);
    bits += 6;
    if (bits >= 8) {
      bits -= 8;
      out.push_back(static_cast<char>((buffer >> bits) & 0xFF));
    }
  }
  return out;
}

struct Arguments {
  std::string command;
  std::map<std::string, std::string, std::less<>> options;

  [[nodiscard]] const std::string& at(std::string_view name) const {
    const auto found = options.find(name);
    if (found == options.end()) throw Usage("--" + std::string(name) + " is required");
    return found->second;
  }
  [[nodiscard]] std::optional<std::string> optional(std::string_view name) const {
    const auto found = options.find(name);
    if (found == options.end()) return std::nullopt;
    return found->second;
  }
  [[nodiscard]] int count(std::string_view name, int fallback) const {
    const auto text = optional(name);
    if (!text) return fallback;
    int value = 0;
    const auto [end, error] = std::from_chars(text->data(), text->data() + text->size(), value);
    if (error != std::errc{} || end != text->data() + text->size() || value < 1) {
      throw Usage("--" + std::string(name) + " is a positive integer");
    }
    return value;
  }
};

Arguments parse(int argc, char** argv) {
  if (argc < 2) throw Usage("usage: sde_example_trading declare|provision|run|verify ...");
  Arguments arguments{argv[1], {}};
  const std::set<std::string, std::less<>> commands = {"declare", "provision", "run", "verify"};
  if (!commands.contains(arguments.command)) {
    throw Usage("the command is declare, provision, run or verify, not " + arguments.command);
  }
  if (arguments.command == "declare") {
    if (argc != 2) throw Usage("declare takes no argument");
    return arguments;
  }
  std::set<std::string, std::less<>> known = {"map", "keys", "project", "engines"};
  if (arguments.command != "provision") known.insert({"run", "books", "updates"});
  if (arguments.command == "run") known.insert({"window", "workload"});
  for (int i = 2; i < argc; i += 2) {
    const std::string_view flag = argv[i];
    if (!flag.starts_with("--") || !known.contains(flag.substr(2)) || i + 1 >= argc) {
      throw Usage("unexpected argument " + std::string(flag));
    }
    arguments.options.insert_or_assign(std::string(flag.substr(2)), std::string(argv[i + 1]));
  }
  for (const char* required : {"map", "keys", "project", "engines"}) (void)arguments.at(required);
  if (arguments.command != "provision") {
    const std::string& run = arguments.at("run");
    if (run.size() != 32 ||
        run.find_first_not_of("0123456789abcdef") != std::string::npos) {
      throw Usage("--run is 32 lowercase hex digits");
    }
  }
  if (const auto workload = arguments.optional("workload");
      workload && *workload != "feed" && *workload != "accounts") {
    throw Usage("--workload is feed or accounts");
  }
  return arguments;
}

sde::PlacementMap placement(const Arguments& arguments, const sde::Model& model) {
  // Named: a range-for over a member of a temporary would read it after its end (C++20).
  const sde::Json encoded = sde::parse_json(read_file(arguments.at("keys")));
  std::map<std::string, std::string> keys;
  for (const auto& [name, value] : encoded.as_object()) {
    keys.emplace(name, base64_decode(value.as_string()));
  }
  sde::LoadOptions options;
  options.model = &model;
  options.public_keys = sde::PublicKeys::named(std::move(keys));
  options.require_signature = true;
  return sde::load_map(sde::parse_json(read_file(arguments.at("map"))), options);
}

/// The engines the map names, each from its DSN in this process's environment, connected.
/// Credentials stay here: the control plane has none.
class Engines {
 public:
  Engines(const Arguments& arguments, const sde::PlacementMap& placed, const std::string& role) {
    const sde::Json entries = sde::parse_json(read_file(arguments.at("engines")));
    std::set<std::string> needed;
    for (const auto& entry : placed.groups()) {
      for (const sde::Materialization* spot : entry.second.all()) needed.insert(spot->engine);
    }
    for (const std::string& name : needed) {
      const sde::Json* entry = entries.find(name);
      const sde::Json* variable = entry != nullptr ? entry->find(role + "_env") : nullptr;
      if (variable == nullptr) throw Usage(name + ": no " + role + "_env in --engines");
      const char* dsn = std::getenv(variable->as_string().c_str());
      if (dsn == nullptr || *dsn == '\0') {
        throw Usage(name + ": set " + variable->as_string() + " to its " + role + " DSN");
      }
      owned_.push_back(open(dsn));
      engines_.emplace(name, owned_.back().get());
    }
  }

  [[nodiscard]] const std::map<std::string, sde::Engine*>& all() const noexcept { return engines_; }
  [[nodiscard]] std::vector<std::string> names() const {
    std::vector<std::string> out;
    for (const auto& entry : engines_) out.push_back(entry.first);
    return out;
  }

 private:
  static std::unique_ptr<sde::Engine> open(std::string_view dsn) {
    if (dsn.starts_with("postgresql://") || dsn.starts_with("postgres://")) {
      auto engine = std::make_unique<sde::PostgresEngine>(std::string(dsn));
      engine->connect();
      return engine;
    }
    if (dsn.starts_with("clickhouse://") || dsn.starts_with("clickhouses://")) {
      auto engine = std::make_unique<sde::ClickHouseEngine>(dsn);
      engine->connect();
      return engine;
    }
    if (dsn.starts_with("orderbook://")) {
      auto engine = std::make_unique<sde::OrderbookEngine>(dsn);
      engine->connect();
      return engine;
    }
    throw Usage("a DSN starts with postgresql://, clickhouse:// or orderbook://");
  }

  std::vector<std::unique_ptr<sde::Engine>> owned_;  // each closes when it goes
  std::map<std::string, sde::Engine*> engines_;
};

/// A name-based UUID, version 5 (RFC 9562, SHA-1), as Python's `uuid.uuid5` makes it.
sde::Uuid uuid5(const std::array<std::uint8_t, 16>& space, std::string_view name) {
  std::string input(space.begin(), space.end());
  input.append(name);
  std::array<unsigned char, EVP_MAX_MD_SIZE> digest{};
  unsigned int size = 0;
  if (EVP_Digest(input.data(), input.size(), digest.data(), &size, EVP_sha1(), nullptr) != 1) {
    throw std::runtime_error("SHA-1 failed");
  }
  std::array<std::uint8_t, 16> bytes{};
  std::copy_n(digest.begin(), bytes.size(), bytes.begin());
  bytes[6] = static_cast<std::uint8_t>((bytes[6] & 0x0F) | 0x50);
  bytes[8] = static_cast<std::uint8_t>((bytes[8] & 0x3F) | 0x80);
  return sde::Uuid(bytes);
}

/// Every row a run writes, derived from its id: `verify` regenerates what `run` wrote - in this
/// program and in `trading.py` alike.
class Traffic {
 public:
  Traffic(const std::string& run, int books, int updates)
      : run_(run), books_(books), updates_(updates) {
    std::string prefix = run.substr(0, 10);
    std::ranges::transform(prefix, prefix.begin(), [](char c) {
      return c >= 'a' && c <= 'z' ? static_cast<char>(c - 'a' + 'A') : c;
    });
    for (int book = 0; book < books; ++book) {
      symbols_.push_back("S" + prefix + std::to_string(book));
    }
    for (std::size_t i = 0; i < space_.size(); ++i) {
      space_[i] = static_cast<std::uint8_t>(std::stoi(run.substr(2 * i, 2), nullptr, 16));
    }
  }

  [[nodiscard]] const std::string& run() const noexcept { return run_; }
  [[nodiscard]] int books() const noexcept { return books_; }
  [[nodiscard]] int updates() const noexcept { return updates_; }
  [[nodiscard]] const std::string& symbol(int book) const {
    return symbols_.at(static_cast<std::size_t>(book));
  }

  [[nodiscard]] std::vector<sde::Row> depth(int update, int book) const {
    const bool bid = update % 2 == 0;
    const std::int64_t top = 1'000'000 + (update % 50) * 10;
    std::vector<sde::Row> rows;
    for (int level = 0; level < LEVELS; ++level) {
      rows.push_back({
          {"symbol", symbol(book)},
          {"exchange", std::string(EXCHANGE)},
          {"timestamp_ns", T0 + std::int64_t{update} * 1'000'000 + book},
          {"side", std::string(bid ? "bid" : "ask")},
          {"level", std::int64_t{level}},
          {"price", bid ? top - level : top + 1 + level},
          {"quantity", std::int64_t{1 + (update * 7 + level) % 20}},
          {"order_count", std::int64_t{1 + level % 3}},
          {"sequence_number", sde::Null{}},
      });
    }
    return rows;
  }

  [[nodiscard]] sde::Row trade(int update, int book) const {
    return {
        {"symbol", symbol(book)},
        {"exchange", std::string(EXCHANGE)},
        {"trade_id", std::int64_t{update}},
        {"price", std::int64_t{1'000'000 + (update % 50) * 10}},
        {"quantity", std::int64_t{1 + update % 9}},
        {"at_ns", T0 + std::int64_t{update} * 1'000'000 + book + 500},
    };
  }

  [[nodiscard]] std::pair<sde::Row, std::vector<sde::Row>> order(int update) const {
    const sde::Uuid id = uuid5(space_, "order-" + std::to_string(update));
    // 2026-10-02T00:00:00Z plus one microsecond per update.
    const std::int64_t at = std::int64_t{sde::Date(2026, 10, 2).days()} * 86'400'000'000 + update;
    const sde::Decimal price(std::to_string(100 + update % 50) + ".12345678");
    sde::Row order = {
        {"id", id},
        {"account", "acct-" + std::to_string(update % ACCOUNTS)},
        {"symbol", symbol(update % books_)},
        {"side", std::string(update % 2 == 0 ? "buy" : "sell")},
        {"qty", sde::Decimal(std::to_string(1 + update % 5) + ".50000000")},
        {"price", price},
        {"placed_at", *sde::TimestampTz::from_micros(at)},
    };
    std::vector<sde::Row> fills;
    for (int part = 0; part < 2; ++part) {
      fills.push_back({
          {"id", uuid5(space_, "fill-" + std::to_string(update) + "-" + std::to_string(part))},
          {"order_id", id},
          {"qty", sde::Decimal("0.75000000")},
          {"price", price},
          {"at", *sde::TimestampTz::from_micros(at + part + 1)},
      });
    }
    return {std::move(order), std::move(fills)};
  }

 private:
  std::string run_;
  int books_;
  int updates_;
  std::vector<std::string> symbols_;
  std::array<std::uint8_t, 16> space_{};
};

/// The address of one book: its symbol and exchange.
sde::Row book_of(const Traffic& traffic, int index) {
  return {{"symbol", traffic.symbol(index)}, {"exchange", std::string(EXCHANGE)}};
}

/// The model as the control plane's `declare` reads it.
int declare() {
  std::cout << sde::dump_json(sde::neutral_declaration(trading_model())) << "\n";
  return 0;
}

int provision(const Arguments& arguments) {
  const sde::Model model = trading_model();
  const sde::PlacementMap placed = placement(arguments, model);
  const Engines engines(arguments, placed, "provision");
  sde::prepare_schema(model, placed, engines.all(), arguments.at("project"));
  sde::Json::Array names;
  for (const std::string& name : engines.names()) names.emplace_back(name);
  std::cout << sde::dump_json(sde::Json::Object{{"provisioned", names}}) << "\n";
  return 0;
}

int run(const Arguments& arguments) {
  const sde::Model model = trading_model();
  const sde::PlacementMap placed = placement(arguments, model);
  const Engines engines(arguments, placed, "runtime");
  sde::Recorder recorder(model);
  sde::SessionOptions options;
  options.recorder = &recorder;
  options.project_id = arguments.at("project");
  sde::Session session(model, placed, engines.all(), options);
  const Traffic traffic(arguments.at("run"), arguments.count("books", 4),
                        arguments.count("updates", 200));
  const bool accounts = arguments.optional("workload") == "accounts";
  for (int update = 0; update < traffic.updates(); ++update) {
    std::vector<sde::Row> depth;
    std::vector<sde::Row> trades;
    for (int index = 0; index < traffic.books(); ++index) {
      for (sde::Row& row : traffic.depth(update, index)) depth.push_back(std::move(row));
      trades.push_back(traffic.trade(update, index));
    }
    session.save_many("DepthLevel", depth);
    session.save_many("MarketTrade", trades);
    if (update % ORDER_EVERY == 0) {
      const auto [order, fills] = traffic.order(update);
      session.transaction({"Order", "Fill"}, [&] {
        session.save("Order", order);
        for (const sde::Row& fill : fills) session.save("Fill", fill);
      });
      if (accounts) {
        // What a risk desk does over the same data: each account's orders, page by page - the
        // traffic an index on Order.account is for, and the window says so.
        for (int account = 0; account < ACCOUNTS; ++account) {
          sde::ScanOptions orders;
          orders.where = sde::Row{{"account", "acct-" + std::to_string(account)}};
          orders.limit = 100;
          (void)session.scan("Order", orders);
        }
      }
    }
    if (update % READ_EVERY == 0) {
      sde::ScanOptions recent;
      recent.where = book_of(traffic, update % traffic.books());
      recent.bounds = sde::Range{"timestamp_ns", T0, T0 + std::int64_t{update + 1} * 1'000'000};
      recent.limit = 50;
      (void)session.scan("DepthLevel", recent);
      recent.bounds.reset();
      (void)session.scan("MarketTrade", recent);
      (void)session.get("Order",
                        {{"id", traffic.order(update - update % ORDER_EVERY).first.at("id")}});
    }
  }
  (void)session.measure_storage();
  if (const auto path = arguments.optional("window")) {
    // The window of this run, for the control plane's `observe`.
    const std::optional<sde::Window> window = recorder.roll();
    if (!window) throw Usage("nothing was recorded, so there is no window to write");
    std::ofstream out(*path);
    out << sde::dump_json(window->as_record(model)) << "\n";
    if (!out) throw Usage("cannot write the window to " + *path);
  }
  std::cout << sde::dump_json(sde::Json::Object{{"run", traffic.run()},
                                                {"updates", traffic.updates()},
                                                {"books", traffic.books()}})
            << "\n";
  return 0;
}

/// Every row of one book, page after page in key order.
std::vector<sde::Row> pages(sde::Session& session, std::string_view entity, const sde::Row& where) {
  std::vector<sde::Row> rows;
  sde::ScanOptions next;
  next.where = where;
  next.limit = 1000;
  for (;;) {
    sde::ScanPage page = session.scan(entity, next);
    for (sde::Row& row : page.rows) rows.push_back(std::move(row));
    if (!page.next_after) return rows;
    next.after = std::move(page.next_after);
  }
}

int verify(const Arguments& arguments) {
  const sde::Model model = trading_model();
  const sde::PlacementMap placed = placement(arguments, model);
  const Engines engines(arguments, placed, "runtime");
  sde::SessionOptions options;
  options.project_id = arguments.at("project");
  sde::Session session(model, placed, engines.all(), options);
  const Traffic traffic(arguments.at("run"), arguments.count("books", 4),
                        arguments.count("updates", 200));
  const std::string depth_engine = placed.groups().at("DepthLevel").source.engine;
  const bool on_orderbook = engines.all().at(depth_engine)->dialect() == "orderbook";
  sde::Json::Array mismatches;
  std::int64_t depth = 0;
  std::int64_t trades = 0;
  std::int64_t orders = 0;
  std::int64_t fills = 0;
  for (int index = 0; index < traffic.books(); ++index) {
    const sde::Row where = book_of(traffic, index);
    const std::string& symbol = traffic.symbol(index);
    // The rows a run wrote, in key order: one book's key is the instant, the side, the level.
    std::vector<sde::Row> wanted;
    for (int update = 0; update < traffic.updates(); ++update) {
      for (sde::Row& row : traffic.depth(update, index)) wanted.push_back(std::move(row));
    }
    std::ranges::sort(wanted, {}, [](const sde::Row& row) {
      return std::tuple(std::get<std::int64_t>(row.at("timestamp_ns")),
                        std::get<std::string>(row.at("side")), std::get<std::int64_t>(row.at("level")));
    });
    std::vector<sde::Row> seen = pages(session, "DepthLevel", where);
    // On the orderbook engine the server numbers every update; a row without its number was not
    // read back from that engine.
    const bool unnumbered = std::ranges::any_of(seen, [](const sde::Row& row) {
      const auto number = row.find("sequence_number");
      return number == row.end() || sde::is_null(number->second);
    });
    if (on_orderbook && unnumbered) {
      mismatches.emplace_back(symbol + ": a row without the server's sequence number");
    }
    for (sde::Row& row : seen) row.erase("sequence_number");
    for (sde::Row& row : wanted) row.erase("sequence_number");
    if (seen != wanted) {
      mismatches.emplace_back(symbol + ": depth differs (" + std::to_string(seen.size()) + " of " +
                              std::to_string(wanted.size()) + " rows)");
    }
    depth += static_cast<std::int64_t>(seen.size());
    const std::vector<sde::Row> stored = pages(session, "MarketTrade", where);
    std::vector<sde::Row> expected;
    for (int update = 0; update < traffic.updates(); ++update) {
      expected.push_back(traffic.trade(update, index));
    }
    if (stored != expected) {
      mismatches.emplace_back(symbol + ": trades differ (" + std::to_string(stored.size()) + ")");
    }
    trades += static_cast<std::int64_t>(stored.size());
  }
  for (int update = 0; update < traffic.updates(); update += ORDER_EVERY) {
    const auto [order, its_fills] = traffic.order(update);
    if (session.get("Order", {{"id", order.at("id")}}) != order) {
      mismatches.emplace_back("order " + std::to_string(update) + " differs");
    }
    ++orders;
    for (const sde::Row& fill : its_fills) {
      if (session.get("Fill", {{"id", fill.at("id")}}) != fill) {
        mismatches.emplace_back("fill " + std::get<sde::Uuid>(fill.at("id")).to_string() +
                                " differs");
      }
      ++fills;
    }
  }
  const bool clean = mismatches.empty();
  std::cout << sde::dump_json(sde::Json::Object{
                   {"run", traffic.run()},
                   {"verified", sde::Json::Object{{"depth", depth},
                                                  {"trades", trades},
                                                  {"orders", orders},
                                                  {"fills", fills}}},
                   {"mismatches", std::move(mismatches)}})
            << "\n";
  return clean ? 0 : 1;
}

}  // namespace

int main(int argc, char** argv) {
  try {
    const Arguments arguments = parse(argc, argv);
    if (arguments.command == "declare") return declare();
    if (arguments.command == "provision") return provision(arguments);
    if (arguments.command == "run") return run(arguments);
    return verify(arguments);
  } catch (const std::exception& error) {
    // Usage and the library's refusals alike: they name fields and engines, never a value.
    std::cerr << error.what() << "\n";
    return 2;
  }
}
