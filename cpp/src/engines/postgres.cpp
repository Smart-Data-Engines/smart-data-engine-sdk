/// The PostgreSQL adapter: the reference's `sde.engines.postgres`, method for method, over libpq.

#include "sde/postgres.hpp"

#include <unistd.h>

#include <algorithm>
#include <charconv>
#include <map>
#include <set>
#include <string>
#include <utility>
#include <vector>

#include "engines/postgres/connection.hpp"
#include "engines/postgres/fences.hpp"
#include "engines/postgres/values.hpp"
#include "python_compat.hpp"
#include "sde/errors.hpp"
#include "sde/json.hpp"
#include "sde/migration.hpp"
#include "sde/physical.hpp"
#include "sde/placement.hpp"
#include "sde/query.hpp"
#include "sde/schema.hpp"

namespace sde {

namespace {

using detail::python_repr;
using detail::postgres::Connection;
using detail::postgres::Result;
using detail::postgres::ServerError;
using Parameters = std::vector<std::optional<std::string>>;

std::string quote(std::string_view identifier) { return quote_identifier("postgres", identifier); }

/// What fixes a table whose text columns predate the reads' collation, as the reference says it.
constexpr std::string_view kTextCollationRemedy =
    "a staging into a fresh copy, whose text columns are created COLLATE \"C\"; or, by an "
    "administrator, ALTER COLUMN ... TYPE text COLLATE \"C\", which rebuilds the column's indexes "
    "under an exclusive lock on the table";

std::string joined(const std::vector<std::string>& parts, std::string_view separator) {
  std::string out;
  for (std::size_t i = 0; i < parts.size(); ++i) {
    if (i > 0) out += separator;
    out += parts[i];
  }
  return out;
}

/// `$1, $2, ...` from `first`.
std::string placeholders(std::size_t count, std::size_t first = 1) {
  std::vector<std::string> out;
  for (std::size_t i = 0; i < count; ++i) out.push_back("$" + std::to_string(first + i));
  return joined(out, ", ");
}

Row row_at(const Result& result, int row) {
  Row out;
  for (int column = 0; column < result.columns(); ++column) {
    out[std::string(result.name(column))] =
        result.is_null(row, column)
            ? Value(Null{})
            : detail::postgres::cell_value(result.text(row, column), result.type(column));
  }
  return out;
}

std::vector<Row> rows_from(const Result& result) {
  std::vector<Row> out;
  out.reserve(static_cast<std::size_t>(result.rows()));
  for (int row = 0; row < result.rows(); ++row) out.push_back(row_at(result, row));
  return out;
}

/// The columns of a batch: every row's, in name order, and the same in each.
std::vector<std::string> batch_columns(const std::vector<Row>& rows, std::string_view operation,
                                       const std::string& table) {
  if (rows.empty()) return {};
  std::vector<std::string> columns;
  for (const auto& [name, unused] : rows.front()) columns.push_back(name);
  for (const Row& row : rows) {
    std::vector<std::string> names;
    for (const auto& [name, unused] : row) names.push_back(name);
    if (names != columns) {
      throw EngineError(std::string(operation) + " into " + table +
                        " was given rows with different columns (" + python_repr(columns) +
                        " and " + python_repr(names) +
                        "). A chunk comes from one table, so this is a caller assembling it from "
                        "two.");
    }
  }
  return columns;
}

/// A JSON array of text from the server, as `array_to_json` writes it.
std::vector<std::string> json_texts(std::string_view text) {
  const Json parsed = parse_json(text);
  std::vector<std::string> out;
  for (const Json& item : parsed.as_array()) out.push_back(item.as_string());
  return out;
}

}  // namespace

// --- one thread at a time -----------------------------------------------------------------------

/// The reference's usage gate: an operation, or a transaction, owns the engine until it ends; the
/// thread that owns it may re-enter, and any other is refused before any I/O.
class PostgresEngine::Use {
 public:
  explicit Use(PostgresEngine& engine) : engine_(engine) {
    if (engine.process_ != static_cast<long>(getpid())) {
      throw ResourceClosed("create a fresh adapter after fork; the inherited connection is not usable");
    }
    const std::thread::id me = std::this_thread::get_id();
    std::thread::id nobody;
    if (!engine.owner_.compare_exchange_strong(nobody, me) && nobody != me) {
      throw ResourceBusy(engine.transactions_ > 0
                             ? "the connection is owned by another active transaction scope"
                             : "the connection already has an operation in progress");
    }
    ++engine.depth_;
  }
  ~Use() {
    if (--engine_.depth_ == 0) engine_.owner_.store(std::thread::id{});
  }
  Use(const Use&) = delete;
  Use& operator=(const Use&) = delete;

 private:
  PostgresEngine& engine_;
};

// --- connection ---------------------------------------------------------------------------------

PostgresEngine::PostgresEngine(std::string dsn, PostgresOptions options)
    : dsn_(std::move(dsn)),
      options_(std::move(options)),
      fences_(std::make_unique<detail::postgres::Fences>(*this)),
      process_(static_cast<long>(getpid())) {}

PostgresEngine::~PostgresEngine() = default;

void PostgresEngine::connect() {
  const Use use(*this);
  if (connection_ && unusable_) {
    throw EngineError("transaction completion was uncertain; close() then connect() before reuse");
  }
  if (connection_) return;
  auto connection = std::make_unique<Connection>(dsn_);
  // Dates and timestamps are read in the ISO style. The server reports the style it uses, so a
  // connection whose server defaults to another is set to ISO here rather than misread later.
  if (!connection->parameter_status("DateStyle").starts_with("ISO")) {
    (void)connection->execute("SET DateStyle TO ISO");
  }
  connection_ = std::move(connection);
  unusable_ = false;
}

void PostgresEngine::close() noexcept {
  connection_.reset();
  unusable_ = false;
}

bool PostgresEngine::connected() const noexcept { return connection_ != nullptr; }

Connection& PostgresEngine::cx() {
  if (unusable_) {
    throw EngineError("transaction completion was uncertain; close() then connect() before reuse");
  }
  if (!connection_) throw EngineError("not connected; call connect() first");
  if (!connection_->parameter_status("DateStyle").starts_with("ISO")) {
    throw EngineError(
        "the connection's DateStyle is " + python_repr(connection_->parameter_status("DateStyle")) +
        " and this adapter reads dates in the ISO style it set when it connected; something set "
        "it again since");
  }
  return *connection_;
}

std::string PostgresEngine::explain(const std::string& message) const {
  if (connection_ && connection_->lost()) {
    return message +
           ". The connection is gone and this library does not reopen one it was handed: call "
           "close() then connect() on the engine, or hand the session a new one. Nothing was "
           "retried, so no write reached the engine twice.";
  }
  return message;
}

std::vector<Row> PostgresEngine::rows_of(const std::string& sql, const Parameters& parameters,
                                         const std::string& refused) {
  try {
    return rows_from(cx().execute(sql, parameters));
  } catch (const ServerError& error) {
    throw EngineError(refused + ": " + explain(error.what()));
  }
}

Capabilities PostgresEngine::capabilities() noexcept {
  Capabilities offered;
  offered.query = this;
  offered.count = this;
  offered.summary = this;
  offered.bulk = this;
  offered.watermark = this;
  offered.migration = this;
  offered.fences = this;
  offered.schema = this;
  offered.storage = this;
  return offered;
}

WriteFence PostgresEngine::write_fence(const std::string& table, const std::string& project_id) {
  (void)cx();
  return WriteFence(*fences_, table, project_id);
}

// --- schema -------------------------------------------------------------------------------------

std::vector<PhysicalFinding> PostgresEngine::ensure_schema(const PhysicalLayout& layout,
                                                           const Keys& keys) {
  const Use use(*this);
  std::vector<std::string> tables;
  for (std::string& statement : schema_statements(layout, keys, "postgres")) {
    if (statement.starts_with("CREATE TABLE ")) tables.push_back(std::move(statement));
  }
  execute_schema(tables);
  verify_schema(layout);
  // Indexes only on tables whose key is the declared one. A table with another primary key belongs
  // to another design, and this map's refusal is coming; CREATE INDEX without CONCURRENTLY blocks
  // the table's writes while it builds, so running it first would be a refused operation that
  // still stopped the client's writes.
  std::set<std::string> blocked;
  for (const PhysicalFinding& finding : physical_findings(layout, keys)) {
    if (finding.aspect == "primary key") blocked.insert(finding.table);
  }
  PhysicalLayout applicable = layout;
  applicable.indexes.clear();
  for (const Index& index : layout.indexes) {
    const auto table = layout.tables.find(index.entity);
    if (table == layout.tables.end() || !blocked.contains(table->second)) {
      applicable.indexes.push_back(index);
    }
  }
  std::vector<std::string> indexes;
  for (std::string& statement : schema_statements(applicable, keys, "postgres")) {
    if (statement.starts_with("CREATE INDEX ")) indexes.push_back(std::move(statement));
  }
  execute_schema(indexes);
  Json fields = Json::object();
  fields.set("engine", "postgres");
  fields.set("statements", static_cast<std::int64_t>(tables.size() + indexes.size()));
  detail::emit(options_.log, "sde.schema.applied", fields);
  return physical_findings(layout, keys);
}

void PostgresEngine::execute_schema(const std::vector<std::string>& statements) {
  for (const std::string& statement : statements) {
    try {
      (void)cx().execute(statement);
    } catch (const ServerError& error) {
      throw EngineError("schema statement failed: " + statement + ": " + error.what());
    }
  }
}

std::vector<PhysicalFinding> PostgresEngine::validate_schema(const PhysicalLayout& layout,
                                                             const Keys& keys) {
  const Use use(*this);
  verify_schema(layout);
  return keys.empty() ? std::vector<PhysicalFinding>{} : physical_findings(layout, keys);
}

std::vector<PhysicalFinding> PostgresEngine::physical_findings(const PhysicalLayout& layout,
                                                               const Keys& keys) {
  const std::vector<DeclaredTable> declared = declared_tables(layout, keys);
  if (declared.empty()) return {};
  std::vector<std::string> names;
  for (const DeclaredTable& entry : declared) names.push_back(entry.table);
  struct Found {
    std::string method;
    std::vector<std::string> columns;
    bool simple;
    bool unique;
    bool usable;
  };
  std::map<std::string, std::vector<std::string>> primary;
  std::map<std::string, std::string> primary_index;
  std::map<std::string, std::map<std::string, Found>> indexes;
  std::map<std::pair<std::string, std::string>, std::vector<std::pair<std::string, std::string>>>
      collations;
  // The catalogues are readable by any login. An index with a predicate, an expression or INCLUDE
  // columns is not the index a layout declares, however its name reads; nor is a unique one, or
  // one an interrupted concurrent build left invalid. Arrays come back as JSON.
  const Result result = [&] {
    try {
      return cx().execute(
          "SELECT t.relname::text, ic.relname::text, i.indisprimary, am.amname::text, "
          "array_to_json(ARRAY(SELECT a.attname::text FROM unnest(i.indkey) WITH ORDINALITY "
          "k(num, pos) JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.num "
          "ORDER BY k.pos))::text, "
          "i.indpred IS NULL AND i.indexprs IS NULL AND i.indnkeyatts = i.indnatts, "
          "i.indisunique, i.indisvalid AND i.indisready, "
          "array_to_json(ARRAY(SELECT coalesce(co.collname::text, '') FROM unnest(i.indcollation) "
          "WITH ORDINALITY k(oid, pos) LEFT JOIN pg_collation co ON co.oid = k.oid "
          "ORDER BY k.pos))::text "
          "FROM pg_index i JOIN pg_class ic ON ic.oid = i.indexrelid "
          "JOIN pg_class t ON t.oid = i.indrelid JOIN pg_am am ON am.oid = ic.relam "
          "JOIN pg_namespace n ON n.oid = t.relnamespace "
          "WHERE n.nspname = current_schema() AND t.relname::text = ANY($1::text[])",
          {detail::postgres::text_array(names)});
    } catch (const ServerError& error) {
      throw EngineError("reading the physical design failed: " + explain(error.what()));
    }
  }();
  for (int row = 0; row < result.rows(); ++row) {
    const std::string table(result.text(row, 0));
    const std::string index(result.text(row, 1));
    const bool is_primary = result.text(row, 2) == "t";
    const std::vector<std::string> columns = json_texts(result.text(row, 4));
    const bool simple = result.text(row, 5) == "t";
    if (simple) {
      const std::vector<std::string> collated = json_texts(result.text(row, 8));
      auto& pairs = collations[{table, index}];
      for (std::size_t i = 0; i < std::min(columns.size(), collated.size()); ++i) {
        pairs.emplace_back(columns[i], collated[i]);
      }
    }
    if (is_primary) {
      primary[table] = columns;
      primary_index[table] = index;
    } else {
      indexes[table][index] =
          Found{std::string(result.text(row, 3)), columns, simple, result.text(row, 6) == "t",
                result.text(row, 7) == "t"};
    }
  }
  // A primary key or declared index whose text columns are in another collation than the reads'
  // cannot serve them: named, not refused, since provisioning and building on it are both right.
  std::map<std::string, std::set<std::string>> text_columns;
  for (const auto& [entity, table] : layout.tables) {
    const auto columns = layout.columns.find(entity);
    if (columns == layout.columns.end()) continue;
    for (const auto& [column, type] : columns->second) {
      if (type == "text") text_columns[table].insert(column);
    }
  }
  for (const DeclaredTable& entry : declared) {
    std::vector<std::string> checked;
    if (const auto found = primary_index.find(entry.table); found != primary_index.end()) {
      checked.push_back(found->second);
    }
    std::vector<std::string> declared_names;
    for (const DeclaredIndex& index : entry.indexes) declared_names.push_back(index.name);
    std::sort(declared_names.begin(), declared_names.end());
    checked.insert(checked.end(), declared_names.begin(), declared_names.end());
    for (const std::string& name : checked) {
      std::vector<std::string> stale;
      const auto pairs = collations.find({entry.table, name});
      if (pairs == collations.end()) continue;
      for (const auto& [column, collation] : pairs->second) {
        if (text_columns[entry.table].contains(column) && collation != "C") stale.push_back(column);
      }
      if (stale.empty()) continue;
      Json fields = Json::object();
      fields.set("table", entry.table);
      fields.set("index", name);
      Json columns = Json::array();
      for (const std::string& column : stale) columns.as_array().push_back(column);
      fields.set("columns", std::move(columns));
      fields.set("remedy", std::string(kTextCollationRemedy));
      detail::emit(options_.log, "sde.schema.text_collation", fields);
    }
  }
  std::vector<PhysicalFinding> findings;
  for (const DeclaredTable& entry : declared) {
    const auto found_key = primary.find(entry.table);
    if (found_key == primary.end() || found_key->second != entry.key) {
      findings.push_back({entry.table, "primary key", python_repr(entry.key),
                          found_key == primary.end() ? "absent" : python_repr(found_key->second)});
    }
    for (const DeclaredIndex& index : entry.indexes) {
      const std::string wanted = index.method + " on " + python_repr(index.columns);
      const auto table = indexes.find(entry.table);
      const Found* got = nullptr;
      if (table != indexes.end()) {
        if (const auto at = table->second.find(index.name); at != table->second.end()) {
          got = &at->second;
        }
      }
      if (got == nullptr) {
        findings.push_back({entry.table, "index " + index.name, wanted, "absent"});
        continue;
      }
      if (got->method != index.method || got->columns != index.columns || !got->simple ||
          got->unique || !got->usable) {
        std::string shape = got->simple ? "" : " with a predicate, expression or INCLUDE";
        if (got->unique) shape += ", unique";
        if (!got->usable) shape += ", not valid (an unfinished concurrent build)";
        findings.push_back({entry.table, "index " + index.name, wanted,
                            got->method + " on " + python_repr(got->columns) + shape});
      }
    }
  }
  return findings;
}

void PostgresEngine::verify_schema(const PhysicalLayout& layout) {
  std::map<std::string, std::map<std::string, std::string>> expected;
  for (const auto& [entity, table] : layout.tables) {
    const auto columns = layout.columns.find(entity);
    expected[table] = columns == layout.columns.end() ? std::map<std::string, std::string>{}
                                                      : columns->second;
  }
  if (expected.empty()) return;
  std::vector<std::string> names;
  for (const auto& [table, unused] : expected) names.push_back(table);
  // pg_attribute rather than information_schema, for format_type: the canonical spelling with the
  // modifier, which is what makes comparing types possible.
  Result result = [&] {
    try {
      return cx().execute(
          "SELECT c.relname::text, a.attname::text, format_type(a.atttypid, a.atttypmod) "
          "FROM pg_attribute a "
          "JOIN pg_class c ON c.oid = a.attrelid "
          "JOIN pg_namespace n ON n.oid = c.relnamespace "
          "WHERE n.nspname = current_schema() AND c.relname::text = ANY($1::text[]) "
          "AND a.attnum > 0 AND NOT a.attisdropped",
          {detail::postgres::text_array(names)});
    } catch (const ServerError& error) {
      throw EngineError("reading the schema failed: " + explain(error.what()));
    }
  }();
  std::map<std::string, std::map<std::string, std::string>> found;
  for (int row = 0; row < result.rows(); ++row) {
    found[std::string(result.text(row, 0))][std::string(result.text(row, 1))] =
        std::string(result.text(row, 2));
  }
  for (const auto& [table, columns] : expected) {
    const auto actual = found.find(table);
    if (actual == found.end()) {
      throw EngineError(python_repr(table) +
                        " does not exist after applying the schema. The statement reported "
                        "success, so this is a permissions or search_path problem rather than a "
                        "bad map.");
    }
    std::vector<std::string> missing;
    for (const auto& [column, unused] : columns) {
      if (!actual->second.contains(column)) missing.push_back(column);
    }
    if (!missing.empty()) {
      std::vector<std::string> present;
      for (const auto& [column, unused] : actual->second) present.push_back(column);
      throw EngineError(
          python_repr(table) + " already existed with a different shape: the map needs " +
          python_repr(missing) + " and the table has " + python_repr(present) +
          ". `CREATE TABLE IF NOT EXISTS` keeps whatever is there, so this table came from "
          "somewhere else - an older map, another application, a migration run by hand. Refusing "
          "here rather than at the first insert, which would fail in your request path with an "
          "error naming a column and not the cause.");
    }
    for (const auto& [column, declared] : columns) {
      const std::string& reported = actual->second.at(column);
      if (same_type(declared, reported)) continue;
      throw EngineError(
          table + "." + column + " is " + python_repr(reported) + " and this map declares it " +
          python_repr(declared) +
          ". `CREATE TABLE IF NOT EXISTS` keeps a table of that name whatever shape it is in, and "
          "this library never alters a column's type - so the table came from somewhere else, or "
          "from a map that rendered this column differently. Refusing rather than writing into "
          "it: a type that differs is either a write that fails in your request path or, worse, "
          "one that succeeds and hands the value back as something else.");
    }
    std::vector<std::string> extra;
    for (const auto& [column, unused] : actual->second) {
      if (!columns.contains(column)) extra.push_back(column);
    }
    if (!extra.empty()) {
      Json fields = Json::object();
      fields.set("table", table);
      Json listed = Json::array();
      for (const std::string& column : extra) listed.as_array().push_back(column);
      fields.set("columns", std::move(listed));
      detail::emit(options_.log, "sde.schema.extra_columns", fields);
    }
  }
}

bool PostgresEngine::same_type(const std::string& declared, const std::string& reported) {
  if (declared == reported) return true;
  // A modifier makes a literal mismatch a real one: to_regtype discards it, so numeric(12,2) and
  // numeric(8,2) would both resolve to numeric.
  if (declared.find('(') != std::string::npos || reported.find('(') != std::string::npos) {
    return false;
  }
  try {
    const Result result =
        cx().execute("SELECT to_regtype($1)::text, to_regtype($2)::text", {declared, reported});
    return result.rows() == 1 && !result.is_null(0, 0) && !result.is_null(0, 1) &&
           result.text(0, 0) == result.text(0, 1);
  } catch (const ServerError& error) {
    throw EngineError("reading the schema failed: " + explain(error.what()));
  }
}

// --- data ---------------------------------------------------------------------------------------

void PostgresEngine::insert(const std::string& table, const Row& values) {
  const Use use(*this);
  if (values.empty()) throw EngineError("nothing to insert");
  std::vector<std::string> columns;
  Parameters parameters;
  for (const auto& [column, value] : values) {
    columns.push_back(quote(column));
    parameters.push_back(detail::postgres::parameter_text(value));
  }
  const std::string sql = "INSERT INTO " + quote(table) + " (" + joined(columns, ", ") +
                          ") VALUES (" + placeholders(columns.size()) + ")";
  try {
    (void)cx().execute(sql, parameters);
  } catch (const ServerError& error) {
    // Surfaced, not swallowed and not rerouted.
    Json fields = Json::object();
    fields.set("table", table);
    fields.set("error", std::string(error.class_name()));
    detail::emit(options_.log, "sde.write.failed", fields);
    throw EngineError("insert into " + table + " failed: " + explain(error.what()));
  }
}

void PostgresEngine::insert_many(const std::string& table, const std::vector<Row>& rows) {
  const Use use(*this);
  const std::vector<std::string> columns = batch_columns(rows, "batch insert", table);
  if (columns.empty()) return;
  std::vector<std::string> quoted;
  for (const std::string& column : columns) quoted.push_back(quote(column));
  std::vector<std::string> tuples;
  Parameters parameters;
  for (const Row& row : rows) {
    tuples.push_back("(" + placeholders(columns.size(), parameters.size() + 1) + ")");
    for (const std::string& column : columns) {
      parameters.push_back(detail::postgres::parameter_text(row.at(column)));
    }
  }
  const std::string sql = "INSERT INTO " + quote(table) + " (" + joined(quoted, ", ") +
                          ") VALUES " + joined(tuples, ", ");
  try {
    (void)cx().execute(sql, parameters);
  } catch (const ServerError& error) {
    Json fields = Json::object();
    fields.set("table", table);
    fields.set("error", std::string(error.class_name()));
    detail::emit(options_.log, "sde.write.failed", fields);
    throw EngineError("batch insert into " + table + " failed: " + explain(error.what()));
  }
}

std::optional<Row> PostgresEngine::get(const std::string& table, const Row& key) {
  const Use use(*this);
  std::vector<std::string> clauses;
  Parameters parameters;
  for (const auto& [column, value] : key) {
    clauses.push_back(quote(column) + " = $" + std::to_string(clauses.size() + 1));
    parameters.push_back(detail::postgres::parameter_text(value));
  }
  const std::vector<Row> rows =
      rows_of("SELECT * FROM " + quote(table) + " WHERE " + joined(clauses, " AND "), parameters,
              "select from " + table + " failed");
  if (rows.empty()) return std::nullopt;
  return rows.front();
}

// --- the forward-only bookkeeping ---------------------------------------------------------------

std::optional<std::int64_t> PostgresEngine::map_watermark() {
  const Use use(*this);
  const std::string table(WATERMARK_TABLE);
  try {
    Connection& connection = cx();
    // Existing bookkeeping needs only read privileges: CREATE ... IF NOT EXISTS checks the schema's
    // CREATE permission even when the table exists, so its existence is read first.
    const Result existing = connection.execute("SELECT to_regclass($1)", {table});
    if (existing.rows() != 1) throw EngineError("watermark catalog lookup returned no result");
    if (existing.is_null(0, 0)) {
      (void)connection.execute("CREATE TABLE IF NOT EXISTS " + quote(table) + " (" +
                               quote("map_version") + " bigint NOT NULL, " +
                               quote("model_version") + " text NOT NULL, " + quote("seen_at") +
                               " timestamptz NOT NULL DEFAULT now())");
    }
    const Result highest =
        connection.execute("SELECT max(" + quote("map_version") + ") FROM " + quote(table));
    if (highest.rows() == 0 || highest.is_null(0, 0)) return std::nullopt;
    return std::get<std::int64_t>(detail::postgres::cell_value(highest.text(0, 0),
                                                               highest.type(0)));
  } catch (const ServerError& error) {
    throw EngineError("reading " + table + " failed: " + explain(error.what()));
  }
}

void PostgresEngine::record_map_version(std::int64_t version, const std::string& model_version) {
  const Use use(*this);
  const std::string table(WATERMARK_TABLE);
  try {
    (void)cx().execute("INSERT INTO " + quote(table) + " (" + quote("map_version") + ", " +
                           quote("model_version") + ") VALUES ($1, $2)",
                       {std::to_string(version), model_version});
  } catch (const ServerError& error) {
    throw EngineError("recording a map version in " + table + " failed: " + error.what());
  }
}

// --- logical reads ------------------------------------------------------------------------------

std::vector<Row> PostgresEngine::select_rows(const std::string& table, const ReadPlan& plan) {
  const Use use(*this);
  Parameters parameters;
  const std::string sql = read_sql(table, plan, "postgres", [&](const Value& value) {
    parameters.push_back(detail::postgres::parameter_text(value));
    return "$" + std::to_string(parameters.size());
  });
  std::vector<Row> rows = rows_of(sql, parameters, "logical scan of " + table + " failed");
  for (Row& row : rows) row = read_row(plan.columns, std::move(row));
  return rows;
}

std::uint64_t PostgresEngine::count_rows(const std::string& table, const ReadPlan& plan) {
  const Use use(*this);
  Parameters parameters;
  const std::string sql = read_sql(
      table, plan, "postgres",
      [&](const Value& value) {
        parameters.push_back(detail::postgres::parameter_text(value));
        return "$" + std::to_string(parameters.size());
      },
      true);
  try {
    const Result result = cx().execute(sql, parameters);
    if (result.rows() == 0) throw EngineError("count query returned no result");
    const std::string_view text = result.text(0, 0);
    std::uint64_t count = 0;
    const auto [end, problem] = std::from_chars(text.data(), text.data() + text.size(), count);
    if (problem != std::errc() || end != text.data() + text.size()) {
      throw EngineError("count query returned " + python_repr(text));
    }
    return count;
  } catch (const ServerError& error) {
    throw EngineError("logical count of " + table + " failed: " + explain(error.what()));
  }
}

SummaryRecord PostgresEngine::summarize_rows(const std::string& table, const ReadPlan& plan,
                                             const ReadColumn& column) {
  const Use use(*this);
  Parameters parameters;
  const std::string sql = summary_sql(table, plan, column, "postgres", [&](const Value& value) {
    parameters.push_back(detail::postgres::parameter_text(value));
    return "$" + std::to_string(parameters.size());
  });
  try {
    const Result result = cx().execute(sql, parameters);
    if (result.rows() == 0) throw EngineError("summary query returned no result");
    const auto cell = [&](int at) -> std::optional<std::string> {
      if (result.is_null(0, at)) return std::nullopt;
      return std::string(result.text(0, at));
    };
    return SummaryRecord{cell(0).value_or(""), cell(1).value_or(""), cell(2), cell(3), cell(4)};
  } catch (const ServerError& error) {
    throw EngineError("logical summary of " + table + " failed: " + explain(error.what()));
  }
}

std::map<std::string, std::pair<std::int64_t, std::int64_t>> PostgresEngine::storage_sizes(
    const std::vector<std::string>& tables) {
  const Use use(*this);
  if (tables.empty()) return {};
  std::map<std::string, std::pair<std::int64_t, std::int64_t>> out;
  try {
    // The table with its TOAST and every index, and the indexes other than the primary key; a
    // name the connection does not resolve is absent from the answer, not a zero.
    const Result result = cx().execute(
        "SELECT t.name, pg_total_relation_size(r.oid), "
        "COALESCE((SELECT sum(pg_relation_size(i.indexrelid)) FROM pg_index i "
        "WHERE i.indrelid = r.oid AND NOT i.indisprimary), 0) "
        "FROM unnest($1::text[]) AS t(name) "
        "JOIN pg_class r ON r.oid = to_regclass(quote_ident(t.name))",
        {detail::postgres::text_array(tables)});
    for (int row = 0; row < result.rows(); ++row) {
      const auto number = [&](int at) {
        const Value value = detail::postgres::cell_value(result.text(row, at), result.type(at));
        if (const auto* decimal = std::get_if<Decimal>(&value)) {
          return static_cast<std::int64_t>(std::stoll(decimal->to_string()));
        }
        return std::get<std::int64_t>(value);
      };
      out[std::string(result.text(row, 0))] = {number(1), number(2)};
    }
  } catch (const ServerError& error) {
    throw EngineError("storage sizes could not be read: " + explain(error.what()));
  }
  return out;
}

// --- migration ----------------------------------------------------------------------------------

std::uint64_t PostgresEngine::count(const std::string& table) {
  const Use use(*this);
  try {
    const Result result = cx().execute("SELECT count(*) FROM " + quote(table));
    if (result.rows() == 0) return 0;
    return static_cast<std::uint64_t>(
        std::get<std::int64_t>(detail::postgres::cell_value(result.text(0, 0), result.type(0))));
  } catch (const ServerError& error) {
    throw EngineError("count on " + table + " failed: " + error.what());
  }
}

std::vector<Row> PostgresEngine::key_range(const std::string& table,
                                           const std::vector<std::string>& order,
                                           const std::optional<std::vector<Value>>& after,
                                           const std::optional<std::vector<Value>>& upto,
                                           std::optional<std::size_t> limit) {
  const Use use(*this);
  // Row-value comparison, never a hand-rolled disjunction over the key's columns: the disjunction
  // is where composite-key pagination goes wrong, by skipping rows. `after` is exclusive, a resume
  // point; `upto` inclusive, the last row of a chunk read elsewhere.
  const std::vector<std::string> columns = key_columns(order, table);
  std::vector<std::string> quoted;
  for (const std::string& column : columns) quoted.push_back(quote(column));
  const std::string tuple = "(" + joined(quoted, ", ") + ")";
  std::vector<std::string> clauses;
  Parameters parameters;
  if (after) {
    same_width(*after, columns, "after");
    clauses.push_back(tuple + " > (" + placeholders(columns.size(), parameters.size() + 1) + ")");
    for (const Value& value : *after) parameters.push_back(detail::postgres::parameter_text(value));
  }
  if (upto) {
    same_width(*upto, columns, "upto");
    clauses.push_back(tuple + " <= (" + placeholders(columns.size(), parameters.size() + 1) + ")");
    for (const Value& value : *upto) parameters.push_back(detail::postgres::parameter_text(value));
  }
  std::string sql = "SELECT * FROM " + quote(table);
  if (!clauses.empty()) sql += " WHERE " + joined(clauses, " AND ");
  sql += " ORDER BY " + tuple;
  if (limit) {
    parameters.push_back(std::to_string(*limit));
    sql += " LIMIT $" + std::to_string(parameters.size());
  }
  try {
    return rows_from(cx().execute(sql, parameters));
  } catch (const ServerError& error) {
    throw EngineError("key range select from " + table + " failed: " + error.what());
  }
}

std::optional<std::vector<Value>> PostgresEngine::nth_key(const std::string& table,
                                                          const std::vector<std::string>& order,
                                                          std::int64_t position) {
  const Use use(*this);
  // An OFFSET scan, paid once per resume rather than once per chunk.
  const std::vector<std::string> columns = key_columns(order, table);
  if (position < 1) {
    throw EngineError("position is one-based; " + std::to_string(position) + " is not a row");
  }
  std::vector<std::string> quoted;
  for (const std::string& column : columns) quoted.push_back(quote(column));
  const std::string projection = joined(quoted, ", ");
  try {
    const Result result =
        cx().execute("SELECT " + projection + " FROM " + quote(table) + " ORDER BY (" +
                         projection + ") OFFSET $1 LIMIT 1",
                     {std::to_string(position - 1)});
    if (result.rows() == 0) return std::nullopt;
    std::vector<Value> key;
    for (int column = 0; column < result.columns(); ++column) {
      key.push_back(result.is_null(0, column)
                        ? Value(Null{})
                        : detail::postgres::cell_value(result.text(0, column),
                                                       result.type(column)));
    }
    return key;
  } catch (const ServerError& error) {
    throw EngineError("reading row " + std::to_string(position) + " of " + table +
                      " failed: " + error.what());
  }
}

void PostgresEngine::copy_in(const std::string& table, const std::vector<Row>& rows) {
  const Use use(*this);
  // ON CONFLICT DO NOTHING is what makes a chunk idempotent, and idempotence what makes a backfill
  // resumable: the marker is written after the chunk, so a crash between them costs a recopy.
  const std::vector<std::string> columns = batch_columns(rows, "copy_in", table);
  if (columns.empty()) return;
  std::vector<std::string> quoted;
  for (const std::string& column : columns) quoted.push_back(quote(column));
  std::vector<std::string> tuples;
  Parameters parameters;
  for (const Row& row : rows) {
    tuples.push_back("(" + placeholders(columns.size(), parameters.size() + 1) + ")");
    for (const std::string& column : columns) {
      parameters.push_back(detail::postgres::parameter_text(row.at(column)));
    }
  }
  try {
    (void)cx().execute("INSERT INTO " + quote(table) + " (" + joined(quoted, ", ") + ") VALUES " +
                           joined(tuples, ", ") + " ON CONFLICT DO NOTHING",
                       parameters);
  } catch (const ServerError& error) {
    Json fields = Json::object();
    fields.set("table", table);
    fields.set("error", std::string(error.class_name()));
    detail::emit(options_.log, "sde.write.failed", fields);
    throw EngineError("copying " + std::to_string(rows.size()) + " rows into " + table +
                      " failed: " + error.what());
  }
}

std::int64_t PostgresEngine::backfill_marker(const std::string& materialization,
                                             const std::string& entity) {
  const Use use(*this);
  const std::string table(BACKFILL_TABLE);
  try {
    Connection& connection = cx();
    (void)connection.execute("CREATE TABLE IF NOT EXISTS " + quote(table) + " (" +
                             quote("materialization") + " text NOT NULL, " + quote("entity") +
                             " text NOT NULL, " + quote("rows_copied") + " bigint NOT NULL, " +
                             quote("at") + " timestamptz NOT NULL DEFAULT now())");
    const Result result = connection.execute(
        "SELECT max(" + quote("rows_copied") + ") FROM " + quote(table) + " WHERE " +
            quote("materialization") + " = $1 AND " + quote("entity") + " = $2",
        {materialization, entity});
    if (result.rows() == 0 || result.is_null(0, 0)) return 0;
    return std::get<std::int64_t>(detail::postgres::cell_value(result.text(0, 0), result.type(0)));
  } catch (const ServerError& error) {
    throw EngineError("reading " + table + " failed: " + error.what());
  }
}

void PostgresEngine::record_backfill_marker(const std::string& materialization,
                                            const std::string& entity, std::int64_t rows) {
  const Use use(*this);
  const std::string table(BACKFILL_TABLE);
  try {
    (void)cx().execute("INSERT INTO " + quote(table) + " (" + quote("materialization") + ", " +
                           quote("entity") + ", " + quote("rows_copied") + ") VALUES ($1, $2, $3)",
                       {materialization, entity, std::to_string(rows)});
  } catch (const ServerError& error) {
    throw EngineError("recording backfill progress in " + table + " failed: " + error.what());
  }
}

// --- transactions -------------------------------------------------------------------------------

void PostgresEngine::transaction(const std::function<void()>& body) {
  const Use use(*this);
  Connection& connection = cx();
  const bool outer = transactions_ == 0;
  const std::string savepoint = "sde_scope_" + std::to_string(++savepoints_);
  ++transactions_;
  enum class Phase { opening, running, closing } phase = Phase::opening;
  try {
    (void)connection.execute(outer ? "BEGIN" : "SAVEPOINT " + savepoint);
    phase = Phase::running;
    body();
    if (connection.transaction_status() == PQTRANS_INERROR) {
      throw EngineError("the transaction is aborted and cannot be reported as committed");
    }
    phase = Phase::closing;
    const Result done = connection.execute(outer ? "COMMIT" : "RELEASE SAVEPOINT " + savepoint);
    // A COMMIT of an aborted transaction succeeds with a ROLLBACK tag.
    if (outer && done.tag() != "COMMIT") {
      throw EngineError("the transaction is aborted and cannot be reported as committed");
    }
    --transactions_;
  } catch (...) {
    --transactions_;
    bool rolled_back = true;
    try {
      (void)connection.execute(outer ? "ROLLBACK" : "ROLLBACK TO SAVEPOINT " + savepoint);
      if (!outer) (void)connection.execute("RELEASE SAVEPOINT " + savepoint);
    } catch (const ServerError&) {
      rolled_back = false;
    }
    const PGTransactionStatusType status = connection.transaction_status();
    if (!rolled_back || (outer && status != PQTRANS_IDLE) ||
        (!outer && status != PQTRANS_INTRANS)) {
      unusable_ = true;
    }
    if (phase != Phase::running) {
      unusable_ = true;
      throw EngineError("transaction completion was uncertain; close() then connect() before reuse");
    }
    throw;
  }
}

}  // namespace sde
