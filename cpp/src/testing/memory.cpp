#include "sde/testing/memory.hpp"

#include <algorithm>
#include <stdexcept>
#include <string>
#include <tuple>
#include <variant>

#include "python_compat.hpp"
#include "sde/errors.hpp"
#include "sde/migration.hpp"

namespace sde::testing {

namespace {

using detail::python_repr;

/// A total order over the value kinds a vector may use, with the kind first: comparing a string
/// with an integer is not a key order, and a vector that mixed them should fail loudly. Within a
/// kind, as Python orders it: integers exactly, text by code point (the bytes of UTF-8).
struct Sortable {
  int kind = 0;
  std::variant<std::int64_t, double> number;
  std::string text;

  friend bool operator==(const Sortable& left, const Sortable& right) {
    return (left <=> right) == std::weak_ordering::equivalent;
  }
  friend std::weak_ordering operator<=>(const Sortable& left, const Sortable& right) {
    if (left.kind != right.kind) return left.kind <=> right.kind;
    const auto* a = std::get_if<std::int64_t>(&left.number);
    const auto* b = std::get_if<std::int64_t>(&right.number);
    if (a != nullptr && b != nullptr) {
      if (*a != *b) return *a < *b ? std::weak_ordering::less : std::weak_ordering::greater;
    } else {
      const auto as_long = [](const std::variant<std::int64_t, double>& n) {
        return std::visit([](auto v) { return static_cast<long double>(v); }, n);
      };
      const long double x = as_long(left.number);
      const long double y = as_long(right.number);
      if (x < y) return std::weak_ordering::less;
      if (x > y) return std::weak_ordering::greater;
    }
    const int order = left.text.compare(right.text);
    return order < 0 ? std::weak_ordering::less
                     : (order > 0 ? std::weak_ordering::greater : std::weak_ordering::equivalent);
  }
};

Sortable sortable(const Value& value) {
  if (is_null(value)) return {0, std::int64_t{0}, {}};
  if (const auto* flag = std::get_if<bool>(&value)) return {1, std::int64_t{*flag ? 1 : 0}, {}};
  if (const auto* integer = std::get_if<std::int64_t>(&value)) return {2, *integer, {}};
  if (const auto* real = std::get_if<double>(&value)) return {2, *real, {}};
  if (const auto* text = std::get_if<std::string>(&value)) return {3, std::int64_t{0}, *text};
  // Any other kind by its text, as the reference's fixture orders by `str(value)`.
  const Json written = value_to_json(value);
  return {3, std::int64_t{0}, written.is_string() ? written.as_string() : dump_json(written)};
}

std::vector<Sortable> key_of(const std::vector<std::string>& order, const Row& row) {
  std::vector<Sortable> out;
  out.reserve(order.size());
  for (const std::string& column : order) {
    const auto found = row.find(column);
    out.push_back(sortable(found == row.end() ? Value(Null{}) : found->second));
  }
  return out;
}

std::vector<Sortable> key_of(const std::vector<Value>& values) {
  std::vector<Sortable> out;
  out.reserve(values.size());
  for (const Value& value : values) out.push_back(sortable(value));
  return out;
}

Json values_json(const std::optional<std::vector<Value>>& values) {
  if (!values) return nullptr;
  Json out = Json::array();
  for (const Value& value : *values) out.as_array().push_back(value_to_json(value));
  return out;
}

Value column_of(const Row& row, const std::string& column) {
  const auto found = row.find(column);
  return found == row.end() ? Value(Null{}) : found->second;
}

std::vector<std::string> sorted_names(const Row& row) {
  std::vector<std::string> names;
  for (const auto& [name, unused] : row) names.push_back(name);
  return names;
}

}  // namespace

// --- Recorded -----------------------------------------------------------------------------------

void Recorded::note(std::string_view engine, std::string_view method, Json arguments) {
  Json call = Json::object();
  call.set("engine", std::string(engine));
  call.set("call", std::string(method));
  for (auto& [key, value] : arguments.as_object()) call.set(key, std::move(value));
  calls_.push_back(std::move(call));
}

Json Recorded::as_json() const {
  Json out = Json::array();
  for (const Json& call : calls_) out.as_array().push_back(call);
  return out;
}

// --- values -------------------------------------------------------------------------------------

Value value_from_json(const Json& value) {
  switch (value.kind()) {
    case Json::Kind::null:
      return Null{};
    case Json::Kind::boolean:
      return value.as_bool();
    case Json::Kind::number:
      if (const auto integer = value.to_int64()) return *integer;
      return *value.to_double();
    case Json::Kind::string:
      return value.as_string();
    case Json::Kind::array:
    case Json::Kind::object:
      return JsonDocument{value};
  }
  return Null{};
}

Json value_to_json(const Value& value) {
  return std::visit(
      [](const auto& item) -> Json {
        using T = std::decay_t<decltype(item)>;
        if constexpr (std::is_same_v<T, Null>) {
          return nullptr;
        } else if constexpr (std::is_same_v<T, bool> || std::is_same_v<T, std::int64_t> ||
                             std::is_same_v<T, double> || std::is_same_v<T, std::string>) {
          return item;
        } else if constexpr (std::is_same_v<T, Bytes>) {
          return item.to_hex();
        } else if constexpr (std::is_same_v<T, JsonDocument>) {
          return item.document;
        } else {
          return item.to_string();
        }
      },
      value);
}

Row row_from_json(const Json& object) {
  Row row;
  for (const auto& [name, value] : object.as_object()) row[name] = value_from_json(value);
  return row;
}

Json row_to_json(const Row& row) {
  Json out = Json::object();
  for (const auto& [name, value] : row) out.set(name, value_to_json(value));
  return out;
}

// --- MemoryFences -------------------------------------------------------------------------------

FenceMetadata MemoryFences::metadata(const std::string& /*table*/) {
  return FenceMetadata{identity, column, constraints};
}

void MemoryFences::done(Json call) {
  calls.push_back(call);
  if (on_call) on_call(call);
  if (fail_after && calls.size() == *fail_after) {
    throw EngineError("lost the response after the DDL took effect");
  }
}

void MemoryFences::add_column(const std::string& table) {
  column = ColumnState::valid;
  done(Json(Json::Array{"column", table}));
}

void MemoryFences::add_constraint(const std::string& table, const std::string& name,
                                  const std::string& expression) {
  constraints.emplace(name, expression);  // like setdefault: a constraint already there stays
  done(Json(Json::Array{"add", table, name, expression}));
}

void MemoryFences::drop_constraint(const std::string& table, const std::string& name) {
  constraints.erase(name);
  done(Json(Json::Array{"drop", table, name}));
}

void MemoryFences::drain(const std::string& table, const std::string& project_id,
                         const std::string& hold) {
  // A drain without its own hold in place would wait for nothing: the caller's protocol is wrong.
  const FenceState state = fence_state(metadata(table));
  if (state.project_id != project_id ||
      std::find(state.holds.begin(), state.holds.end(), hold) == state.holds.end()) {
    throw std::logic_error("drain called without the project's hold " + hold + " in place");
  }
  done(Json(Json::Array{"drain", table, project_id, hold}));
}

void MemoryFences::restore(const std::string& table, const std::string& project_id,
                           const std::string& hold) {
  done(Json(Json::Array{"restore", table, project_id, hold}));
}

// --- MemoryEngine -------------------------------------------------------------------------------

MemoryEngine::MemoryEngine(MemoryEngineOptions options)
    : tables(std::move(options.tables)),
      dialect_(std::move(options.dialect)),
      name_(std::move(options.name)),
      journal_(options.journal ? std::move(options.journal) : std::make_shared<Recorded>()),
      can_keep_bookkeeping_(options.can_keep_bookkeeping),
      can_migrate_(options.can_migrate),
      can_bulk_write_(options.can_bulk_write),
      fail_inserts_(std::move(options.fail_inserts)) {
  if (options.watermark) watermarks_.push_back(*options.watermark);
  for (const auto& [key, rows] : options.markers) markers_[key].push_back(rows);
}

void MemoryEngine::bind_fences(std::map<std::string, std::shared_ptr<MemoryFences>> backends) {
  fences_ = std::move(backends);
}

Capabilities MemoryEngine::capabilities() noexcept {
  Capabilities offered;
  if (can_keep_bookkeeping_) offered.watermark = this;
  if (can_bulk_write_) offered.bulk = this;
  if (can_migrate_) offered.migration = this;
  if (!fences_.empty()) {
    offered.fences = this;
    offered.schema = this;
  }
  return offered;
}

std::vector<PhysicalFinding> MemoryEngine::ensure_schema(const PhysicalLayout& layout,
                                                         const Keys& /*keys*/) {
  std::vector<std::string> created;
  for (const auto& [entity, table] : layout.tables) created.push_back(table);
  std::sort(created.begin(), created.end());
  Json arguments = Json::object();
  Json names = Json::array();
  for (const std::string& table : created) names.as_array().push_back(table);
  arguments.set("tables", std::move(names));
  journal_->note(name_, "ensure_schema", std::move(arguments));
  for (const std::string& table : created) tables.try_emplace(table);
  return {};
}

bool MemoryEngine::fail_insert(const std::string& table) {
  const auto remaining = fail_inserts_.find(table);
  if (remaining == fail_inserts_.end() || remaining->second <= 0) return false;
  --remaining->second;
  return true;
}

void MemoryEngine::insert(const std::string& table, const Row& values) {
  Json arguments = Json::object();
  arguments.set("table", table);
  journal_->note(name_, "insert", std::move(arguments));
  if (fail_insert(table)) {
    throw EngineError("insert into " + table + " failed: this engine was told to refuse it");
  }
  tables[table].push_back(values);
}

void MemoryEngine::insert_many(const std::string& table, const std::vector<Row>& rows) {
  Json arguments = Json::object();
  arguments.set("table", table);
  arguments.set("rows", static_cast<std::int64_t>(rows.size()));
  journal_->note(name_, "insert_many", std::move(arguments));
  if (fail_insert(table)) {
    throw EngineError("batch insert failed: this engine was told to refuse it");
  }
  auto& stored = tables[table];
  stored.insert(stored.end(), rows.begin(), rows.end());
}

std::optional<Row> MemoryEngine::get(const std::string& table, const Row& key) {
  Json arguments = Json::object();
  arguments.set("table", table);
  journal_->note(name_, "get", std::move(arguments));
  const auto found = tables.find(table);
  if (found == tables.end()) return std::nullopt;
  for (const Row& row : found->second) {
    const bool matches = std::all_of(key.begin(), key.end(), [&row](const auto& item) {
      return column_of(row, item.first) == item.second;
    });
    if (matches) return row;
  }
  return std::nullopt;
}

void MemoryEngine::transaction(const std::function<void()>& body) {
  // A snapshot put back on failure: enough to make a rollback observable, which is what the
  // dual-write cases need - rows written in a transaction that throws must not reach the copy, and
  // the only way to check that is for the source to forget them too.
  journal_->note(name_, "transaction");
  auto snapshot = tables;
  try {
    body();
  } catch (...) {
    tables = std::move(snapshot);
    throw;
  }
}

std::optional<std::int64_t> MemoryEngine::map_watermark() {
  journal_->note(name_, "map_watermark");
  if (watermarks_.empty()) return std::nullopt;
  return *std::max_element(watermarks_.begin(), watermarks_.end());
}

void MemoryEngine::record_map_version(std::int64_t version, const std::string& /*model_version*/) {
  Json arguments = Json::object();
  arguments.set("version", version);
  journal_->note(name_, "record_map_version", std::move(arguments));
  watermarks_.push_back(version);
}

std::vector<Row> MemoryEngine::key_range(const std::string& table,
                                         const std::vector<std::string>& order,
                                         const std::optional<std::vector<Value>>& after,
                                         const std::optional<std::vector<Value>>& upto,
                                         std::optional<std::size_t> limit) {
  const std::vector<std::string> columns = key_columns(order, table);
  if (after) same_width(*after, columns, "after");
  if (upto) same_width(*upto, columns, "upto");
  Json arguments = Json::object();
  arguments.set("table", table);
  arguments.set("after", values_json(after));
  arguments.set("upto", values_json(upto));
  arguments.set("limit", limit ? Json(static_cast<std::int64_t>(*limit)) : Json(nullptr));
  journal_->note(name_, "key_range", std::move(arguments));

  std::vector<Row> rows;
  if (const auto found = tables.find(table); found != tables.end()) rows = found->second;
  std::stable_sort(rows.begin(), rows.end(), [&columns](const Row& left, const Row& right) {
    return key_of(columns, left) < key_of(columns, right);
  });
  std::vector<Row> out;
  const std::optional<std::vector<Sortable>> low =
      after ? std::optional(key_of(*after)) : std::nullopt;
  const std::optional<std::vector<Sortable>> high =
      upto ? std::optional(key_of(*upto)) : std::nullopt;
  for (Row& row : rows) {
    const std::vector<Sortable> key = key_of(columns, row);
    if (low && !(key > *low)) continue;
    if (high && !(key <= *high)) continue;
    if (limit && out.size() >= *limit) break;
    out.push_back(std::move(row));
  }
  return out;
}

std::optional<std::vector<Value>> MemoryEngine::nth_key(const std::string& table,
                                                        const std::vector<std::string>& order,
                                                        std::int64_t position) {
  const std::vector<std::string> columns = key_columns(order, table);
  Json arguments = Json::object();
  arguments.set("table", table);
  arguments.set("position", position);
  journal_->note(name_, "nth_key", std::move(arguments));
  if (position < 1) {
    throw EngineError("position is one-based; " + std::to_string(position) + " is not a row");
  }
  std::vector<Row> rows;
  if (const auto found = tables.find(table); found != tables.end()) rows = found->second;
  std::stable_sort(rows.begin(), rows.end(), [&columns](const Row& left, const Row& right) {
    return key_of(columns, left) < key_of(columns, right);
  });
  if (static_cast<std::size_t>(position) > rows.size()) return std::nullopt;
  const Row& row = rows[static_cast<std::size_t>(position - 1)];
  std::vector<Value> key;
  for (const std::string& column : columns) key.push_back(column_of(row, column));
  return key;
}

void MemoryEngine::copy_in(const std::string& table, const std::vector<Row>& rows) {
  Json arguments = Json::object();
  arguments.set("table", table);
  arguments.set("rows", static_cast<std::int64_t>(rows.size()));
  journal_->note(name_, "copy_in", std::move(arguments));
  if (rows.empty()) return;
  const std::vector<std::string> columns = sorted_names(rows.front());
  for (const Row& row : rows) {
    if (sorted_names(row) != columns) {
      throw EngineError("copy_in into " + table + " was given rows with different columns (" +
                        python_repr(columns) + " and " + python_repr(sorted_names(row)) +
                        "). A chunk comes from one table, so this is a caller assembling it from "
                        "two.");
    }
  }
  // Idempotent on the whole row's identity, which is what both real targets do by different means:
  // ON CONFLICT DO NOTHING in PostgreSQL, a ReplacingMergeTree collapse in ClickHouse.
  auto& existing = tables[table];
  for (const Row& row : rows) {
    const bool present = std::any_of(existing.begin(), existing.end(), [&row](const Row& held) {
      return std::all_of(row.begin(), row.end(), [&held](const auto& item) {
        return column_of(held, item.first) == item.second;
      });
    });
    if (!present) existing.push_back(row);
  }
}

std::uint64_t MemoryEngine::count(const std::string& table) {
  Json arguments = Json::object();
  arguments.set("table", table);
  journal_->note(name_, "count", std::move(arguments));
  const auto found = tables.find(table);
  return found == tables.end() ? 0U : found->second.size();
}

std::int64_t MemoryEngine::backfill_marker(const std::string& materialization,
                                           const std::string& entity) {
  Json arguments = Json::object();
  arguments.set("materialization", materialization);
  arguments.set("entity", entity);
  journal_->note(name_, "backfill_marker", std::move(arguments));
  const auto seen = markers_.find({materialization, entity});
  if (seen == markers_.end() || seen->second.empty()) return 0;
  return *std::max_element(seen->second.begin(), seen->second.end());
}

void MemoryEngine::record_backfill_marker(const std::string& materialization,
                                          const std::string& entity, std::int64_t rows) {
  Json arguments = Json::object();
  arguments.set("materialization", materialization);
  arguments.set("entity", entity);
  arguments.set("rows", rows);
  journal_->note(name_, "record_backfill_marker", std::move(arguments));
  markers_[{materialization, entity}].push_back(rows);
}

WriteFence MemoryEngine::write_fence(const std::string& table, const std::string& project_id) {
  const auto found = fences_.find(table);
  if (found == fences_.end()) {
    throw EngineError("no write fence is bound for " + table + " in " + name_);
  }
  return WriteFence(*found->second, table, project_id);
}

std::vector<PhysicalFinding> MemoryEngine::validate_schema(const PhysicalLayout& /*layout*/,
                                                           const Keys& /*keys*/) {
  return {};
}

// --- engines_from -------------------------------------------------------------------------------

std::map<std::string, std::unique_ptr<MemoryEngine>> engines_from(const Json& spec,
                                                                  std::shared_ptr<Recorded> journal) {
  if (!journal) journal = std::make_shared<Recorded>();
  std::map<std::string, std::unique_ptr<MemoryEngine>> built;
  for (const auto& [name, body] : spec.as_object()) {
    MemoryEngineOptions options;
    options.name = name;
    options.journal = journal;
    const auto flag = [&body](std::string_view key) {
      const Json* value = body.find(key);
      return value == nullptr || detail::python_truthy(*value);
    };
    if (const Json* dialect = body.find("dialect")) options.dialect = dialect->as_string();
    if (const Json* given = body.find("tables"); given != nullptr && !given->is_null()) {
      for (const auto& [table, rows] : given->as_object()) {
        auto& stored = options.tables[table];
        for (const Json& row : rows.as_array()) stored.push_back(row_from_json(row));
      }
    }
    options.can_keep_bookkeeping = flag("bookkeeping");
    options.can_migrate = flag("migratable");
    options.can_bulk_write = flag("bulk_writable");
    if (const Json* watermark = body.find("watermark"); watermark != nullptr && !watermark->is_null()) {
      options.watermark = *watermark->to_int64();
    }
    if (const Json* markers = body.find("markers"); markers != nullptr && !markers->is_null()) {
      for (const auto& [key, rows] : markers->as_object()) {
        const std::size_t bar = key.find('|');
        options.markers[{key.substr(0, bar), key.substr(bar + 1)}] = *rows.to_int64();
      }
    }
    if (const Json* failing = body.find("fail_inserts"); failing != nullptr && !failing->is_null()) {
      for (const auto& [table, count] : failing->as_object()) {
        options.fail_inserts[table] = *count.to_int64();
      }
    }
    built.emplace(name, std::make_unique<MemoryEngine>(std::move(options)));
  }
  return built;
}

}  // namespace sde::testing
