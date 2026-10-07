#include "engines/postgres/fences.hpp"

#include <algorithm>
#include <optional>
#include <string>
#include <vector>

#include "engines/postgres/connection.hpp"
#include "sde/errors.hpp"
#include "sde/placement.hpp"
#include "sde/postgres.hpp"
#include "sde/schema.hpp"

namespace sde::detail::postgres {

namespace {

std::string quote(std::string_view identifier) { return quote_identifier("postgres", identifier); }

using Parameters = std::vector<std::optional<std::string>>;

/// One fence statement, its failure named as a state to inspect or resume.
Result query(Connection& connection, const std::string& sql, const Parameters& parameters = {}) {
  try {
    return connection.execute(sql, parameters);
  } catch (const ServerError& error) {
    throw EngineError(std::string("write-fence operation failed; inspect or resume its state: ") +
                      error.what());
  }
}

/// `_held`: the barrier is the project's, and holds this request.
void held(const FenceMetadata& metadata, const std::string& project_id, const std::string& hold) {
  const FenceState state = fence_state(metadata);
  if (state.project_id != project_id ||
      std::find(state.holds.begin(), state.holds.end(), hold) == state.holds.end()) {
    throw MigrationRefused("the requested project's write barrier is not installed");
  }
}

}  // namespace

void Fences::idle() const {
  if (engine_.transactions_ > 0 || engine_.cx().transaction_status() != PQTRANS_IDLE) {
    throw MigrationRefused("write-fence DDL cannot run inside an application transaction");
  }
}

FenceMetadata Fences::metadata(const std::string& table) {
  Connection& connection = engine_.cx();
  const std::string quoted = quote(table);
  const Result relation = query(
      connection,
      "SELECT c.oid::text, c.relkind, EXISTS (SELECT 1 FROM pg_inherits i "
      "WHERE i.inhrelid=c.oid OR i.inhparent=c.oid) FROM pg_class c "
      "WHERE c.oid=to_regclass($1)",
      {quoted});
  if (relation.rows() != 1) throw EngineError("write-fence table does not exist");
  if (relation.text(0, 1) != "r" || relation.text(0, 2) == "t") {
    throw MigrationRefused("write fences support ordinary PostgreSQL tables without inheritance");
  }
  FenceMetadata out;
  out.identity = std::string(relation.text(0, 0));
  const Result column = query(
      connection,
      "SELECT a.atttypid='bigint'::regtype, a.attnotnull, a.attgenerated, "
      "pg_get_expr(d.adbin,d.adrelid) FROM pg_attribute a "
      "LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum "
      "WHERE a.attrelid=to_regclass($1) AND a.attname=$2 AND NOT a.attisdropped",
      {quoted, std::string(EPOCH_COLUMN)});
  if (column.rows() == 0) {
    out.column = ColumnState::absent;
  } else {
    const bool valid = column.text(0, 0) == "t" && column.text(0, 1) == "t" &&
                       column.text(0, 2).empty() && !column.is_null(0, 3) &&
                       (column.text(0, 3) == "0" || column.text(0, 3) == "'0'::bigint");
    out.column = valid ? ColumnState::valid : ColumnState::conflict;
  }
  const Result constraints = query(connection,
                                   "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                                   "WHERE conrelid=to_regclass($1) AND contype='c'",
                                   {quoted});
  for (int row = 0; row < constraints.rows(); ++row) {
    out.constraints[std::string(constraints.text(row, 0))] = std::string(constraints.text(row, 1));
  }
  return out;
}

void Fences::add_column(const std::string& table) {
  idle();
  (void)query(engine_.cx(), "ALTER TABLE " + quote(table) + " ADD COLUMN IF NOT EXISTS " +
                                quote(EPOCH_COLUMN) + " bigint NOT NULL DEFAULT 0");
}

void Fences::add_constraint(const std::string& table, const std::string& name,
                            const std::string& expression) {
  idle();
  if (metadata(table).constraints.contains(name)) return;
  const std::string check = expression == "1" ? "true" : expression == "0" ? "false" : expression;
  (void)query(engine_.cx(), "ALTER TABLE " + quote(table) + " ADD CONSTRAINT " + quote(name) +
                                " CHECK (" + check + ") NOT VALID");
}

void Fences::drop_constraint(const std::string& table, const std::string& name) {
  idle();
  (void)query(engine_.cx(),
              "ALTER TABLE " + quote(table) + " DROP CONSTRAINT IF EXISTS " + quote(name));
}

void Fences::drain(const std::string& table, const std::string& project_id,
                   const std::string& hold) {
  idle();
  // Even when the hold already existed, returning from this transaction proves that every writer
  // which held a conflicting table lock has completed. A retry repeats the proof.
  Connection& connection = engine_.cx();
  (void)query(connection, "BEGIN");
  try {
    (void)query(connection, "LOCK TABLE " + quote(table) + " IN SHARE ROW EXCLUSIVE MODE");
    held(metadata(table), project_id, hold);
    (void)query(connection, "COMMIT");
  } catch (...) {
    try {
      (void)connection.execute("ROLLBACK");
    } catch (const ServerError&) {
      // The connection reports itself; the first failure is the one to see.
    }
    throw;
  }
}

void Fences::restore(const std::string& table, const std::string& project_id,
                     const std::string& hold) {
  held(metadata(table), project_id, hold);
}

}  // namespace sde::detail::postgres
