#include "engines/clickhouse/fences.hpp"

#include <algorithm>
#include <map>
#include <string>
#include <variant>
#include <vector>

#include "engines/clickhouse/http.hpp"
#include "engines/clickhouse/values.hpp"
#include "sde/clickhouse.hpp"
#include "sde/errors.hpp"
#include "sde/placement.hpp"
#include "sde/schema.hpp"

namespace sde::detail::clickhouse {

namespace {

std::string quote(std::string_view identifier) {
  return quote_identifier("clickhouse", identifier);
}

/// A cell that must be text; anything else is an answer this code did not ask for.
std::string text(const std::vector<Value>& row, std::size_t index) {
  if (index >= row.size() || !std::holds_alternative<std::string>(row[index])) {
    throw EngineError("write-fence metadata query returned an unexpected answer");
  }
  return std::get<std::string>(row[index]);
}

/// What an exchange's failure says, in the reference's words.
std::string failure(const std::exception& error) {
  if (dynamic_cast<const TransportFailure*>(&error) != nullptr) return std::string(kNotReplayed);
  return error.what();
}

/// `_held`: the barrier is the project's, and holds this request.
void held(const FenceMetadata& metadata, const std::string& project_id, const std::string& hold) {
  const FenceState state = fence_state(metadata);
  if (state.project_id != project_id ||
      std::find(state.holds.begin(), state.holds.end(), hold) == state.holds.end()) {
    throw MigrationRefused("the requested project's write barrier is not installed");
  }
}

bool space(char c) noexcept {
  return c == ' ' || c == '\t' || c == '\n' || c == '\r' || c == '\f' || c == '\v';
}

}  // namespace

std::map<std::string, std::string> fence_constraints(std::string_view statement) {
  // ^\s*CONSTRAINT\s+[`"]?(__sde_f_[A-Za-z0-9_]+)[`"]?\s+CHECK\s+([^\n]+), line by line.
  std::map<std::string, std::string> out;
  std::size_t start = 0;
  while (start <= statement.size()) {
    const std::size_t end = statement.find('\n', start);
    std::string_view line = statement.substr(start, end == std::string_view::npos ? std::string_view::npos : end - start);
    start = end == std::string_view::npos ? statement.size() + 1 : end + 1;
    std::size_t at = 0;
    while (at < line.size() && space(line[at])) ++at;
    if (line.substr(at, 10) != "CONSTRAINT") continue;
    at += 10;
    const std::size_t gap = at;
    while (at < line.size() && space(line[at])) ++at;
    if (at == gap) continue;
    if (at < line.size() && (line[at] == '`' || line[at] == '"')) ++at;
    if (line.substr(at, 8) != "__sde_f_") continue;
    const std::size_t name_start = at;
    at += 8;
    const auto word = [](char c) {
      return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') || c == '_';
    };
    if (at >= line.size() || !word(line[at])) continue;
    while (at < line.size() && word(line[at])) ++at;
    const std::string name(line.substr(name_start, at - name_start));
    if (at < line.size() && (line[at] == '`' || line[at] == '"')) ++at;
    const std::size_t before_check = at;
    while (at < line.size() && space(line[at])) ++at;
    if (at == before_check || line.substr(at, 5) != "CHECK") continue;
    at += 5;
    const std::size_t after_check = at;
    while (at < line.size() && space(line[at])) ++at;
    if (at == after_check || at >= line.size()) continue;
    std::string_view expression = line.substr(at);
    while (!expression.empty() && space(expression.back())) expression.remove_suffix(1);
    if (!expression.empty() && expression.back() == ',') expression.remove_suffix(1);
    out[name] = std::string(expression);
  }
  return out;
}

std::vector<std::vector<Value>> Fences::query(const std::string& sql) {
  try {
    return engine_.answer(sql).rows;
  } catch (const std::exception& error) {
    throw EngineError("write-fence metadata query failed: " + failure(error));
  }
}

void Fences::command(const std::string& sql) {
  try {
    engine_.command(sql);
  } catch (const std::exception& error) {
    throw EngineError(
        "write-fence DDL failed; the table may remain closed or detached; resume the same "
        "operation: " +
        failure(error));
  }
}

std::vector<std::vector<Value>> Fences::table_rows(const std::string& table) {
  return query(
      "SELECT toString(uuid), engine, formatQuery(create_table_query) FROM system.tables "
      "WHERE database=currentDatabase() AND name=" +
      literal(table));
}

FenceMetadata Fences::metadata(const std::string& table) {
  const std::vector<std::vector<Value>> database =
      query("SELECT engine FROM system.databases WHERE name=currentDatabase()");
  if (database.size() != 1 || text(database[0], 0) != "Atomic") {
    throw MigrationRefused("write fences require a local Atomic ClickHouse database");
  }
  const std::vector<std::vector<Value>> rows = table_rows(table);
  if (rows.size() != 1) {
    throw EngineError("write-fence table does not exist or is detached; resume the same operation");
  }
  const std::string kind = text(rows[0], 1);
  if (kind != "MergeTree" && kind != "ReplacingMergeTree") {
    throw MigrationRefused("write fences support local MergeTree and ReplacingMergeTree tables");
  }
  const std::vector<std::vector<Value>> columns = query(
      "SELECT type, default_kind, default_expression FROM system.columns "
      "WHERE database=currentDatabase() AND table=" +
      literal(table) + " AND name=" + literal(std::string(EPOCH_COLUMN)));
  const bool valid = columns.size() == 1 && text(columns[0], 0) == "Int64" &&
                     text(columns[0], 1) == "DEFAULT" && text(columns[0], 2) == "0";
  FenceMetadata out;
  out.identity = text(rows[0], 0);
  out.column = valid ? ColumnState::valid : !columns.empty() ? ColumnState::conflict : ColumnState::absent;
  out.constraints = fence_constraints(text(rows[0], 2));
  return out;
}

void Fences::add_column(const std::string& table) {
  command("ALTER TABLE " + quote(table) + " ADD COLUMN IF NOT EXISTS " + quote(EPOCH_COLUMN) +
          " Int64 DEFAULT 0");
}

void Fences::add_constraint(const std::string& table, const std::string& name,
                            const std::string& expression) {
  command("ALTER TABLE " + quote(table) + " ADD CONSTRAINT IF NOT EXISTS " + quote(name) +
          " CHECK " + expression);
}

void Fences::drop_constraint(const std::string& table, const std::string& name) {
  command("ALTER TABLE " + quote(table) + " DROP CONSTRAINT IF EXISTS " + quote(name));
}

void Fences::drain_log() {
  command("CREATE TABLE IF NOT EXISTS " + quote(DRAIN_TABLE) +
          " (table_name String, table_uuid UUID, project_id FixedString(32), hold String) "
          "ENGINE=MergeTree ORDER BY (table_uuid, project_id, hold)");
  const std::vector<std::vector<Value>> columns = query(
      "SELECT name,type FROM system.columns WHERE database=currentDatabase() AND table=" +
      literal(std::string(DRAIN_TABLE)) + " ORDER BY name");
  const std::vector<std::pair<std::string, std::string>> expected = {
      {"hold", "String"},
      {"project_id", "FixedString(32)"},
      {"table_name", "String"},
      {"table_uuid", "UUID"}};
  bool same = columns.size() == expected.size();
  for (std::size_t i = 0; same && i < columns.size(); ++i) {
    same = text(columns[i], 0) == expected[i].first && text(columns[i], 1) == expected[i].second;
  }
  if (!same) throw MigrationRefused("the reserved write-fence drain log has an incompatible schema");
  const std::vector<std::vector<Value>> kinds =
      query("SELECT engine FROM system.tables WHERE database=currentDatabase() AND name=" +
            literal(std::string(DRAIN_TABLE)));
  if (kinds.size() != 1 || text(kinds[0], 0) != "MergeTree") {
    throw MigrationRefused("the reserved write-fence drain log has an incompatible engine");
  }
}

void Fences::drain(const std::string& table, const std::string& project_id,
                   const std::string& hold) {
  const FenceMetadata before = metadata(table);
  held(before, project_id, hold);
  drain_log();
  // The exact Atomic UUID, recorded before DETACH: a failed answer can leave the table detached
  // for good, and a resumption must not attach an unrelated table because its name fits.
  try {
    engine_.insert_rows(std::string(DRAIN_TABLE), {"table_name", "table_uuid", "project_id", "hold"},
                        {Row{{"table_name", table},
                             {"table_uuid", *Uuid::parse(before.identity)},
                             {"project_id", project_id},
                             {"hold", hold}}},
                        "&async_insert=0&wait_for_async_insert=1");
  } catch (const std::exception& error) {
    throw EngineError("write-fence drain intent was not confirmed: " + failure(error));
  }
  command("DETACH TABLE " + quote(table) + " PERMANENTLY SYNC");
  command("ATTACH TABLE " + quote(table));
  const FenceMetadata after = metadata(table);
  if (after.identity != before.identity) {
    throw EngineError("the write-fence table identity changed while draining it");
  }
  held(after, project_id, hold);
}

void Fences::restore(const std::string& table, const std::string& project_id,
                     const std::string& hold) {
  if (!table_rows(table).empty()) {
    held(metadata(table), project_id, hold);
    return;
  }
  // Reading a missing log fails closed: this path creates none, and guesses no ownership from a
  // detached table's name or from the request alone.
  const std::vector<std::vector<Value>> records = query(
      "SELECT DISTINCT toString(table_uuid) FROM " + quote(DRAIN_TABLE) +
      " WHERE table_name=" + literal(table) + " AND project_id=" + literal(project_id) +
      " AND hold=" + literal(hold));
  const std::vector<std::vector<Value>> detached = query(
      "SELECT toString(uuid) FROM system.detached_tables "
      "WHERE database=currentDatabase() AND table=" +
      literal(table) + " AND is_permanently=1");
  if (records.size() != 1 || detached.size() != 1 ||
      text(detached[0], 0) != text(records[0], 0)) {
    throw MigrationRefused("no matching durable intent for this detached write-fence table");
  }
  command("ATTACH TABLE " + quote(table));
  const FenceMetadata restored = metadata(table);
  if (restored.identity != text(records[0], 0)) {
    throw EngineError("the restored write-fence table has another identity");
  }
  held(restored, project_id, hold);
}

}  // namespace sde::detail::clickhouse
