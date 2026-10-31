#include "sde/clickhouse.hpp"

#include <unistd.h>

#include <algorithm>
#include <map>
#include <set>
#include <string>
#include <utility>
#include <variant>
#include <vector>

#include "bulk.hpp"
#include "engines/clickhouse/dsn.hpp"
#include "engines/clickhouse/fences.hpp"
#include "engines/clickhouse/http.hpp"
#include "engines/clickhouse/values.hpp"
#include "physical_internal.hpp"
#include "python_compat.hpp"
#include "sde/errors.hpp"
#include "sde/migration.hpp"
#include "sde/physical.hpp"
#include "sde/placement.hpp"
#include "sde/query.hpp"
#include "sde/schema.hpp"
#include "storage_internal.hpp"

namespace sde {

namespace {

using detail::python_repr;
using detail::clickhouse::Http;
using detail::clickhouse::kNotReplayed;
using detail::clickhouse::literal;
using detail::clickhouse::ServerError;
using detail::clickhouse::TransportFailure;

std::string quote(std::string_view identifier) {
  return quote_identifier("clickhouse", identifier);
}

/// What an exchange's failure says after the operation's own words, as the reference's `{exc}`.
std::string failure(const std::exception& error) {
  if (dynamic_cast<const TransportFailure*>(&error) != nullptr) return std::string(kNotReplayed);
  return error.what();
}

/// The class name the reference logs in `sde.write.failed`: its driver's for a server's refusal,
/// its own for a transport that failed.
std::string_view failure_class(const std::exception& error) {
  if (dynamic_cast<const ServerError*>(&error) != nullptr) return "DatabaseError";
  return "EngineError";
}

/// The reference lets its driver's error through where an operation adds no words of its own, as
/// the schema check does. Here that is an `EngineError` with the same text, so nothing outside the
/// contract's classes leaves the adapter.
template <typename Body>
auto as_engine_error(const Body& body) -> decltype(body()) {
  try {
    return body();
  } catch (const ServerError& error) {
    throw EngineError(error.what());
  } catch (const TransportFailure&) {
    throw EngineError(std::string(kNotReplayed));
  }
}

/// `[...]` of literals: how the reference's driver binds a list.
std::string array_literal(const std::vector<std::string>& items) {
  std::string out = "[";
  for (std::size_t i = 0; i < items.size(); ++i) {
    if (i != 0) out += ", ";
    out += literal(Value(items[i]));
  }
  return out + "]";
}

std::vector<std::string> sorted_names(const Row& row) {
  std::vector<std::string> names;
  for (const auto& [name, unused] : row) names.push_back(name);
  return names;  // a Row is ordered by name already
}

const std::string& text_at(const std::vector<Value>& row, std::size_t index) {
  if (index >= row.size() || !std::holds_alternative<std::string>(row[index])) {
    throw EngineError("ClickHouse returned an unexpected answer");
  }
  return std::get<std::string>(row[index]);
}

std::int64_t integer_at(const std::vector<Value>& row, std::size_t index) {
  if (index >= row.size() || !std::holds_alternative<std::int64_t>(row[index])) {
    throw EngineError("ClickHouse returned an unexpected answer");
  }
  return std::get<std::int64_t>(row[index]);
}

const std::string& text_cell(const Row& row, std::string_view column) {
  const auto found = row.find(column);
  if (found == row.end() || !std::holds_alternative<std::string>(found->second)) {
    throw EngineError("ClickHouse returned an unexpected answer");
  }
  return std::get<std::string>(found->second);
}

}  // namespace

// --- the usage gate -----------------------------------------------------------------------------

class ClickHouseEngine::Use {
 public:
  explicit Use(ClickHouseEngine& engine) : engine_(engine) {
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
  ClickHouseEngine& engine_;
};

// --- connection ---------------------------------------------------------------------------------

ClickHouseEngine::ClickHouseEngine(std::string_view dsn, ClickHouseOptions options)
    : target_(std::make_unique<detail::clickhouse::Target>(detail::clickhouse::parse_dsn(dsn))),
      options_(std::move(options)),
      fences_(std::make_unique<detail::clickhouse::Fences>(*this)),
      process_(static_cast<long>(getpid())) {}

ClickHouseEngine::~ClickHouseEngine() = default;

void ClickHouseEngine::connect() {
  const Use use(*this);
  if (http_) return;
  try {
    auto opened = std::make_unique<Http>(*target_);
    const detail::clickhouse::Answer handshake = detail::clickhouse::decode_answer(
        opened->post("SELECT version()", "RowBinaryWithNamesAndTypes", {}, true));
    if (handshake.rows.size() != 1 || handshake.rows[0].size() != 1 ||
        !std::holds_alternative<std::string>(handshake.rows[0][0])) {
      throw ServerError(std::nullopt, "");
    }
    version_ = std::get<std::string>(handshake.rows[0][0]);
    http_ = std::move(opened);
  } catch (const TransportFailure&) {
    throw EngineError("could not connect to ClickHouse: " + std::string(kNotReplayed));
  } catch (const EngineError& error) {
    // The CA's refusal, in its own words.
    throw EngineError(std::string("could not connect to ClickHouse: ") + error.what());
  } catch (const std::exception&) {
    // A server's answer at this point can echo the URL, a credential or its own content, so it
    // is not repeated; the connection error proves nothing was changed.
    throw EngineError("could not connect to ClickHouse with the configured transport");
  }
}

void ClickHouseEngine::close() noexcept {
  http_.reset();
  version_.clear();
}

bool ClickHouseEngine::connected() const noexcept { return http_ != nullptr; }

const Http& ClickHouseEngine::http() const {
  if (!http_) throw EngineError("not connected; call connect() first");
  return *http_;
}

ClickHouseEngine::Answer ClickHouseEngine::answer(const std::string& sql,
                                                  std::string_view settings) const {
  detail::clickhouse::Answer decoded = detail::clickhouse::decode_answer(
      http().post(sql, "RowBinaryWithNamesAndTypes", {}, false, settings));
  return Answer{std::move(decoded.names), std::move(decoded.rows)};
}

std::vector<Row> ClickHouseEngine::query(const std::string& sql) const {
  const Answer found = answer(sql);
  std::vector<Row> rows;
  for (const std::vector<Value>& values : found.rows) {
    Row row;
    for (std::size_t i = 0; i < found.names.size(); ++i) row[found.names[i]] = values[i];
    rows.push_back(std::move(row));
  }
  return rows;
}

void ClickHouseEngine::command(const std::string& sql) const { (void)http().post(sql); }

void ClickHouseEngine::insert_rows(const std::string& table, const std::vector<std::string>& columns,
                                   const std::vector<Row>& rows, std::string_view settings) const {
  // The columns' types as the table has them now, as the reference's driver asks before it writes:
  // they go in the header, and the server checks the header against the table.
  std::map<std::string, std::string> described;
  for (const std::vector<Value>& column : answer("DESCRIBE TABLE " + quote(table)).rows) {
    described[text_at(column, 0)] = text_at(column, 1);
  }
  std::vector<std::string> types;
  for (const std::string& column : columns) {
    const auto found = described.find(column);
    if (found == described.end()) {
      throw EngineError("Unrecognized column " + python_repr(column) + " in table " + quote(table));
    }
    types.push_back(found->second);
  }
  std::vector<std::vector<Value>> values;
  for (const Row& row : rows) {
    std::vector<Value> ordered;
    for (const std::string& column : columns) ordered.push_back(row.at(column));
    values.push_back(std::move(ordered));
  }
  std::string names;
  for (std::size_t i = 0; i < columns.size(); ++i) names += (i == 0 ? "" : ", ") + quote(columns[i]);
  (void)http().post("INSERT INTO " + quote(table) + " (" + names + ")", "RowBinaryWithNamesAndTypes",
                    detail::clickhouse::encode_rows(columns, types, values), false, settings);
}

void ClickHouseEngine::write_failed(const std::string& table, const std::exception& error) const {
  Json fields = Json::object();
  fields.set("table", table);
  fields.set("error", std::string(failure_class(error)));
  detail::emit(options_.log, "sde.write.failed", fields);
}

Capabilities ClickHouseEngine::capabilities() noexcept {
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

WriteFence ClickHouseEngine::write_fence(const std::string& table, const std::string& project_id) {
  (void)http();
  return WriteFence(*fences_, table, project_id);
}

void ClickHouseEngine::transaction(const std::function<void()>& /*body*/) {
  throw EngineError(
      "ClickHouse has no multi-statement transactions, so this adapter will not pretend to start "
      "one. If these writes have to commit together, declare it: `atomic_with` on the entities "
      "makes them one colocation group, one group is one engine, and the planner is then not "
      "permitted to put them here. A silent no-op context manager would let the writes proceed and "
      "let you believe they were atomic.");
}

// --- schema -------------------------------------------------------------------------------------

std::vector<PhysicalFinding> ClickHouseEngine::ensure_schema(const PhysicalLayout& layout,
                                                             const Keys& keys) {
  const Use use(*this);
  const std::vector<std::string> statements = schema_statements(layout, keys, "clickhouse");
  for (const std::string& statement : statements) {
    try {
      command(statement);
    } catch (const std::exception& error) {
      throw EngineError("schema statement failed: " + statement + ": " + failure(error));
    }
  }
  Json fields = Json::object();
  fields.set("engine", "clickhouse");
  fields.set("statements", static_cast<std::int64_t>(statements.size()));
  detail::emit(options_.log, "sde.schema.applied", fields);
  return as_engine_error([&] {
    verify_schema(layout);
    return physical_findings(layout, keys);
  });
}

std::vector<PhysicalFinding> ClickHouseEngine::validate_schema(const PhysicalLayout& layout,
                                                               const Keys& keys) {
  const Use use(*this);
  return as_engine_error([&] {
    verify_schema(layout);
    return keys.empty() ? std::vector<PhysicalFinding>{} : physical_findings(layout, keys);
  });
}

std::vector<PhysicalFinding> ClickHouseEngine::physical_findings(const PhysicalLayout& layout,
                                                                 const Keys& keys) {
  const std::vector<DeclaredTable> declared = declared_tables(layout, keys);
  if (declared.empty()) return {};
  std::vector<std::string> tables;
  std::vector<std::string> indexed;
  for (const DeclaredTable& entry : declared) {
    tables.push_back(entry.table);
    if (!entry.indexes.empty()) indexed.push_back(entry.table);
  }
  std::sort(indexed.begin(), indexed.end());
  std::map<std::string, std::pair<std::string, std::string>> keys_found;
  for (const std::vector<Value>& row :
       answer("SELECT name, sorting_key, partition_key FROM system.tables "
              "WHERE database = currentDatabase() AND name IN " +
              array_literal(tables))
           .rows) {
    keys_found[text_at(row, 0)] = {text_at(row, 1), text_at(row, 2)};
  }
  struct Present {
    std::string type_full;
    std::string expression;
    std::int64_t granularity;
  };
  std::map<std::string, std::map<std::string, Present>> present;
  std::string unreadable;
  // Read only when something is declared: system.tables and system.columns are filtered by the
  // login's own grants, and system.data_skipping_indices needs one of its own (code 497).
  if (!indexed.empty()) {
    std::vector<std::vector<Value>> rows;
    try {
      rows = answer("SELECT table, name, type_full, expr, granularity "
                    "FROM system.data_skipping_indices "
                    "WHERE database = currentDatabase() AND table IN " +
                    array_literal(indexed))
                 .rows;
    } catch (const ServerError& error) {
      if (error.code() != 497) throw;
      // Unverified, not matching: a session reports it, a person provisioning refuses on it.
      unreadable =
          "unverified: this login cannot read system.data_skipping_indices "
          "(GRANT SELECT ON system.data_skipping_indices to verify it)";
    }
    for (const std::vector<Value>& row : rows) {
      present[text_at(row, 0)][text_at(row, 1)] =
          Present{text_at(row, 2), text_at(row, 3), integer_at(row, 4)};
    }
  }
  std::vector<PhysicalFinding> findings;
  for (const DeclaredTable& entry : declared) {
    const auto described = keys_found.find(entry.table);
    if (described == keys_found.end()) continue;  // verify_schema has refused a missing table
    const auto& [sorting, partition] = described->second;
    const auto found_key = detail::parse_identifier_list(sorting);
    if (!found_key || *found_key != entry.key) {
      findings.push_back({entry.table, "sort key", python_repr(entry.key),
                          found_key ? detail::python_tuple_repr(*found_key) : python_repr(sorting)});
    }
    const auto found_partition = detail::parse_partition_key(partition);
    const auto partition_text = [](const std::optional<std::pair<std::string, std::string>>& value) {
      return value ? detail::python_tuple_repr({value->first, value->second}) : std::string("None");
    };
    const bool partition_matches =
        found_partition &&
        (*found_partition ? entry.partition && (*found_partition)->function == entry.partition->first &&
                                (*found_partition)->field == entry.partition->second
                          : !entry.partition);
    if (!partition_matches) {
      std::string found_text;
      if (!found_partition) {
        found_text = python_repr(partition);
      } else if (*found_partition) {
        found_text = detail::python_tuple_repr({(*found_partition)->function, (*found_partition)->field});
      } else {
        found_text = "None";
      }
      findings.push_back({entry.table, "partition", partition_text(entry.partition), found_text});
    }
    for (const DeclaredIndex& index : entry.indexes) {
      const std::string wanted =
          index.type_full + " on " + python_repr(index.columns) + " granularity " +
          (index.granularity ? std::to_string(*index.granularity) : std::string("None"));
      const auto table = present.find(entry.table);
      const Present* got = nullptr;
      if (table != present.end()) {
        if (const auto at = table->second.find(index.name); at != table->second.end()) {
          got = &at->second;
        }
      }
      if (got == nullptr) {
        findings.push_back({entry.table, "index " + index.name, wanted,
                            unreadable.empty() ? std::string("absent") : unreadable});
        continue;
      }
      const auto columns = detail::parse_identifier_list(got->expression);
      if (got->type_full != index.type_full || !columns || *columns != index.columns ||
          !index.granularity || got->granularity != *index.granularity) {
        findings.push_back({entry.table, "index " + index.name, wanted,
                            got->type_full + " on " +
                                (columns ? detail::python_tuple_repr(*columns)
                                         : python_repr(got->expression)) +
                                " granularity " + std::to_string(got->granularity)});
      }
    }
  }
  return findings;
}

void ClickHouseEngine::verify_schema(const PhysicalLayout& layout) {
  std::map<std::string, std::map<std::string, std::string>> expected;
  for (const auto& [entity, table] : layout.tables) {
    const auto columns = layout.columns.find(entity);
    expected[table] = columns == layout.columns.end() ? std::map<std::string, std::string>{}
                                                      : columns->second;
  }
  if (expected.empty()) return;
  std::vector<std::string> names;
  for (const auto& [table, unused] : expected) names.push_back(table);
  std::map<std::string, std::map<std::string, std::string>> found;
  for (const std::vector<Value>& row : answer("SELECT table, name, type FROM system.columns "
                                              "WHERE database = currentDatabase() AND table IN " +
                                              array_literal(names))
                                           .rows) {
    found[text_at(row, 0)][text_at(row, 1)] = text_at(row, 2);
  }
  for (const auto& [table, columns] : expected) {
    const auto actual = found.find(table);
    if (actual == found.end()) {
      throw EngineError(python_repr(table) +
                        " does not exist after applying the schema. The statement reported "
                        "success, so this is a permissions or database-selection problem rather "
                        "than a bad map.");
    }
    std::vector<std::string> missing;
    for (const auto& [column, unused] : columns) {
      if (!actual->second.contains(column)) missing.push_back(column);
    }
    if (!missing.empty()) {
      std::vector<std::string> has;
      for (const auto& [column, unused] : actual->second) has.push_back(column);
      throw EngineError(python_repr(table) + " already existed with a different shape: the map needs " +
                        python_repr(missing) + " and the table has " + python_repr(has) +
                        ". `CREATE TABLE IF NOT EXISTS` keeps whatever is there, so this table came "
                        "from somewhere else. Refusing here rather than at the first insert, which "
                        "would fail in your request path with an error naming a column and not the "
                        "cause.");
    }
    for (const auto& [column, declared] : columns) {
      const std::string& reported = actual->second.at(column);
      if (reported == declared) continue;
      throw EngineError(
          table + "." + column + " is " + python_repr(reported) + " and this map declares it " +
          python_repr(declared) +
          ". `CREATE TABLE IF NOT EXISTS` keeps a table of that name whatever shape it is in, and "
          "this library never alters a column's type - so the table came from somewhere else, or "
          "from a map that rendered this column differently. Refusing rather than writing into it: "
          "with a timestamp the difference is usually precision, and a write that succeeds and "
          "comes back rounded is worse than one that fails.");
    }
    // The generation column is the library's own, added by provisioning for a map with write
    // generations: naming it fired the event on every start of such a deployment (finding 21).
    std::vector<std::string> extra;
    for (const auto& [column, unused] : actual->second) {
      if (!columns.contains(column) && column != EPOCH_COLUMN) extra.push_back(column);
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

// --- data ---------------------------------------------------------------------------------------

void ClickHouseEngine::insert(const std::string& table, const Row& values) {
  const Use use(*this);
  if (values.empty()) throw EngineError("nothing to insert");
  try {
    insert_rows(table, sorted_names(values), {values});
  } catch (const std::exception& error) {
    // Surfaced, not swallowed and not rerouted: a write that did not happen is not this library's
    // problem to absorb.
    write_failed(table, error);
    throw EngineError("insert into " + table + " failed: " + failure(error));
  }
}

void ClickHouseEngine::insert_many(const std::string& table, const std::vector<Row>& rows) {
  const Use use(*this);
  const std::vector<std::string> columns = detail::batch_columns(rows);
  if (columns.empty()) return;
  try {
    insert_rows(table, columns, rows);
  } catch (const std::exception& error) {
    write_failed(table, error);
    throw EngineError("batch insert into " + table + " failed: " + failure(error));
  }
}

std::optional<Row> ClickHouseEngine::get(const std::string& table, const Row& key) {
  const Use use(*this);
  // FINAL: without it a key saved twice returns whichever duplicate the scan reaches first.
  std::string where;
  for (const auto& [column, value] : key) {
    where += (where.empty() ? "" : " AND ") + quote(column) + " = " + literal(value);
  }
  try {
    const std::vector<Row> rows =
        query("SELECT * FROM " + quote(table) + " FINAL WHERE " + where + " LIMIT 1");
    if (rows.empty()) return std::nullopt;
    return rows.front();
  } catch (const std::exception& error) {
    throw EngineError("select from " + table + " failed: " + failure(error));
  }
}

std::map<std::string, std::pair<std::int64_t, std::int64_t>> ClickHouseEngine::storage_sizes(
    const std::vector<std::string>& tables) {
  const Use use(*this);
  if (tables.empty()) return {};
  std::map<std::string, std::pair<std::int64_t, std::int64_t>> out;
  try {
    // system.tables says which of the names exist, readable by a runtime login for its own
    // tables; system.parts what their active parts occupy, which needs the column grant.
    for (const std::vector<Value>& row :
         answer("SELECT t.name, sum(p.bytes_on_disk), "
                "sum(p.secondary_indices_compressed_bytes + p.secondary_indices_marks_bytes) "
                "FROM system.tables AS t LEFT JOIN ("
                "SELECT table, bytes_on_disk, secondary_indices_compressed_bytes, "
                "secondary_indices_marks_bytes FROM system.parts "
                "WHERE active AND database = currentDatabase()"
                ") AS p ON p.table = t.name "
                "WHERE t.database = currentDatabase() AND t.name IN " +
                    array_literal(tables) + " GROUP BY t.name",
                "&join_use_nulls=0")
             .rows) {
      out[text_at(row, 0)] = {integer_at(row, 1), integer_at(row, 2)};
    }
  } catch (const std::exception& error) {
    const std::string message = "storage sizes could not be read: " + failure(error);
    if (const auto* refused = dynamic_cast<const ServerError*>(&error);
        refused != nullptr && refused->code() == 497) {
      throw detail::CatalogueRefused(message);
    }
    throw EngineError(message);
  }
  return out;
}

// --- the forward-only bookkeeping ---------------------------------------------------------------

std::optional<std::int64_t> ClickHouseEngine::map_watermark() {
  const Use use(*this);
  const std::string table(WATERMARK_TABLE);
  std::vector<std::vector<Value>> highest;
  try {
    const std::vector<std::vector<Value>> existing = answer("EXISTS TABLE " + quote(table)).rows;
    if (existing.size() != 1 || existing[0].size() != 1) {
      throw EngineError("watermark catalog lookup returned no presence result");
    }
    const std::int64_t presence = integer_at(existing[0], 0);
    if (presence != 0 && presence != 1) {
      throw EngineError("watermark catalog lookup returned no presence result");
    }
    if (presence == 0) {
      command("CREATE TABLE IF NOT EXISTS " + quote(table) + " (" + quote("map_version") +
              " Int64, " + quote("model_version") + " String, " + quote("seen_at") +
              " DateTime64(3, 'UTC') DEFAULT now64(3, 'UTC')) ENGINE = MergeTree ORDER BY (" +
              quote("map_version") + ")");
    }
    highest = answer("SELECT max(" + quote("map_version") + ") FROM " + quote(table)).rows;
  } catch (const std::exception& error) {
    throw EngineError("reading " + table + " failed: " + failure(error));
  }
  if (highest.empty() || highest[0].empty() || std::holds_alternative<Null>(highest[0][0])) {
    return std::nullopt;
  }
  // An empty MergeTree answers max() with 0, which here means no map has been applied.
  const std::int64_t value = integer_at(highest[0], 0);
  if (value > 0) return value;
  return std::nullopt;
}

void ClickHouseEngine::record_map_version(std::int64_t version, const std::string& model_version) {
  const Use use(*this);
  try {
    insert_rows(std::string(WATERMARK_TABLE), {"map_version", "model_version"},
                {Row{{"map_version", version}, {"model_version", model_version}}});
  } catch (const std::exception& error) {
    throw EngineError("recording a map version in " + std::string(WATERMARK_TABLE) +
                      " failed: " + failure(error));
  }
}

// --- logical reads ------------------------------------------------------------------------------

std::vector<Row> ClickHouseEngine::select_rows(const std::string& table, const ReadPlan& plan) {
  const Use use(*this);
  const std::string statement =
      read_sql(table, plan, "clickhouse", [](const Value& value) { return literal(value); });
  try {
    std::vector<Row> rows = query(statement);
    for (Row& row : rows) {
      for (const ReadColumn& column : plan.columns) {
        // The read's SQL gives every moment through toTimeZone(..., 'UTC'), so it arrives as an
        // instant whatever its column's zone; a wall-clock field is that instant's UTC reading.
        Value& value = row.at(column.name);
        if (column.type == "timestamp") {
          if (const auto* instant = std::get_if<TimestampTz>(&value)) {
            value = *Timestamp::from_micros(instant->micros());
          }
        }
      }
      row = read_row(plan.columns, std::move(row));
    }
    return rows;
  } catch (const std::exception& error) {
    throw EngineError("logical scan of " + table + " failed: " + failure(error));
  }
}

std::uint64_t ClickHouseEngine::count_rows(const std::string& table, const ReadPlan& plan) {
  const Use use(*this);
  const std::string statement = read_sql(
      table, plan, "clickhouse", [](const Value& value) { return literal(value); }, true);
  try {
    const std::vector<Row> rows = query(statement);
    if (rows.empty()) throw EngineError("count query returned no result");
    return std::stoull(text_cell(rows.front(), "sde_count"));
  } catch (const std::exception& error) {
    throw EngineError("logical count of " + table + " failed: " + failure(error));
  }
}

SummaryRecord ClickHouseEngine::summarize_rows(const std::string& table, const ReadPlan& plan,
                                               const ReadColumn& column) {
  const Use use(*this);
  const std::string statement = summary_sql(table, plan, column, "clickhouse",
                                            [](const Value& value) { return literal(value); });
  try {
    const std::vector<Row> rows = query(statement);
    if (rows.empty()) throw EngineError("summary query returned no result");
    const Row& row = rows.front();
    const auto cell = [&](std::string_view name) -> std::optional<std::string> {
      const auto found = row.find(name);
      if (found == row.end() || std::holds_alternative<Null>(found->second)) return std::nullopt;
      return std::get<std::string>(found->second);
    };
    return SummaryRecord{cell("sde_count").value_or(""), cell("sde_present").value_or(""),
                         cell("sde_min"), cell("sde_max"), cell("sde_total")};
  } catch (const std::exception& error) {
    throw EngineError("logical summary of " + table + " failed: " + failure(error));
  }
}

// --- migration ----------------------------------------------------------------------------------

std::uint64_t ClickHouseEngine::count(const std::string& table) {
  const Use use(*this);
  try {
    // FINAL, so this counts entities rather than stored rows.
    const std::vector<std::vector<Value>> rows =
        answer("SELECT count() FROM " + quote(table) + " FINAL").rows;
    if (rows.empty()) return 0;
    return static_cast<std::uint64_t>(integer_at(rows.front(), 0));
  } catch (const std::exception& error) {
    throw EngineError("count on " + table + " failed: " + failure(error));
  }
}

std::vector<Row> ClickHouseEngine::key_range(const std::string& table,
                                             const std::vector<std::string>& order,
                                             const std::optional<std::vector<Value>>& after,
                                             const std::optional<std::vector<Value>>& upto,
                                             std::optional<std::size_t> limit) {
  const Use use(*this);
  const std::vector<std::string> columns = key_columns(order, table);
  std::string tuple = "(";
  for (std::size_t i = 0; i < columns.size(); ++i) tuple += (i == 0 ? "" : ", ") + quote(columns[i]);
  tuple += ")";
  const auto bound = [](const std::vector<Value>& values) {
    std::string out = "(";
    for (std::size_t i = 0; i < values.size(); ++i) out += (i == 0 ? "" : ", ") + literal(values[i]);
    return out + ")";
  };
  std::vector<std::string> clauses;
  if (after) {
    same_width(*after, columns, "after");
    clauses.push_back(tuple + " > " + bound(*after));
  }
  if (upto) {
    same_width(*upto, columns, "upto");
    clauses.push_back(tuple + " <= " + bound(*upto));
  }
  std::string where;
  for (std::size_t i = 0; i < clauses.size(); ++i) where += (i == 0 ? " WHERE " : " AND ") + clauses[i];
  const std::string cap = limit ? " LIMIT " + std::to_string(*limit) : "";
  try {
    return query("SELECT * FROM " + quote(table) + " FINAL" + where + " ORDER BY " + tuple + cap);
  } catch (const std::exception& error) {
    throw EngineError("key range select from " + table + " failed: " + failure(error));
  }
}

std::optional<std::vector<Value>> ClickHouseEngine::nth_key(const std::string& table,
                                                            const std::vector<std::string>& order,
                                                            std::int64_t position) {
  const Use use(*this);
  const std::vector<std::string> columns = key_columns(order, table);
  if (position < 1) {
    throw EngineError("position is one-based; " + std::to_string(position) + " is not a row");
  }
  std::string projection;
  for (std::size_t i = 0; i < columns.size(); ++i) projection += (i == 0 ? "" : ", ") + quote(columns[i]);
  std::vector<Row> rows;
  try {
    rows = query("SELECT " + projection + " FROM " + quote(table) + " FINAL ORDER BY (" +
                 projection + ") LIMIT 1 OFFSET " + std::to_string(position - 1));
  } catch (const std::exception& error) {
    throw EngineError("reading row " + std::to_string(position) + " of " + table +
                      " failed: " + failure(error));
  }
  if (rows.empty()) return std::nullopt;
  std::vector<Value> key;
  for (const std::string& column : columns) key.push_back(rows.front().at(column));
  return key;
}

void ClickHouseEngine::copy_in(const std::string& table, const std::vector<Row>& rows) {
  const Use use(*this);
  if (rows.empty()) return;
  // No conflict clause to write: a ReplacingMergeTree collapses a recopied row, which is the same
  // idempotence PostgreSQL gets from ON CONFLICT DO NOTHING.
  const std::vector<std::string> columns = sorted_names(rows.front());
  for (const Row& row : rows) {
    const std::vector<std::string> names = sorted_names(row);
    if (names != columns) {
      throw EngineError("copy_in into " + table + " was given rows with different columns (" +
                        python_repr(columns) + " and " + python_repr(names) +
                        "). A chunk comes from one table, so this is a caller assembling it from "
                        "two.");
    }
  }
  try {
    insert_rows(table, columns, rows);
  } catch (const std::exception& error) {
    write_failed(table, error);
    throw EngineError("copying " + std::to_string(rows.size()) + " rows into " + table +
                      " failed: " + failure(error));
  }
}

std::int64_t ClickHouseEngine::backfill_marker(const std::string& materialization,
                                               const std::string& entity) {
  const Use use(*this);
  const std::string table(BACKFILL_TABLE);
  std::vector<std::vector<Value>> rows;
  try {
    command("CREATE TABLE IF NOT EXISTS " + quote(table) + " (" + quote("materialization") +
            " String, " + quote("entity") + " String, " + quote("rows_copied") + " Int64, " +
            quote("at") + " DateTime64(3, 'UTC') DEFAULT now64(3, 'UTC')) ENGINE = MergeTree ORDER BY (" +
            quote("materialization") + ", " + quote("entity") + ")");
    rows = answer("SELECT max(" + quote("rows_copied") + ") FROM " + quote(table) + " WHERE " +
                  quote("materialization") + " = " + literal(Value(materialization)) + " AND " +
                  quote("entity") + " = " + literal(Value(entity)))
               .rows;
  } catch (const std::exception& error) {
    throw EngineError("reading " + table + " failed: " + failure(error));
  }
  if (rows.empty() || rows.front().empty() || std::holds_alternative<Null>(rows.front()[0])) return 0;
  return integer_at(rows.front(), 0);
}

void ClickHouseEngine::record_backfill_marker(const std::string& materialization,
                                              const std::string& entity, std::int64_t rows) {
  const Use use(*this);
  try {
    insert_rows(std::string(BACKFILL_TABLE), {"materialization", "entity", "rows_copied"},
                {Row{{"materialization", materialization}, {"entity", entity}, {"rows_copied", rows}}});
  } catch (const std::exception& error) {
    throw EngineError("recording backfill progress in " + std::string(BACKFILL_TABLE) +
                      " failed: " + failure(error));
  }
}

}  // namespace sde
