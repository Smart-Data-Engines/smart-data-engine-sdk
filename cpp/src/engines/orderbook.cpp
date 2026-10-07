#include "sde/orderbook.hpp"

#include <unistd.h>

#include <algorithm>
#include <array>
#include <map>
#include <set>
#include <utility>

#include "engines/orderbook/dsn.hpp"
#include "engines/orderbook/wire.hpp"
#include "python_compat.hpp"
#include "python_url.hpp"
#include "sde/errors.hpp"
#include "sde/layout.hpp"
#include "unicode_internal.hpp"

namespace sde {

namespace {

using detail::python_repr;
using detail::python_value_int;
using detail::python_value_repr;
using detail::python_value_str;
using detail::python_value_type_name;
using detail::to_int64;
using detail::orderbook::Answer;
using detail::orderbook::Connection;
using detail::orderbook::WireError;
using detail::python_url::PythonInt;

/// Integers as wide as the reference's arithmetic on them: Python's are unbounded, a scan's bounds
/// run to the engine's last instant, 2^64 - 1, and a window's width doubles past it.
__extension__ typedef __int128 Wide;
__extension__ typedef unsigned __int128 WideMagnitude;

constexpr std::int64_t kMaxTimestamp = INT64_MAX;  ///< the model's int64; the engine stores uint64
constexpr std::int64_t kMaxQuantity = INT64_MAX;
constexpr std::int64_t kMaxOrderCount = (std::int64_t{1} << 31) - 1;  ///< the model's int32
constexpr std::int64_t kMinPrice = INT64_MIN;  ///< negative prices are real, and stored
constexpr std::int64_t kMaxPrice = INT64_MAX;
constexpr std::int64_t kUnknownSequence = 0;  ///< the engine's numbering starts at 1

/// The two values `side` may take, sorted as strings compare - the order a key-order scan returns.
constexpr std::array<std::string_view, 2> kSides = {"ask", "bid"};
constexpr std::string_view kSidesRepr = "['ask', 'bid']";

/// What a row read back may hold: the model's types, which are what this library writes. The engine
/// stores unsigned 64-bit times, quantities and numbers, so it can hand back a value the model's
/// int64 cannot hold; an engine before c1f14c0 read a stored quantity of 2^60 - 1 back as 2^64 - 1.
struct Bound {
  const char* field;
  std::int64_t low;
  std::int64_t high;
};
constexpr std::array<Bound, 5> kReadBounds = {{{"timestamp_ns", 0, kMaxTimestamp},
                                               {"level", 0, OrderbookEngine::MAX_LEVEL},
                                               {"quantity", 0, kMaxQuantity},
                                               {"order_count", 0, kMaxOrderCount},
                                               {"sequence_number", 1, INT64_MAX}}};

/// The columns of a `SELECT *` answer, in order: read by position, so checked against the header.
const std::vector<std::string>& query_columns() {
  static const std::vector<std::string> columns = {
      "timestamp_ns", "price", "quantity", "order_count", "side", "level", "sequence_number"};
  return columns;
}

std::string wide_text(Wide value) {
  if (value == 0) return "0";
  const bool negative = value < 0;
  WideMagnitude magnitude =
      negative ? WideMagnitude{0} - static_cast<WideMagnitude>(value) : static_cast<WideMagnitude>(value);
  std::string digits;
  while (magnitude != 0) {
    digits.insert(digits.begin(), static_cast<char>('0' + static_cast<int>(magnitude % 10)));
    magnitude /= 10;
  }
  return negative ? "-" + digits : digits;
}

std::string integer_text(const PythonInt& value) { return (value.negative ? "-" : "") + value.digits; }

/// An integer the reference holds without bound, as wide as any it reaches here; the plan's values
/// are the model's int64, and anything past 38 digits is past every bound a scan compares it with.
Wide wide_of(const PythonInt& value) {
  if (value.digits.size() > 38) {
    const Wide far = Wide{1} << 120;
    return value.negative ? -far : far;
  }
  Wide magnitude = 0;
  for (const char c : value.digits) magnitude = magnitude * 10 + (c - '0');
  return value.negative ? -magnitude : magnitude;
}

/// The value as a Python `str` when it is one: a string, or a document that is a JSON string.
std::optional<std::string> python_text(const Value& value) {
  if (const auto* text = std::get_if<std::string>(&value)) return *text;
  if (const auto* json = std::get_if<JsonDocument>(&value)) {
    if (json->document.kind() == Json::Kind::string) return json->document.as_string();
  }
  return std::nullopt;
}

/// The value as a Python `int` when it is one - not a bool, which Python also calls one.
std::optional<PythonInt> python_integer(const Value& value) {
  if (std::holds_alternative<std::int64_t>(value)) return python_value_int(value);
  if (const auto* json = std::get_if<JsonDocument>(&value)) {
    if (json->document.kind() == Json::Kind::number && json->document.as_number().integer) {
      return python_value_int(value);
    }
  }
  return std::nullopt;
}

/// Python's `None`: a null, or a document that is JSON's.
bool python_none(const Value& value) {
  if (std::holds_alternative<Null>(value)) return true;
  const auto* json = std::get_if<JsonDocument>(&value);
  return json != nullptr && json->document.kind() == Json::Kind::null;
}

bool is_side(const Value& value) {
  const std::optional<std::string> text = python_text(value);
  return text && std::find(kSides.begin(), kSides.end(), *text) != kSides.end();
}

/// A symbol or an exchange, refused when the engine could not store or address it faithfully. Two
/// reasons: the query language has no escape inside a string literal, so a quote or a backslash
/// cannot be expressed - and a doubled quote would address another symbol; and the protocol splits
/// a command on whitespace, so a space shifts every later field, and a value crafted to line up
/// would write another book.
std::string name_of(const Value& value, const std::string& what) {
  const std::optional<std::string> text = python_text(value);
  if (!text || text->empty()) {
    throw EngineError(what + " must be a non-empty string, not " + python_value_repr(value));
  }
  const std::optional<std::u32string> characters = detail::decode_utf8(*text);
  bool faithful = characters.has_value();
  if (characters) {
    for (const char32_t c : *characters) {
      if (c == U'\'' || c == U'\\' || detail::python_space(c) || !detail::python_printable(c)) {
        faithful = false;
      }
    }
  }
  if (!faithful) {
    throw EngineError(python_repr(*text) + " cannot be used as a " + what +
                      ": this engine's query language has no escape sequence inside a string "
                      "literal and its wire protocol separates fields by whitespace, so a quote, a "
                      "backslash, a space or a control character cannot be stored or addressed "
                      "faithfully. Refused rather than escaped, because the escaping one would "
                      "reach for would address a different book without saying so.");
  }
  return *text;
}

/// A single-quoted literal of the engine's query language, for a symbol or an exchange.
std::string literal(const std::string& value) {
  return "'" + name_of(Value(value), "symbol or exchange") + "'";
}

/// An integer in `[low, high]`, or a refusal naming the field and both bounds.
std::int64_t integer_of(const Value& value, const std::string& field, std::int64_t low,
                        std::int64_t high) {
  const std::optional<PythonInt> integer = python_integer(value);
  if (!integer) {
    throw EngineError(field + " must be an integer, not " + python_value_type_name(value));
  }
  const std::optional<std::int64_t> fits = to_int64(*integer);
  if (!fits || *fits < low || *fits > high) {
    throw EngineError(field + " " + integer_text(*integer) + " is outside what this engine stores (" +
                      std::to_string(low) + " to " + std::to_string(high) +
                      "). Refused before sending: the server would refuse it with a message about "
                      "tokens, or store something else.");
  }
  return *fits;
}

std::string out_of_range(const std::string& field, std::int64_t low, std::int64_t high) {
  // The value itself is left out: it is read data, and it is not what this library wrote.
  return "the engine returned a row whose " + field + " is outside " + std::to_string(low) + " to " +
         std::to_string(high) +
         ", the range of the model's type. No write of this library stores such a value, so the "
         "row is not one it wrote, and it is refused rather than returned. An engine before "
         "c1f14c0 read a stored quantity of 2^60 - 1 back as 2^64 - 1.";
}

const Value& field_of(const Row& row, const std::string& name) {
  static const Value none;
  const auto found = row.find(name);
  return found == row.end() ? none : found->second;
}

std::vector<std::string> sorted_names(const std::set<std::string>& names) {
  return std::vector<std::string>(names.begin(), names.end());
}

std::set<std::string> shape_names() {
  std::set<std::string> out;
  for (const auto& [name, type] : ORDERBOOK_SHAPE) out.emplace(name);
  return out;
}

bool nullable(const std::string& name) {
  return std::find(std::begin(ORDERBOOK_NULLABLE), std::end(ORDERBOOK_NULLABLE), name) !=
         std::end(ORDERBOOK_NULLABLE);
}

std::string ints_repr(const std::vector<std::size_t>& values) {
  std::string out = "[";
  for (std::size_t i = 0; i < values.size(); ++i) {
    if (i != 0) out += ", ";
    out += std::to_string(values[i]);
  }
  return out + "]";
}

/// A row's position within one book: time, then side, then level - its key without the book.
using Position = std::tuple<std::int64_t, std::string, std::int64_t>;

Position position_of(const Row& row) {
  return {std::get<std::int64_t>(row.at("timestamp_ns")), std::get<std::string>(row.at("side")),
          std::get<std::int64_t>(row.at("level"))};
}

/// `{'symbol': ..., 'exchange': ..., ...}`: a key as the reference writes it in a refusal.
std::string key_repr(const Row& key) {
  std::string out = "{";
  bool first = true;
  for (const std::string_view name : ORDERBOOK_KEY) {
    if (!first) out += ", ";
    first = false;
    out += python_repr(name) + ": " + python_value_repr(field_of(key, std::string(name)));
  }
  return out + "}";
}

std::string duplicate(const std::string& table, const Row& key, std::size_t count) {
  return std::to_string(count) + " rows in " + table + " share the key " + key_repr(key) +
         ". This engine is an append-only log of depth updates and does not enforce a key, so this "
         "is a key violation it could not have prevented. Refused rather than answered with one "
         "of them: picking either would be a read that lies about uniqueness, and you would not see "
         "it.";
}

void check_table(const std::string& table) {
  if (table != ORDERBOOK_TABLE) {
    throw EngineError("this engine has one table, 'orderbook', not " + python_repr(table));
  }
}

/// The sequence number is the server's over TCP: a number chosen here would be refused by a current
/// server and silently replaced by an older one.
void no_sequence(const Value& value) {
  if (python_none(value)) return;
  throw EngineError(
      "over TCP the sequence number is the server's: it numbers every update per book, and a "
      "number chosen here would be refused by a current server and silently replaced by an older "
      "one, so the row read back would disagree with the row written. Write sequence_number as "
      "null and read the server's number back.");
}

/// One row of an answer, as the engine's client reads it: every number with `int()`.
struct Raw {
  PythonInt timestamp;
  PythonInt price;
  PythonInt quantity;
  PythonInt order_count;
  std::string side;
  PythonInt level;
  PythonInt sequence;
};

PythonInt read_int(const std::string& text) { return python_value_int(Value(text)); }

/// The row the reference makes of an answer's row, refused when a field is outside its type.
Row row_of(const std::string& symbol, const std::string& exchange, const Raw& raw) {
  const std::array<const PythonInt*, 5> checked = {&raw.timestamp, &raw.level, &raw.quantity,
                                                   &raw.order_count, &raw.sequence};
  const bool unknown_sequence = to_int64(raw.sequence) == kUnknownSequence;
  for (std::size_t i = 0; i < kReadBounds.size(); ++i) {
    if (i == 4 && unknown_sequence) continue;  // unknown, not zero
    const std::optional<std::int64_t> value = to_int64(*checked[i]);
    if (!value || *value < kReadBounds[i].low || *value > kReadBounds[i].high) {
      throw EngineError(out_of_range(kReadBounds[i].field, kReadBounds[i].low, kReadBounds[i].high));
    }
  }
  // The reference holds a price of any size; a C++ row holds an int64, the model's type.
  const std::optional<std::int64_t> price = to_int64(raw.price);
  if (!price) throw EngineError(out_of_range("price", kMinPrice, kMaxPrice));
  Row out;
  out["symbol"] = symbol;
  out["exchange"] = exchange;
  out["timestamp_ns"] = *to_int64(raw.timestamp);
  out["side"] = raw.side;
  out["level"] = *to_int64(raw.level);
  out["price"] = *price;
  out["quantity"] = *to_int64(raw.quantity);
  out["order_count"] = *to_int64(raw.order_count);
  out["sequence_number"] = unknown_sequence ? Value() : Value(*to_int64(raw.sequence));
  return out;
}

/// The bytes of one update: `INSERT` for one level, `MINSERT` and a line per level for more, with
/// the event time, which this library always gives.
std::string command_of(const std::string& symbol, const std::string& exchange,
                       const std::string& side, std::int64_t timestamp,
                       const std::vector<OrderbookLevel>& levels) {
  if (levels.size() == 1) {
    return "INSERT " + symbol + " " + exchange + " " + side + " " + std::to_string(levels[0].price) +
           " " + std::to_string(levels[0].quantity) + " " + std::to_string(levels[0].order_count) +
           " " + std::to_string(timestamp);
  }
  std::string out = "MINSERT " + symbol + " " + exchange + " " + side + " " +
                    std::to_string(levels.size()) + " " + std::to_string(timestamp);
  for (const OrderbookLevel& level : levels) {
    out += "\n" + std::to_string(level.price) + " " + std::to_string(level.quantity) + " " +
           std::to_string(level.order_count);
  }
  return out;
}

const char* const kNotConnected = "not connected; call connect() first";

}  // namespace

// --- the engine's own types ------------------------------------------------------------------------

/// One update: N levels of one side of one book at one instant, and the rows they came from.
struct OrderbookEngine::Update {
  std::string symbol;
  std::string exchange;
  std::string side;
  std::int64_t timestamp = 0;
  std::vector<OrderbookLevel> levels;
  std::vector<std::size_t> rows;
};

/// One logical read, translated into what this engine can be asked.
struct OrderbookEngine::Scan {
  std::string symbol;
  std::string exchange;
  std::optional<std::string> side;
  std::optional<PythonInt> level;
  std::optional<Wide> price_low;
  std::optional<Wide> price_high;
  Wide low = 0;
  Wide high = 0;
  bool descending = false;
  std::optional<std::tuple<Wide, std::string, Wide>> after;

  [[nodiscard]] bool keeps(const Row& row) const {
    if (side && std::get<std::string>(row.at("side")) != *side) return false;
    if (level) {
      const std::optional<std::int64_t> wanted = to_int64(*level);
      if (!wanted || std::get<std::int64_t>(row.at("level")) != *wanted) return false;
    }
    if (after) {
      const std::tuple<Wide, std::string, Wide> position(
          std::get<std::int64_t>(row.at("timestamp_ns")), std::get<std::string>(row.at("side")),
          std::get<std::int64_t>(row.at("level")));
      return descending ? position < *after : position > *after;
    }
    return true;
  }
};

class OrderbookEngine::Use {
 public:
  explicit Use(OrderbookEngine& engine) : engine_(engine) {
    if (engine.process_ != static_cast<long>(getpid())) {
      throw ResourceClosed("create a fresh adapter after fork; the inherited connection is not usable");
    }
    const std::thread::id me = std::this_thread::get_id();
    std::thread::id nobody;
    if (!engine.owner_.compare_exchange_strong(nobody, me) && nobody != me) {
      throw ResourceBusy("the connection already has an operation in progress");
    }
    ++engine.depth_;
  }
  ~Use() {
    if (--engine_.depth_ == 0) engine_.owner_.store(std::thread::id{});
  }
  Use(const Use&) = delete;
  Use& operator=(const Use&) = delete;

 private:
  OrderbookEngine& engine_;
};

// --- connection ------------------------------------------------------------------------------------

OrderbookEngine::OrderbookEngine(std::string_view dsn, OrderbookOptions options)
    : target_(std::make_unique<detail::orderbook::Target>(detail::orderbook::parse_dsn(dsn))),
      options_(std::move(options)),
      process_(static_cast<long>(getpid())) {}

OrderbookEngine::~OrderbookEngine() { close(); }

void OrderbookEngine::connect() {
  const Use use(*this);
  // An open connection is kept; one an exchange left out of step has said why to every call since,
  // and is replaced here.
  if (connection_ && connection_->open()) return;
  connection_.reset();
  std::unique_ptr<Connection> opened;
  try {
    opened = std::make_unique<Connection>(*target_);
  } catch (const std::exception& error) {
    throw EngineError(std::string("could not open the orderbook engine: ") + error.what());
  }
  std::set<std::string> capabilities;
  try {
    capabilities = detail::orderbook::server_capabilities(opened->execute("STATUS"));
  } catch (const std::exception& error) {
    opened->close();
    throw EngineError(std::string("could not read the orderbook server's capabilities: ") +
                      error.what());
  }
  if (!capabilities.contains("insert_event_time")) {
    opened->close();
    throw EngineError(
        "this orderbook server cannot store a write's event time, so every update would be "
        "stamped with its arrival instead of the time it happened and could not be found by the "
        "time it carries. Upgrade the server; nothing was written.");
  }
  connection_ = std::move(opened);
}

void OrderbookEngine::close() noexcept {
  if (connection_) connection_->close();
  connection_.reset();
  unflushed_ = 0;
}

bool OrderbookEngine::connected() const noexcept { return connection_ && connection_->open(); }

std::string OrderbookEngine::describe() const {
  const std::string identity = target_->auth ? ", identity=" + python_repr(target_->auth->first) : "";
  return "OrderbookEngine(tcp " + target_->host + ":" + std::to_string(target_->port) + identity +
         ", tls=" + (target_->tls ? "on" : "off") + ")";
}

Connection& OrderbookEngine::ready() {
  // Refused before anything is sent, and said as it is: the reference reads its client inside the
  // write's own failure, and reports a write it never sent as one that may have been stored.
  if (!connection_) throw EngineError(kNotConnected);
  try {
    connection_->ensure_open();
  } catch (const WireError& error) {
    throw EngineError(error.what());
  }
  return *connection_;
}

void OrderbookEngine::write_failed(const std::string& table, const std::string& error) const {
  Json fields = Json::object();
  fields.set("table", table);
  fields.set("error", error);
  detail::emit(options_.log, "sde.write.failed", fields);
}

// --- schema ------------------------------------------------------------------------------------------

void OrderbookEngine::check_layout(const PhysicalLayout& layout, const Keys& keys) const {
  std::vector<std::string> entities;
  for (const auto& [entity, table] : layout.tables) entities.push_back(entity);
  if (entities.size() != 1) {
    throw EngineError("this engine stores one thing and the map gives it " + python_repr(entities) +
                      ". A colocation group is what shares an engine, so a group of two cannot be "
                      "placed here.");
  }
  const std::string& entity = entities.front();
  const std::string& table = layout.tables.at(entity);
  if (table != ORDERBOOK_TABLE) {
    throw EngineError("the map calls the table " + python_repr(table) +
                      " and this engine's storage is 'orderbook'. The name is the engine's, not "
                      "ours: there is no CREATE TABLE to send it, so a map naming something else "
                      "was built for another engine.");
  }
  std::set<std::string> declared;
  if (const auto columns = layout.columns.find(entity); columns != layout.columns.end()) {
    for (const auto& [name, type] : columns->second) declared.insert(name);
  }
  const std::set<std::string> expected = shape_names();
  std::set<std::string> missing;
  std::set<std::string> extra;
  std::set_difference(expected.begin(), expected.end(), declared.begin(), declared.end(),
                      std::inserter(missing, missing.end()));
  std::set_difference(declared.begin(), declared.end(), expected.begin(), expected.end(),
                      std::inserter(extra, extra.end()));
  if (!missing.empty() || !extra.empty()) {
    throw EngineError("the map's layout for " + entity + " does not match this engine's fixed shape: " +
                      (missing.empty() ? "" : "missing " + python_repr(sorted_names(missing))) +
                      (!missing.empty() && !extra.empty() ? "; " : "") +
                      (extra.empty() ? "" : "unexpected " + python_repr(sorted_names(extra))) +
                      ". The shape is fixed in the engine and the whole of it is " +
                      python_repr(sorted_names(expected)) + ".");
  }
  const auto given = keys.find(entity);
  const std::vector<std::string> key = given == keys.end() ? std::vector<std::string>{} : given->second;
  const std::vector<std::string> wanted(std::begin(ORDERBOOK_KEY), std::end(ORDERBOOK_KEY));
  if (key != wanted) {
    throw EngineError("the map keys " + entity + " by " + python_repr(key) +
                      " and this engine addresses rows by " + python_repr(wanted) +
                      ". The order is positional and it is load-bearing: the symbol and the "
                      "exchange are how a query reaches the data at all.");
  }
}

std::vector<PhysicalFinding> OrderbookEngine::ensure_schema(const PhysicalLayout& layout,
                                                            const Keys& keys) {
  const Use use(*this);
  // Checked here as well as where the map is built: this runs against the document the client
  // holds, the only place a map built for another engine, or by an older version of us, is caught.
  check_layout(layout, keys);
  Json fields = Json::object();
  fields.set("engine", std::string(dialect()));
  fields.set("statements", std::int64_t{0});
  detail::emit(options_.log, "sde.schema.applied", fields);
  return {};
}

std::vector<PhysicalFinding> OrderbookEngine::validate_schema(const PhysicalLayout& layout,
                                                              const Keys& keys) {
  const Use use(*this);
  check_layout(layout, keys);
  return {};
}

// --- writes ------------------------------------------------------------------------------------------

void OrderbookEngine::insert(const std::string& table, const Row& values) {
  const Use use(*this);
  check_table(table);
  const std::set<std::string> shape = shape_names();
  std::vector<std::string> missing;
  for (const std::string& name : shape) {
    if (!values.contains(name) && !nullable(name)) missing.push_back(name);
  }
  if (!missing.empty()) {
    throw EngineError("insert into " + table + " is missing " + python_repr(missing) +
                      ". Every field of the fixed shape is required: this engine has no defaults to "
                      "fall back on and no nullable columns except the sequence number.");
  }
  std::vector<std::string> extra;
  for (const auto& [name, value] : values) {
    if (!shape.contains(name)) extra.push_back(name);
  }
  if (!extra.empty()) {
    throw EngineError("insert into " + table + " carries " + python_repr(extra) +
                      ", which this engine has nowhere to store: its shape is fixed. A write "
                      "generation in particular is never stamped here: a group on this engine "
                      "carries no write generation.");
  }
  const std::int64_t level = integer_of(values.at("level"), "level", 0, MAX_LEVEL);
  if (level != 0) {
    throw EngineError(
        "insert into " + table + " declares level " + std::to_string(level) +
        ", and this engine's write API has no level parameter - a price's level is its index "
        "within one update. Writing this would store it at level 0 and the read would disagree "
        "with the write. Write the whole update with Session.save_many (levels 0 to n-1 of one "
        "side of one book at one instant), which is the granularity this engine has.");
  }
  // `insert_levels` of the one level, as the reference writes it.
  const Value& side = values.at("side");
  if (!is_side(side)) {
    throw EngineError("side must be one of " + std::string(kSidesRepr) + ", not " +
                      python_value_repr(side));
  }
  const std::string symbol = name_of(values.at("symbol"), "symbol");
  const std::string exchange = name_of(values.at("exchange"), "exchange");
  const std::int64_t stamp = integer_of(values.at("timestamp_ns"), "timestamp_ns", 0, kMaxTimestamp);
  const OrderbookLevel only{integer_of(values.at("price"), "price", kMinPrice, kMaxPrice),
                            integer_of(values.at("quantity"), "quantity", 0, kMaxQuantity),
                            integer_of(values.at("order_count"), "order_count", 0, kMaxOrderCount)};
  no_sequence(field_of(values, "sequence_number"));
  Connection& connection = ready();
  try {
    const Answer answer = detail::orderbook::parse_answer(
        connection.execute(command_of(symbol, exchange, *python_text(side), stamp, {only})));
    if (answer.error) throw WireError("TCP INSERT failed: " + answer.message, "OrderbookError");
  } catch (const WireError& error) {
    // Surfaced, not swallowed and not rerouted: a write that did not happen is not ours to hide.
    write_failed(table, error.python_class());
    throw EngineError("the engine did not confirm the write to " + table + ": " + error.what() +
                      ". If the connection dropped after the update was sent, it may still have "
                      "been stored: read the book before writing it again.");
  }
  unflushed_ += 1;
}

void OrderbookEngine::insert_levels(const std::string& table, const std::string& symbol,
                                    const std::string& exchange, const std::string& side,
                                    std::int64_t timestamp_ns,
                                    const std::vector<OrderbookLevel>& levels) {
  const Use use(*this);
  check_table(table);
  if (!is_side(Value(side))) {
    throw EngineError("side must be one of " + std::string(kSidesRepr) + ", not " + python_repr(side));
  }
  const std::string checked_symbol = name_of(Value(symbol), "symbol");
  const std::string checked_exchange = name_of(Value(exchange), "exchange");
  (void)integer_of(Value(timestamp_ns), "timestamp_ns", 0, kMaxTimestamp);
  if (levels.empty()) {
    throw EngineError(
        "an update with no levels is not an empty update, it is a write that would report success "
        "without storing anything");
  }
  if (levels.size() > static_cast<std::size_t>(MAX_LEVEL + 1)) {
    throw EngineError("has " + std::to_string(levels.size()) +
                      " levels and this engine stores at most 1000 per side");
  }
  for (const OrderbookLevel& level : levels) {
    (void)integer_of(Value(level.price), "price", kMinPrice, kMaxPrice);
    (void)integer_of(Value(level.quantity), "quantity", 0, kMaxQuantity);
    (void)integer_of(Value(level.order_count), "order_count", 0, kMaxOrderCount);
  }
  Connection& connection = ready();
  try {
    const Answer answer = detail::orderbook::parse_answer(connection.execute(
        command_of(checked_symbol, checked_exchange, side, timestamp_ns, levels)));
    if (answer.error) {
      throw WireError(std::string(levels.size() > 1 ? "TCP MINSERT" : "TCP INSERT") +
                          " failed: " + answer.message,
                      "OrderbookError");
    }
  } catch (const WireError& error) {
    write_failed(table, error.python_class());
    throw EngineError("the engine did not confirm the write to " + table + ": " + error.what() +
                      ". If the connection dropped after the update was sent, it may still have "
                      "been stored: read the book before writing it again.");
  }
  unflushed_ += static_cast<std::int64_t>(levels.size());
}

std::vector<OrderbookEngine::Update> OrderbookEngine::updates(const std::vector<Row>& rows) const {
  // Rows grouped into the engine's updates, in the order each update first appears, and every
  // malformed batch refused before anything is sent. One side of one book at one instant is one
  // update, and its rows are levels 0 to n-1, each once.
  struct Entry {
    std::tuple<std::string, std::string, std::string, std::int64_t> book;
    std::map<std::int64_t, OrderbookLevel> levels;
    std::map<std::int64_t, std::size_t> rows;
  };
  std::vector<Entry> grouped;
  std::map<std::tuple<std::string, std::string, std::string, std::int64_t>, std::size_t> index_of;
  const std::set<std::string> shape = shape_names();
  for (std::size_t index = 0; index < rows.size(); ++index) {
    const Row& row = rows[index];
    const std::string where = "row " + std::to_string(index) + ": ";
    std::vector<std::string> missing;
    for (const std::string& name : shape) {
      if (!row.contains(name) && !nullable(name)) missing.push_back(name);
    }
    if (!missing.empty()) {
      throw EngineError(where + "missing " + python_repr(missing) +
                        "; every field of the shape is required");
    }
    std::vector<std::string> extra;
    for (const auto& [name, value] : row) {
      if (!shape.contains(name)) extra.push_back(name);
    }
    if (!extra.empty()) {
      throw EngineError(where + "carries " + python_repr(extra) +
                        ", which this engine has nowhere to store; a group on it carries no write "
                        "generation");
    }
    const Value& side = row.at("side");
    if (!is_side(side)) {
      throw EngineError(where + "side must be one of " + std::string(kSidesRepr) + ", not " +
                        python_value_repr(side));
    }
    std::tuple<std::string, std::string, std::string, std::int64_t> book{
        name_of(row.at("symbol"), "symbol"), name_of(row.at("exchange"), "exchange"),
        *python_text(side),
        integer_of(row.at("timestamp_ns"), where + "timestamp_ns", 0, kMaxTimestamp)};
    const std::int64_t level = integer_of(row.at("level"), where + "level", 0, MAX_LEVEL);
    const OrderbookLevel value{
        integer_of(row.at("price"), where + "price", kMinPrice, kMaxPrice),
        integer_of(row.at("quantity"), where + "quantity", 0, kMaxQuantity),
        integer_of(row.at("order_count"), where + "order_count", 0, kMaxOrderCount)};
    no_sequence(field_of(row, "sequence_number"));
    auto [found, fresh] = index_of.emplace(book, grouped.size());
    if (fresh) grouped.push_back(Entry{book, {}, {}});
    Entry& entry = grouped[found->second];
    if (entry.levels.contains(level)) {
      throw EngineError("rows " + std::to_string(entry.rows.at(level)) + " and " +
                        std::to_string(index) + " are both level " + std::to_string(level) +
                        " of one update (" + std::get<0>(book) + " " + std::get<1>(book) + " " +
                        std::get<2>(book) + " at " + std::to_string(std::get<3>(book)) +
                        "). An update holds one price per level, so the second would have nowhere "
                        "to go.");
    }
    entry.levels.emplace(level, value);
    entry.rows.emplace(level, index);
  }
  std::vector<Update> out;
  for (const Entry& entry : grouped) {
    const auto& [symbol, exchange, side, stamp] = entry.book;
    const auto depth = static_cast<std::int64_t>(entry.levels.size());
    if (entry.levels.rbegin()->first != depth - 1) {
      std::string present = "[";
      for (const auto& [level, value] : entry.levels) {
        present += (present.size() > 1 ? ", " : "") + std::to_string(level);
      }
      throw EngineError("the update for " + symbol + " " + exchange + " " + side + " at " +
                        std::to_string(stamp) + " has levels " + present +
                        "]. An update is levels 0 to n-1 without a gap: the engine numbers levels "
                        "by position, so a missing one would renumber every level after it.");
    }
    Update update{symbol, exchange, side, stamp, {}, {}};
    for (const auto& [level, value] : entry.levels) {
      update.levels.push_back(value);
      update.rows.push_back(entry.rows.at(level));
    }
    out.push_back(std::move(update));
  }
  return out;
}

void OrderbookEngine::insert_many(const std::string& table, const std::vector<Row>& rows) {
  const Use use(*this);
  check_table(table);
  const std::vector<Update> all = updates(rows);
  if (all.empty()) return;
  Connection& connection = ready();
  std::size_t stored = 0;
  for (std::size_t start = 0; start < all.size(); start += BATCH_UPDATES) {
    const std::size_t end = std::min(all.size(), start + BATCH_UPDATES);
    std::vector<std::string> commands;
    for (std::size_t i = start; i < end; ++i) {
      commands.push_back(command_of(all[i].symbol, all[i].exchange, all[i].side, all[i].timestamp,
                                    all[i].levels));
    }
    std::vector<std::string> answers;
    try {
      answers = connection.execute_pipelined(commands);
    } catch (const WireError& error) {
      write_failed(table, error.python_class());
      // Closed, so a reply still on its way is never read as the answer to the next write.
      connection_.reset();
      unflushed_ = 0;
      throw EngineError("the outcome of this batch is unknown: " + std::to_string(stored) +
                        " of its " + std::to_string(all.size()) +
                        " updates were confirmed, and the connection failed on the next " +
                        std::to_string(end - start) + " (" + error.python_class() +
                        "). Those may have been stored in full, in part or not at all; read the "
                        "book before writing them again, then connect() again.");
    }
    std::optional<std::size_t> first_refused;
    std::string first_message;
    std::size_t refused = 0;
    for (std::size_t i = 0; i < answers.size(); ++i) {
      const Answer answer = detail::orderbook::parse_answer(answers[i]);
      if (answer.error) {
        ++refused;
        if (!first_refused) {
          first_refused = i;
          first_message = answer.message;
        }
        continue;
      }
      ++stored;
      unflushed_ += static_cast<std::int64_t>(all[start + i].levels.size());
    }
    if (first_refused) {
      write_failed(table, "refused");
      throw EngineError("the server refused " + std::to_string(refused) + " of " +
                        std::to_string(all.size()) + " updates in this batch; the first was update " +
                        std::to_string(start + *first_refused) + " (rows " +
                        ints_repr(all[start + *first_refused].rows) + "): " + first_message + ". " +
                        std::to_string(stored) +
                        " updates were stored - a batch here is not a transaction - and any after "
                        "this part of the batch were not sent.");
    }
  }
}

void OrderbookEngine::flush() {
  const Use use(*this);
  flush_now();
}

void OrderbookEngine::flush_now() {
  if (unflushed_ == 0) return;
  try {
    if (!connection_) throw WireError(kNotConnected, "EngineError");
    const Answer answer = detail::orderbook::parse_answer(connection_->execute("FLUSH"));
    if (answer.error) throw WireError("TCP FLUSH failed: " + answer.message, "OrderbookError");
  } catch (const WireError& error) {
    throw EngineError(std::string("flush failed: ") + error.what());
  }
  Json fields = Json::object();
  fields.set("rows", unflushed_);
  detail::emit(options_.log, "sde.orderbook.flushed", fields);
  unflushed_ = 0;
}

// --- reads -------------------------------------------------------------------------------------------

std::vector<std::vector<std::string>> OrderbookEngine::query(const std::string& statement) {
  // As the engine's client reads an answer - an error, an aggregate, columns it cannot read by
  // position, a field that is not a number - and as the reference reads its failure: a book the
  // server does not know is an empty one, because over TCP the server says which it is.
  std::vector<std::vector<std::string>> rows;
  std::string failure;
  try {
    if (!connection_) throw WireError(kNotConnected, "EngineError");
    const Answer answer = detail::orderbook::parse_answer(connection_->execute(statement));
    if (answer.error) throw WireError("query error: " + answer.message, "OrderbookError");
    const std::vector<std::string>& header = answer.header;
    if (header.size() >= 3 && header[0] == "name" && header[1] == "value" && header[2] == "scale") {
      throw WireError("this query returned aggregates, not rows; use query_agg() (columns: " +
                          python_repr(header) + ")",
                      "OrderbookError");
    }
    const std::vector<std::string>& columns = query_columns();
    if (header.size() < 6 || header.size() > columns.size() ||
        !std::equal(header.begin(), header.end(), columns.begin())) {
      throw WireError("this client reads the standard row columns by position and the server "
                      "answered with " +
                          python_repr(header) +
                          ". Ask for `SELECT *`, or read the response with a client that reads "
                          "columns by name.",
                      "OrderbookError");
    }
    for (const std::vector<std::string>& row : answer.rows) {
      if (row.size() < 6) continue;  // as the client skips a row too short to read
      rows.push_back(row);
    }
  } catch (const WireError& error) {
    failure = error.what();
  }
  if (failure.empty()) {
    // The client reads every number with `int()` before it hands a row back; one that is not a
    // number fails the query.
    try {
      for (const std::vector<std::string>& row : rows) {
        (void)read_int(row[0]);
        (void)read_int(row[1]);
        (void)read_int(row[2]);
        (void)read_int(row[3]);
        (void)read_int(row[5]);
        if (row.size() > 6) (void)read_int(row[6]);
      }
    } catch (const EngineError& error) {
      failure = error.what();
    }
  }
  if (failure.empty()) return rows;
  if (failure.find("OB_ERR_NOT_FOUND") != std::string::npos) return {};
  throw EngineError("query failed: " + statement + ": " + failure);
}

namespace {

Raw raw_of(const std::vector<std::string>& fields) {
  Raw raw;
  raw.timestamp = read_int(fields[0]);
  raw.price = read_int(fields[1]);
  raw.quantity = read_int(fields[2]);
  raw.order_count = read_int(fields[3]);
  raw.side = fields[4] == "0" ? "bid" : "ask";
  raw.level = read_int(fields[5]);
  raw.sequence = fields.size() > 6 ? read_int(fields[6]) : PythonInt{false, "0"};
  return raw;
}

}  // namespace

std::vector<Row> OrderbookEngine::book(const std::string& symbol, const std::string& exchange,
                                       std::optional<std::uint64_t> start_ns,
                                       std::optional<std::uint64_t> end_ns,
                                       std::optional<std::int64_t> limit) {
  flush_now();
  std::string where;
  if (start_ns || end_ns) {
    const Wide low = start_ns ? static_cast<Wide>(*start_ns) : 0;
    const Wide high = end_ns ? static_cast<Wide>(*end_ns) : (Wide{1} << 64) - 1;
    where = " WHERE timestamp BETWEEN " + wide_text(low) + " AND " + wide_text(high);
  }
  const std::string cap =
      limit ? std::to_string(*limit) : std::to_string(limits_.chunk_rows + 1);
  const std::string statement =
      "SELECT * FROM " + literal(symbol) + "." + literal(exchange) + where + " LIMIT " + cap;
  const std::vector<std::vector<std::string>> rows = query(statement);
  if (!limit && rows.size() > limits_.chunk_rows) {
    throw EngineError("this read of " + symbol + " " + exchange + " holds more than " +
                      std::to_string(limits_.chunk_rows) +
                      " rows; bound it by time, give a limit, or page through it with Session.scan");
  }
  std::vector<Row> out;
  out.reserve(rows.size());
  for (const std::vector<std::string>& fields : rows) out.push_back(row_of(symbol, exchange, raw_of(fields)));
  return out;
}

std::vector<Row> OrderbookEngine::levels(const std::string& symbol, const std::string& exchange,
                                         std::optional<std::uint64_t> start_ns,
                                         std::optional<std::uint64_t> end_ns,
                                         std::optional<std::int64_t> limit) {
  const Use use(*this);
  return book(symbol, exchange, start_ns, end_ns, limit);
}

std::optional<Row> OrderbookEngine::get(const std::string& table, const Row& key) {
  const Use use(*this);
  check_table(table);
  std::vector<std::string> missing;
  for (const std::string_view name : ORDERBOOK_KEY) {
    if (!key.contains(name)) missing.emplace_back(name);
  }
  std::sort(missing.begin(), missing.end());
  if (!missing.empty()) {
    const std::vector<std::string> order(std::begin(ORDERBOOK_KEY), std::end(ORDERBOOK_KEY));
    throw EngineError("get from " + table + " is missing " + python_repr(missing) +
                      " from the key. This engine addresses rows by " + python_repr(order) +
                      " and cannot scan for a partial one: the symbol and the exchange are how a "
                      "query reaches the data at all.");
  }
  // A key no row can have is answered without asking: a time outside the model's int64 range, a
  // side that is neither.
  const std::optional<std::int64_t> timestamp = to_int64(python_value_int(key.at("timestamp_ns")));
  if (!timestamp || *timestamp < 0 || !is_side(key.at("side"))) return std::nullopt;
  const std::string side = *python_text(key.at("side"));
  const std::string symbol = python_value_str(key.at("symbol"));
  const std::string exchange = python_value_str(key.at("exchange"));
  const auto at = static_cast<std::uint64_t>(*timestamp);
  std::vector<Row> found;
  std::optional<std::optional<std::int64_t>> level;  // `int()` of the key's level, read when needed
  for (Row& row : book(symbol, exchange, at, at, std::nullopt)) {
    if (std::get<std::string>(row.at("side")) != side) continue;
    if (!level) level = to_int64(python_value_int(key.at("level")));
    if (*level && std::get<std::int64_t>(row.at("level")) == **level) found.push_back(std::move(row));
  }
  if (found.empty()) return std::nullopt;
  // The engine does not enforce the key, so two rows can share it; answering with either would be
  // a read that lies about uniqueness.
  if (found.size() > 1) throw EngineError(duplicate(table, key, found.size()));
  return std::move(found.front());
}

std::vector<Row> OrderbookEngine::select_rows(const std::string& table, const ReadPlan& plan) {
  const Use use(*this);
  check_table(table);
  // The read, or none when it provably matches nothing; every refusal before any I/O.
  std::map<std::string, Value> equal;
  std::map<std::string, std::map<ReadOperation, Value>> bounds;
  for (const ReadFilter& item : plan.filters) {
    if (item.operation == ReadOperation::eq) {
      equal[item.column.name] = item.value;
    } else {
      bounds[item.column.name][item.operation] = item.value;
    }
  }
  static const std::set<std::string> kEqualFields = {"symbol", "exchange", "side", "level", "price"};
  static const std::set<std::string> kRangeFields = {"timestamp_ns", "price"};
  std::vector<std::string> unsupported;
  for (const auto& [name, value] : equal) {
    if (!kEqualFields.contains(name)) unsupported.push_back(name);
  }
  if (!unsupported.empty()) {
    throw QueryRefused("this engine cannot filter on " + python_repr(unsupported) +
                       ": its query language selects one book by symbol and exchange and narrows it "
                       "by time and price. Reading a book's history to filter it here would be a "
                       "full scan dressed as a query.");
  }
  std::vector<std::string> ranged;
  for (const auto& [name, value] : bounds) {
    if (!kRangeFields.contains(name)) ranged.push_back(name);
  }
  if (!ranged.empty()) {
    throw QueryRefused("this engine has no range over " + python_repr(ranged) +
                       "; it bounds a read by timestamp_ns or price");
  }
  if (!equal.contains("symbol") || !equal.contains("exchange")) {
    throw QueryRefused(
        "a read here names one book: where= must fix both symbol and exchange. The engine's query "
        "language takes them in its FROM clause, so there is no scan across books - a property of "
        "an engine built for one workload, not a limitation to route around.");
  }
  std::vector<std::string> remaining;
  for (const ReadColumn& column : plan.order) {
    if (!equal.contains(column.name)) remaining.push_back(column.name);
  }
  std::vector<std::string> expected;
  for (const char* name : {"timestamp_ns", "side", "level"}) {
    if (!equal.contains(name)) expected.emplace_back(name);
  }
  if (remaining != expected) {
    throw QueryRefused("this engine answers one book in time order (then side, then level), and "
                       "this read asks for " +
                       python_repr(remaining) +
                       ". Ordering a book's history by anything else would mean reading all of it "
                       "first.");
  }
  const auto wide = [](const Value& value) { return wide_of(python_value_int(value)); };
  Scan scan;
  scan.low = 0;
  scan.high = (Wide{1} << 64) - 1;
  if (const auto times = bounds.find("timestamp_ns"); times != bounds.end()) {
    if (const auto ge = times->second.find(ReadOperation::ge); ge != times->second.end()) {
      scan.low = std::max(scan.low, wide(ge->second));
    }
    if (const auto lt = times->second.find(ReadOperation::lt); lt != times->second.end()) {
      scan.high = std::min(scan.high, wide(lt->second) - 1);
    }
  }
  if (const auto prices = bounds.find("price"); prices != bounds.end()) {
    if (const auto ge = prices->second.find(ReadOperation::ge); ge != prices->second.end()) {
      scan.price_low = wide(ge->second);
    }
    if (const auto lt = prices->second.find(ReadOperation::lt); lt != prices->second.end()) {
      scan.price_high = wide(lt->second) - 1;
    }
  }
  if (const auto price = equal.find("price"); price != equal.end()) {
    const Wide value = wide(price->second);
    scan.price_low = scan.price_low ? std::max(*scan.price_low, value) : value;
    scan.price_high = scan.price_high ? std::min(*scan.price_high, value) : value;
  }
  if (const auto side = equal.find("side"); side != equal.end()) scan.side = python_value_str(side->second);
  if (const auto level = equal.find("level"); level != equal.end()) {
    scan.level = python_value_int(level->second);
  }
  if (scan.side && std::find(kSides.begin(), kSides.end(), *scan.side) == kSides.end()) return {};
  scan.descending = plan.descending;
  if (plan.after) {
    std::map<std::string, Value> position;
    for (std::size_t i = 0; i < plan.order.size() && i < plan.after->size(); ++i) {
      position[plan.order[i].name] = (*plan.after)[i];
    }
    scan.after = std::make_tuple(
        wide(position.at("timestamp_ns")),
        scan.side ? *scan.side : python_value_str(position.at("side")),
        scan.level ? wide_of(*scan.level) : wide(position.at("level")));
    if (scan.descending) {
      scan.high = std::min(scan.high, std::get<0>(*scan.after));
    } else {
      scan.low = std::max(scan.low, std::get<0>(*scan.after));
    }
  }
  if (scan.low > scan.high ||
      (scan.price_low && scan.price_high && *scan.price_low > *scan.price_high)) {
    return {};
  }
  scan.symbol = name_of(equal.at("symbol"), "symbol");
  scan.exchange = name_of(equal.at("exchange"), "exchange");

  flush_now();
  // One page in key order, assembled from windows of time: the engine answers in arrival order, so
  // a page cannot be its LIMIT. A window is read whole and sorted; it starts at one second, doubles
  // while sparse and halves when it holds more than a chunk, so memory stays bounded and the rows
  // read follow the page, not the book. One instant holding more than a chunk is refused.
  const auto need = static_cast<std::size_t>(plan.limit + 1);
  const std::size_t chunk = limits_.chunk_rows;
  std::vector<Row> page;
  Wide width = static_cast<Wide>(limits_.first_window_ns);
  Wide low = scan.low;
  Wide high = scan.high;
  while (low <= high && page.size() < need) {
    const Wide start = scan.descending ? std::max(low, high - width + 1) : low;
    const Wide end = scan.descending ? high : std::min(high, low + width - 1);
    std::string price;
    if (scan.price_low || scan.price_high) {
      price = " AND price BETWEEN " + wide_text(scan.price_low.value_or(kMinPrice)) + " AND " +
              wide_text(scan.price_high.value_or(kMaxPrice));
    }
    const std::string statement = "SELECT * FROM " + literal(scan.symbol) + "." +
                                  literal(scan.exchange) + " WHERE timestamp BETWEEN " +
                                  wide_text(start) + " AND " + wide_text(end) + price + " LIMIT " +
                                  std::to_string(chunk + 1);
    const std::vector<std::vector<std::string>> raw = query(statement);
    if (raw.size() > chunk) {
      if (start == end) {
        throw EngineError(scan.symbol + " " + scan.exchange + " holds more than " +
                          std::to_string(chunk) + " rows at the instant " + wide_text(start) +
                          "; that many updates at one nanosecond is a key collision on a scale "
                          "this read cannot page through");
      }
      width = std::max(Wide{1}, (end - start + 1) / 2);
      continue;
    }
    std::vector<Row> rows;
    for (const std::vector<std::string>& fields : raw) {
      Row row = row_of(scan.symbol, scan.exchange, raw_of(fields));
      if (scan.keeps(row)) rows.push_back(std::move(row));
    }
    std::stable_sort(rows.begin(), rows.end(), [&](const Row& left, const Row& right) {
      return scan.descending ? position_of(right) < position_of(left)
                             : position_of(left) < position_of(right);
    });
    for (Row& row : rows) {
      if (page.size() == need) break;
      page.push_back(std::move(row));
    }
    if (scan.descending) {
      high = start - 1;
    } else {
      low = end + 1;
    }
    // Grown on what the engine sent, not on what the read kept: a filter that keeps few rows of a
    // dense window must not double it into one that holds too many. And only while a doubled
    // window would still fit.
    if (page.size() < need && raw.size() * 4 < chunk) width *= 2;
  }
  for (std::size_t i = 1; i < page.size(); ++i) {
    if (position_of(page[i - 1]) == position_of(page[i])) throw EngineError(duplicate(table, page[i], 2));
  }
  return page;
}

// --- transactions --------------------------------------------------------------------------------------

void OrderbookEngine::transaction(const std::function<void()>& /*body*/) {
  throw EngineError(
      "this engine has no multi-statement transactions, so there is nothing here to give you. One "
      "group is one engine's transaction semantics: if two entities must change together, declare "
      "that with atomic_with and the planner will place them somewhere that can. Refused rather "
      "than quietly doing nothing, because a transaction that is not one is worse than not having "
      "the method.");
}

Capabilities OrderbookEngine::capabilities() noexcept {
  Capabilities offered;
  offered.query = this;
  offered.count_refusal = std::string(COUNT_REFUSAL);
  offered.summary_refusal = std::string(SUMMARY_REFUSAL);
  offered.bulk = this;
  offered.schema = this;
  return offered;
}

}  // namespace sde
