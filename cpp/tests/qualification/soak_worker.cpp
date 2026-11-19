// One soak application process in C++, one engine, as a client of the installed library: the
// protocol of python/tests/qualification/soak_worker.py, with the values of soak_traffic.py.
//
//   sde_soak_worker --soak <soak.json>
//
// It runs its engine's cycle on a fixed schedule until the coordinator writes the stop file or sends
// SIGTERM, and appends what happened to minutes.jsonl and events.jsonl as it goes. After an error of
// the library's hierarchy (sde::SdeError) it closes and connects the same engine and opens a new
// session, at the next slot; the operation that failed is not repeated. Anything else thrown is
// recorded as an error outside the hierarchy, which the soak counts as a breach. No row value leaves
// the process.

#include <cxxabi.h>

#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <csignal>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <map>
#include <memory>
#include <mutex>
#include <optional>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <thread>
#include <typeinfo>
#include <utility>
#include <vector>

#include "sde/clickhouse.hpp"
#include "sde/errors.hpp"
#include "sde/json.hpp"
#include "sde/model.hpp"
#include "sde/placement.hpp"
#include "sde/postgres.hpp"
#include "sde/session.hpp"
#include "sde/value.hpp"

namespace {

volatile std::sig_atomic_t stopping = 0;

constexpr std::string_view LANGUAGE = "cpp";
constexpr int LANGUAGE_NUMBER = 3;
constexpr std::int64_t BATCH = 20;
constexpr std::int64_t ACCOUNTS = 3;
constexpr std::int64_t SLOW_NS = 1'000'000'000;
constexpr std::array<std::int64_t, 4> STUCK_REPORTS_S = {60, 300, 900, 3600};

const std::array<std::string_view, 20> POSTGRES_CYCLE = {
    "write", "get", "write", "get", "write", "get", "write", "get", "write", "get",
    "write", "get", "write", "get", "write", "get", "write", "get", "scan",  "count"};
const std::array<std::string_view, 20> CLICKHOUSE_CYCLE = {
    "write", "get", "get", "get", "get", "write", "get", "get", "get", "scan",
    "write", "get", "get", "get", "get", "write", "get", "get", "get", "count"};

constexpr std::array<std::string_view, 5> KINDS = {"write", "get", "scan", "count", "connect"};

int kind_index(std::string_view kind) {
  const auto* found = std::ranges::find(KINDS, kind);
  return static_cast<int>(found - KINDS.begin());
}

std::int64_t monotonic_ns() {
  // steady_clock is CLOCK_MONOTONIC on Linux: the clock the coordinator's zero is on.
  return std::chrono::duration_cast<std::chrono::nanoseconds>(
             std::chrono::steady_clock::now().time_since_epoch())
      .count();
}

std::string wall() {
  return sde::TimestampTz(std::chrono::time_point_cast<std::chrono::microseconds>(
                              std::chrono::system_clock::now()))
      .to_string();
}

std::string read_file(const std::string& path) {
  std::ifstream in(path, std::ios::binary);
  if (!in) throw std::runtime_error("cannot read " + path);
  std::ostringstream text;
  text << in.rdbuf();
  return text.str();
}

std::string base64_decode(std::string_view text) {
  constexpr std::string_view alphabet =
      "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
  std::string out;
  std::uint32_t buffer = 0;
  int bits = 0;
  for (const char c : text) {
    if (c == '=') break;
    const std::size_t digit = alphabet.find(c);
    if (digit == std::string_view::npos) throw std::runtime_error("a public key is not base64");
    buffer = (buffer << 6) | static_cast<std::uint32_t>(digit);
    bits += 6;
    if (bits >= 8) {
      bits -= 8;
      out.push_back(static_cast<char>((buffer >> bits) & 0xFF));
    }
  }
  return out;
}

/// The class of an exception without its namespace: `EngineError` for `sde::EngineError`.
std::string class_name(const std::exception& error) {
  int status = 0;
  std::unique_ptr<char, void (*)(void*)> name(
      abi::__cxa_demangle(typeid(error).name(), nullptr, nullptr, &status), std::free);
  std::string text = status == 0 && name ? name.get() : typeid(error).name();
  if (text.starts_with("sde::")) text = text.substr(5);
  return text;
}

// --- the values (soak_traffic.py) -------------------------------------------------------------

const std::int64_t BASE = std::int64_t{sde::Date(2026, 11, 20).days()} * 86'400'000'000;

std::string hex(std::int64_t value, int width) {
  std::ostringstream text;
  text << std::hex << value;
  std::string digits = text.str();
  return std::string(static_cast<std::size_t>(std::max(0, width - static_cast<int>(digits.size()))),
                     '0') +
         digits;
}

sde::Uuid uuid(int kind, std::int64_t number) {
  return sde::Uuid(hex(LANGUAGE_NUMBER, 8) + "-" + hex(kind, 4) + "-4000-8000-" + hex(number, 12));
}

sde::TimestampTz at(std::int64_t micros) { return *sde::TimestampTz::from_micros(BASE + micros); }

std::string account(std::int64_t s) {
  return "soak-" + std::string(LANGUAGE) + "-" + std::to_string(s % ACCOUNTS);
}

const std::string STATION = "soak-" + std::string(LANGUAGE);

bool is_transaction(std::int64_t s) { return s % 3 == 0; }

sde::Row order(std::int64_t s) {
  return {
      {"account", account(s)},
      {"id", uuid(1, s)},
      {"placed_at", at(s * 10)},
      {"price", sde::Decimal(std::to_string(100 + s % 50) + ".12345678")},
      {"qty", sde::Decimal(std::to_string(1 + s % 5) + ".50000000")},
      {"side", std::string(s % 2 == 0 ? "buy" : "sell")},
  };
}

std::vector<sde::Row> fills(std::int64_t s) {
  const sde::Row placed = order(s);
  std::vector<sde::Row> out;
  for (const std::int64_t part : {1, 2}) {
    out.push_back({
        {"account", placed.at("account")},
        {"at", at(s * 10 + part)},
        {"id", uuid(2, s * 2 + part)},
        {"order_id", placed.at("id")},
        {"price", placed.at("price")},
        {"qty", sde::Decimal("0.75000000")},
    });
  }
  return out;
}

sde::Row reading(std::int64_t r) {
  const std::int64_t cents = 1525 + r % 1000;
  const std::string fraction = std::to_string(cents % 100);
  return {
      {"at", at(r)},
      {"celsius", sde::Decimal(std::to_string(cents / 100) + "." +
                               (fraction.size() < 2 ? "0" + fraction : fraction))},
      {"humidity", std::int64_t{30 + r % 70}},
      {"id", uuid(3, r)},
      {"station", STATION},
  };
}

std::vector<sde::Row> batch(std::int64_t b) {
  std::vector<sde::Row> rows;
  for (std::int64_t i = 0; i < BATCH; ++i) rows.push_back(reading(b * BATCH + i));
  return rows;
}

std::int64_t micros_of(const sde::Value& value) {
  return std::get<sde::TimestampTz>(value).micros() - BASE;
}

// --- timings --------------------------------------------------------------------------------------

int bucket(std::int64_t nanoseconds) {
  const double micros = static_cast<double>(nanoseconds) / 1000.0;
  if (micros < 1.0) return 0;
  return 1 + static_cast<int>(std::floor(std::log2(micros) * 8.0));
}

struct OpStats {
  std::int64_t ok = 0;
  std::map<std::string, std::int64_t> errors;
  std::int64_t skipped = 0;
  std::int64_t unavailable = 0;
  std::map<int, std::int64_t> duration;
  std::map<int, std::int64_t> delay;
};

sde::Json counts(const std::map<std::string, std::int64_t>& values) {
  sde::Json::Object out;
  for (const auto& [name, value] : values) out.emplace_back(name, value);
  return out;
}

sde::Json histogram(const std::map<int, std::int64_t>& values) {
  sde::Json::Object out;
  for (const auto& [index, value] : values) out.emplace_back(std::to_string(index), value);
  return out;
}

class Worker {
 public:
  explicit Worker(const sde::Json& soak)
      : soak_(soak),
        dialect_(soak.find("dialect")->as_string()),
        project_(sde::parse_json(read_file(soak.find("project_config")->as_string()))),
        model_(sde::load_neutral_model(*project_.find("model"))),
        out_(soak.find("output")->as_string()),
        events_(out_ + "/events.jsonl", std::ios::app),
        minutes_(out_ + "/minutes.jsonl", std::ios::app),
        cycle_(dialect_ == "postgres" ? POSTGRES_CYCLE : CLICKHOUSE_CYCLE),
        window_(dialect_ == "postgres" ? 150 : 10),
        period_ns_(*soak.find("period_ms")->to_int64() * 1'000'000) {
    std::map<std::string, std::string> keys;
    for (const auto& [name, value] : project_.find("public_keys")->as_object()) {
      keys.emplace(name, base64_decode(value.as_string()));
    }
    keys_ = std::move(keys);
    const sde::Json& binding = *project_.find("engines")->find(dialect_);
    const std::string variable = binding.find("runtime_dsn_envs")->as_array().at(0).as_string();
    const char* dsn = std::getenv(variable.c_str());
    if (dsn == nullptr) throw std::runtime_error("set " + variable);
    dsn_ = dsn;
  }

  int run() {
    const sde::Json start = sde::parse_json(read_file(soak_.find("start_file")->as_string()));
    const std::int64_t zero = *start.find("monotonic_ns")->to_int64();
    const std::string stop_file = soak_.find("stop_file")->as_string();
    std::thread watchdog([this] { watch(); });
    try {
      connect();
    } catch (const sde::SdeError& failure) {
      connected_ = false;
      event({{"connect_failed", class_name(failure)},
             {"message", std::string(failure.what()).substr(0, 300)}});
    }
    std::int64_t minute_started = monotonic_ns();
    std::map<std::string, OpStats> minute;
    std::int64_t slot = std::max<std::int64_t>(0, (monotonic_ns() - zero) / period_ns_);
    while (stopping == 0 && !std::filesystem::exists(stop_file)) {
      const std::int64_t now = monotonic_ns();
      if (now - minute_started >= 60'000'000'000) {
        flush(minute_started, minute);
        minute.clear();
        minute_started = now;
      }
      const std::int64_t due = zero + slot * period_ns_;
      if (now < due) {
        std::this_thread::sleep_for(
            std::chrono::nanoseconds(std::min<std::int64_t>(due - now, 50'000'000)));
        continue;
      }
      const std::string kind(cycle_[static_cast<std::size_t>(slot % 20)]);
      if (now - due > period_ns_) {
        ++minute[kind].skipped;
        ++slot;
        continue;
      }
      if (!connected_) {
        const std::int64_t began = monotonic_ns();
        running_kind_ = kind_index("connect");
        running_started_ = began;
        try {
          connect();
        } catch (const sde::SdeError& failure) {
          running_started_ = 0;
          event({{"connect_failed", class_name(failure)},
                 {"duration_ns", monotonic_ns() - began},
                 {"message", std::string(failure.what()).substr(0, 300)}});
          ++minute[kind].unavailable;
          ++slot;
          continue;
        }
        running_started_ = 0;
        event({{"connected", true}, {"duration_ns", monotonic_ns() - began}});
      }
      operate(minute, kind, slot, due);
      ++slot;
    }
    flush(minute_started, minute);
    done_ = true;
    watchdog.join();
    return 0;
  }

  void close() {
    session_.reset();
    close_engine();
    sde::Json::Array failed;
    for (const std::int64_t number : failed_) failed.emplace_back(number);
    std::ofstream done(out_ + "/done.json");
    done << sde::dump_json(sde::Json::Object{{"language", std::string(LANGUAGE)},
                                             {"dialect", dialect_},
                                             {"attempted", attempted_},
                                             {"highest_acked", highest_acked_},
                                             {"failed", failed},
                                             {"totals", counts(totals_)},
                                             {"sdk_module", std::string("sde (installed)")}})
         << "\n";
  }

 private:
  void event(sde::Json::Object record) {
    sde::Json::Object full{{"t", monotonic_ns()}, {"wall", wall()}};
    for (auto& member : record) full.push_back(std::move(member));
    const std::lock_guard<std::mutex> guard(lock_);
    events_ << sde::dump_json(full) << "\n";
    events_.flush();
  }

  void flush(std::int64_t started, const std::map<std::string, OpStats>& minute) {
    sde::Json::Object ops;
    for (const auto& [kind, stats] : minute) {
      ops.emplace_back(kind, sde::Json::Object{{"ok", stats.ok},
                                               {"errors", counts(stats.errors)},
                                               {"skipped", stats.skipped},
                                               {"unavailable", stats.unavailable},
                                               {"duration", histogram(stats.duration)},
                                               {"delay", histogram(stats.delay)}});
    }
    const std::lock_guard<std::mutex> guard(lock_);
    minutes_ << sde::dump_json(sde::Json::Object{{"t", started},
                                                 {"end", monotonic_ns()},
                                                 {"wall", wall()},
                                                 {"ops", ops},
                                                 {"attempted", attempted_},
                                                 {"highest_acked", highest_acked_},
                                                 {"connected", connected_}})
             << "\n";
    minutes_.flush();
  }

  /// The adapters close and connect; the engine interface the session holds does neither.
  void close_engine() noexcept {
    if (!engine_) return;
    if (dialect_ == "postgres") {
      static_cast<sde::PostgresEngine*>(engine_.get())->close();
    } else {
      static_cast<sde::ClickHouseEngine*>(engine_.get())->close();
    }
  }

  /// The documented recovery: close() and connect() the same engine, then a new session.
  void connect() {
    session_.reset();
    if (!engine_) {
      if (dialect_ == "postgres") {
        engine_ = std::make_unique<sde::PostgresEngine>(dsn_);
      } else {
        engine_ = std::make_unique<sde::ClickHouseEngine>(dsn_);
      }
    } else {
      close_engine();
    }
    if (dialect_ == "postgres") {
      static_cast<sde::PostgresEngine*>(engine_.get())->connect();
    } else {
      static_cast<sde::ClickHouseEngine*>(engine_.get())->connect();
    }
    sde::LoadOptions options;
    options.model = &model_;
    options.public_keys = sde::PublicKeys::named(keys_);
    options.require_signature = true;
    placement_ = std::make_unique<sde::PlacementMap>(sde::load_map(
        sde::parse_json(read_file(soak_.find("project_dir")->as_string() + "/active-map.json")),
        options));
    sde::SessionOptions session_options;
    session_options.project_id = project_.find("project_id")->as_string();
    session_ = std::make_unique<sde::Session>(
        model_, *placement_, std::map<std::string, sde::Engine*>{{dialect_, engine_.get()}},
        session_options);
    connected_ = true;
  }

  void check_failed(std::string_view kind, std::int64_t number, const std::string& what) {
    ++totals_["check_failed"];
    event({{"check_failed", std::string(kind)}, {"number", number}, {"what", what}});
  }

  void write() {
    const std::int64_t number = attempted_ + 1;
    attempted_ = number;
    sde::Session& session = *session_;
    if (dialect_ == "postgres") {
      if (is_transaction(number)) {
        session.transaction({"Order", "Fill"}, [&] {
          session.save("Order", order(number));
          for (const sde::Row& fill : fills(number)) session.save("Fill", fill);
        });
      } else {
        session.save("Order", order(number));
      }
    } else {
      session.save_many("WeatherReading", batch(number));
    }
  }

  std::optional<std::int64_t> pick(std::int64_t slot) const {
    if (highest_acked_ < 0) return std::nullopt;
    std::int64_t candidate = slot % 2 == 0 ? highest_acked_ : (slot * 7919) % (highest_acked_ + 1);
    while (candidate >= 0 && failed_.contains(candidate)) --candidate;
    if (candidate < 0) return std::nullopt;
    return candidate;
  }

  void get(std::int64_t slot) {
    const auto number = pick(slot);
    if (!number) return;
    sde::Row expected;
    std::optional<sde::Row> found;
    if (dialect_ == "postgres") {
      expected = order(*number);
      found = session_->get(
          "Order", {{"account", expected.at("account")}, {"placed_at", expected.at("placed_at")}});
    } else {
      expected = reading(*number * BATCH);
      found = session_->get("WeatherReading",
                            {{"station", expected.at("station")}, {"at", expected.at("at")}});
    }
    if (!found || *found != expected) {
      check_failed("get", *number, "the row read is not the row written");
    }
  }

  std::optional<std::pair<std::int64_t, std::int64_t>> window() const {
    if (highest_acked_ < 0) return std::nullopt;
    return std::pair{std::max<std::int64_t>(0, highest_acked_ - window_ + 1), highest_acked_};
  }

  sde::Range range(std::int64_t low, std::int64_t high) const {
    if (dialect_ == "postgres") return sde::Range{"placed_at", at(low * 10), at(high * 10 + 1)};
    return sde::Range{"at", at(low * BATCH), at(high * BATCH + BATCH)};
  }

  void scan(std::int64_t slot) {
    const auto bounds = window();
    if (!bounds) return;
    const auto [low, high] = *bounds;
    std::set<std::int64_t> seen;
    std::vector<std::int64_t> expected;
    sde::ScanOptions options;
    options.bounds = range(low, high);
    if (dialect_ == "postgres") {
      const std::string name = account(slot);
      options.where = sde::Row{{"account", name}};
      options.limit = 200;
      for (const sde::Row& row : session_->scan("Order", options).rows) {
        const std::int64_t micros = micros_of(row.at("placed_at"));
        if (micros % 10 != 0 || row != order(micros / 10)) {
          check_failed("scan", micros / 10, "a scanned row is not the row written");
        }
        seen.insert(micros / 10);
      }
      for (std::int64_t n = low; n <= high; ++n) {
        if (!failed_.contains(n) && account(n) == name) expected.push_back(n);
      }
    } else {
      options.where = sde::Row{{"station", STATION}};
      options.limit = static_cast<int>(BATCH * window_);
      for (const sde::Row& row : session_->scan("WeatherReading", options).rows) {
        const std::int64_t number = micros_of(row.at("at"));
        if (row != reading(number)) {
          check_failed("scan", number, "a scanned row is not the row written");
        }
        seen.insert(number / BATCH);
      }
      for (std::int64_t n = low; n <= high; ++n) {
        if (!failed_.contains(n)) expected.push_back(n);
      }
    }
    std::int64_t missing = 0;
    std::int64_t first = -1;
    for (const std::int64_t n : expected) {
      if (!seen.contains(n)) {
        if (first < 0) first = n;
        ++missing;
      }
    }
    if (missing > 0) {
      check_failed("scan", first, std::to_string(missing) + " acknowledged writes missing");
    }
  }

  void count(std::int64_t slot) {
    const auto bounds = window();
    if (!bounds) return;
    const auto [low, high] = *bounds;
    sde::CountOptions options;
    options.bounds = range(low, high);
    std::int64_t least = 0;
    std::int64_t most = 0;
    std::uint64_t counted = 0;
    if (dialect_ == "postgres") {
      const std::string name = account(slot);
      options.where = sde::Row{{"account", name}};
      counted = session_->count("Order", options);
      for (std::int64_t n = low; n <= high; ++n) {
        if (account(n) != name) continue;
        ++most;
        if (!failed_.contains(n)) ++least;
      }
    } else {
      options.where = sde::Row{{"station", STATION}};
      counted = session_->count("WeatherReading", options);
      for (std::int64_t n = low; n <= high; ++n) {
        most += BATCH;
        if (!failed_.contains(n)) least += BATCH;
      }
    }
    const auto value = static_cast<std::int64_t>(counted);
    if (value < least || value > most) {
      check_failed("count", low,
                   "counted " + std::to_string(value) + ", expected " + std::to_string(least) +
                       " to " + std::to_string(most));
    }
  }

  void operate(std::map<std::string, OpStats>& minute, const std::string& kind, std::int64_t slot,
               std::int64_t due) {
    const std::int64_t began = monotonic_ns();
    running_kind_ = kind_index(kind);
    running_started_ = began;
    std::optional<std::int64_t> number;
    std::optional<std::string> error;
    bool named = true;
    std::string message;
    try {
      if (kind == "write") {
        number = attempted_ + 1;
        write();
        highest_acked_ = std::max(highest_acked_, *number);
      } else if (kind == "get") {
        get(slot);
      } else if (kind == "scan") {
        scan(slot);
      } else {
        count(slot);
      }
    } catch (const sde::SdeError& failure) {
      error = class_name(failure);
      message = std::string(failure.what()).substr(0, 300);
    } catch (const std::exception& failure) {
      error = class_name(failure);
      named = false;
      message = std::string(failure.what()).substr(0, 300);
    }
    running_started_ = 0;
    const std::int64_t ended = monotonic_ns();
    OpStats& stats = minute[kind];
    if (error) {
      ++stats.errors[*error];
    } else {
      ++stats.ok;
    }
    ++stats.duration[bucket(ended - began)];
    ++stats.delay[bucket(std::max<std::int64_t>(0, began - due))];
    if (ended - began >= SLOW_NS) {
      event({{"slow", kind}, {"duration_ns", ended - began}, {"ok", !error}});
    }
    if (!error) {
      ++totals_[kind];
      if (failures_since_success_ > 0) {
        event({{"recovered", kind},
               {"after_failures", failures_since_success_},
               {"since_first_failure_ns", ended - failing_since_}});
      }
      failures_since_success_ = 0;
      return;
    }
    ++totals_["error_" + *error];
    if (number) failed_.insert(*number);
    if (failures_since_success_ == 0) failing_since_ = began;
    ++failures_since_success_;
    event({{"error", *error},
           {"named", named},
           {"op", kind},
           {"number", number ? sde::Json(*number) : sde::Json(nullptr)},
           {"duration_ns", ended - began},
           {"message", message}});
    connected_ = false;
  }

  void watch() {
    std::set<std::pair<std::int64_t, std::int64_t>> reported;
    while (!done_) {
      std::this_thread::sleep_for(std::chrono::seconds(1));
      const std::int64_t started = running_started_;
      if (started == 0) continue;
      const std::int64_t elapsed_s = (monotonic_ns() - started) / 1'000'000'000;
      for (const std::int64_t mark : STUCK_REPORTS_S) {
        if (elapsed_s >= mark && !reported.contains({started, mark})) {
          reported.insert({started, mark});
          event({{"stuck", std::string(KINDS[static_cast<std::size_t>(running_kind_.load())])},
                 {"running_s", mark},
                 {"started", started}});
        }
      }
    }
  }

  const sde::Json soak_;
  const std::string dialect_;
  const sde::Json project_;
  const sde::Model model_;
  const std::string out_;
  std::ofstream events_;
  std::ofstream minutes_;
  const std::array<std::string_view, 20>& cycle_;
  const std::int64_t window_;
  const std::int64_t period_ns_;
  std::map<std::string, std::string> keys_;
  std::string dsn_;
  std::mutex lock_;
  std::unique_ptr<sde::Engine> engine_;
  std::unique_ptr<sde::PlacementMap> placement_;
  std::unique_ptr<sde::Session> session_;
  bool connected_ = false;
  std::int64_t attempted_ = -1;
  std::int64_t highest_acked_ = -1;
  std::set<std::int64_t> failed_;
  std::int64_t failures_since_success_ = 0;
  std::int64_t failing_since_ = 0;
  std::map<std::string, std::int64_t> totals_;
  std::atomic<std::int64_t> running_started_ = 0;
  std::atomic<int> running_kind_ = 0;  // an index into KINDS, read by the watchdog's thread
  std::atomic<bool> done_ = false;
};

}  // namespace

int main(int argc, char** argv) {
  if (argc != 3 || std::string_view(argv[1]) != "--soak") {
    std::cerr << "usage: sde_soak_worker --soak <soak.json>\n";
    return 2;
  }
  std::signal(SIGTERM, [](int) { stopping = 1; });
  try {
    Worker worker(sde::parse_json(read_file(argv[2])));
    std::cout << "READY" << std::endl;
    int code = 1;
    try {
      code = worker.run();
    } catch (const std::exception& failure) {
      std::cerr << "soak worker failed: " << failure.what() << "\n";
    }
    worker.close();
    return code;
  } catch (const std::exception& failure) {
    std::cerr << "soak worker failed: " << failure.what() << "\n";
    return 1;
  }
}
