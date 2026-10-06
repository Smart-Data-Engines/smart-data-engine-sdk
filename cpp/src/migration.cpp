#include "sde/migration.hpp"

#include <algorithm>
#include <chrono>
#include <ctime>
#include <set>
#include <string>
#include <tuple>
#include <type_traits>

#include "generation.hpp"
#include "python_compat.hpp"
#include "session_use.hpp"
#include "sde/errors.hpp"
#include "sde/log.hpp"
#include "sde/session.hpp"
#include "sde/write_fence.hpp"
#include "value_internal.hpp"

namespace sde {

namespace {

using detail::python_repr;

}  // namespace

std::vector<std::string> key_columns(const std::vector<std::string>& order,
                                     const std::string& table) {
  if (order.empty()) {
    throw EngineError("a keyset scan of " + table +
                      " needs at least one ordering column. With none, every page is the first "
                      "page and a backfill would copy the same chunk until it was stopped.");
  }
  return order;
}

void same_width(const std::vector<Value>& bound, const std::vector<std::string>& columns,
                std::string_view name) {
  if (bound.size() != columns.size()) {
    throw EngineError(std::string(name) + " has " + std::to_string(bound.size()) +
                      " values and the order has " + std::to_string(columns.size()) +
                      " columns " + python_repr(columns) +
                      ". A row-value comparison of different widths is not a narrower "
                      "comparison, it is a different one.");
  }
}

std::optional<int> dialect_precision(std::string_view neutral, std::string_view dialect) noexcept {
  // PostgreSQL keeps microseconds in both timestamp types, and so does ClickHouse's DateTime64(6),
  // which is the layout's choice for both (format contract section 3.1).
  if ((neutral == "timestamp" || neutral == "timestamptz") &&
      (dialect == "postgres" || dialect == "clickhouse")) {
    return 6;
  }
  return std::nullopt;
}

std::optional<std::string> precision_refusal(std::string_view group, std::string_view entity,
                                             const std::map<std::string, std::string>& columns,
                                             std::string_view source_dialect,
                                             std::string_view target_dialect) {
  for (const auto& [column, neutral] : columns) {
    if (neutral.rfind("decimal(", 0) == 0 ||
        std::find(std::begin(PRECISION_INDEPENDENT), std::end(PRECISION_INDEPENDENT), neutral) !=
            std::end(PRECISION_INDEPENDENT)) {
      continue;
    }
    const std::optional<int> here = dialect_precision(neutral, source_dialect);
    const std::optional<int> there = dialect_precision(neutral, target_dialect);
    const std::string where =
        std::string(group) + "." + std::string(entity) + "." + column;
    if (!here || !there) {
      return where + " has neutral type " + python_repr(neutral) +
             ", and this library does not know whether " + std::string(source_dialect) + " and " +
             std::string(target_dialect) +
             " store it to the same precision. Refused rather than attempted: a type nobody "
             "classified is a type nobody checked, and the failure mode of guessing here is a "
             "value that comes back changed with no error anywhere.";
    }
    if (*there < *here) {
      return where + " is " + python_repr(neutral) + ", which " + std::string(source_dialect) +
             " stores to " + std::to_string(*here) + " sub-second digits and " +
             std::string(target_dialect) + " to " + std::to_string(*there) +
             ". Copying it would truncate every value with more precision than that - silently, "
             "because the insert succeeds and the value comes back changed - and `verify` would "
             "then find every such row mismatched at the end of the copy rather than before it. "
             "Your rows may all happen to be aligned to " +
             std::to_string(*there) +
             " digits, in which case this refusal costs you a migration that would have worked; we "
             "cannot tell without reading your data, and a copy that is faithful only for the "
             "values that happen to be present is not something to build a gate on.";
    }
  }
  return std::nullopt;
}

// --- the plan of a copy --------------------------------------------------------------------------

namespace {

/// One entity, from one materialisation to one fan-out target: the unit both passes work in.
struct Copy {
  std::string entity;
  std::vector<std::string> key;
  Migratable* source = nullptr;
  std::string source_engine;
  std::string source_table;
  Migratable* target = nullptr;
  Engine* target_engine_handle = nullptr;  ///< for the point read that confirms a loss
  std::string target_engine;
  std::string target_id;
  std::string target_table;
  std::optional<std::int64_t> write_epoch;
};

constexpr std::size_t kDifferencesKept = 20;

const Group& group_named(const MigrationView& view, const std::string& group) {
  for (const Group& candidate : view.model.groups()) {
    if (candidate.name == group) return candidate;
  }
  std::vector<std::string> names;
  for (const Group& candidate : view.model.groups()) names.push_back(candidate.name);
  std::sort(names.begin(), names.end());
  throw MigrationRefused(python_repr(group) + " is not a colocation group of this model. It has " +
                         python_repr(names) + ".");
}

Migratable* migratable(const MigrationView& view, const std::string& engine_name,
                       std::string_view role, const std::string& group) {
  Migratable* engine = view.engines.at(engine_name)->capabilities().migration;
  if (engine == nullptr) {
    throw MigrationRefused(
        python_repr(engine_name) + " cannot act as the " + std::string(role) +
        " of a migration of " + python_repr(group) +
        ": its adapter does not offer the row-level operations a copy needs. An engine whose "
        "schema is fixed in its own source has nowhere to keep a progress marker and no table to "
        "scan in key order, so this is a property of the engine rather than a missing feature. "
        "Refused here rather than skipped, because a migration that copies nothing and says "
        "nothing is the worst thing this module could do.");
  }
  return engine;
}

std::map<std::string, std::string> columns_of(const Materialization& material,
                                              const std::string& entity) {
  const auto found = material.layout.columns.find(entity);
  return found == material.layout.columns.end() ? std::map<std::string, std::string>{}
                                                : found->second;
}

/// The target's table has the source's columns, or this is a reshape and not a move.
void shapes_agree(const std::string& group, const std::string& entity,
                  const Materialization& source, const Materialization& target) {
  const auto here = columns_of(source, entity);
  const auto there = columns_of(target, entity);
  const auto described = [&](std::string_view label, const std::map<std::string, std::string>& columns,
                             const Materialization& material) {
    if (columns.empty()) {
      throw MigrationRefused(
          "the " + std::string(label) + " materialisation " + python_repr(material.id) + " of " +
          python_repr(group) + " does not describe the columns of " + entity +
          ", so a copy cannot be checked for shape before it starts. `ensure_schema` needs them "
          "too; a layout with tables and no columns is not one this library can apply.");
    }
  };
  described("source", here, source);
  described("target", there, target);
  std::vector<std::string> only_source;
  std::vector<std::string> only_target;
  for (const auto& [column, unused] : here) {
    if (there.count(column) == 0) only_source.push_back(column);
  }
  for (const auto& [column, unused] : there) {
    if (here.count(column) == 0) only_target.push_back(column);
  }
  if (!only_source.empty() || !only_target.empty()) {
    throw MigrationRefused(
        group + "." + entity + " has different columns in " + python_repr(source.id) + " and " +
        python_repr(target.id) + " (only in the source: " + python_repr(only_source) +
        "; only in the target: " + python_repr(only_target) +
        "). That is a reshape rather than a move: filling a wide table means reading the group's "
        "relations and assembling rows that exist in no single table, and this module copies rows. "
        "A copy would leave the extra columns null and look like it had worked.");
  }
}

/// Every refusal, before a single row moves.
std::vector<Copy> plan(const MigrationView& view, const std::string& group) {
  const Group& members = group_named(view, group);
  const GroupPlacement& placement = view.placement.placement_of(group);
  if (placement.also_write.empty()) {
    throw MigrationRefused(
        python_repr(group) +
        " has no fan-out target in this map, so there is nothing to backfill. A migration reaches "
        "this library as a placement map with 'also_write' - there is no phase name in the "
        "document and no second channel - so a map without that key is one that says this group "
        "is not being migrated.");
  }
  Migratable* source = migratable(view, placement.source.engine, "source", group);
  std::vector<Copy> copies;
  for (const Materialization* copy : placement.also_write_targets()) {
    Migratable* target = migratable(view, copy->engine, "target", group);
    for (const std::string& entity : members.members) {
      const std::vector<std::string>& key = view.model.entity(entity).key;
      if (key.empty()) {
        throw MigrationRefused(group + "." + entity +
                               " has no key, so its rows cannot be scanned in a stable order and a "
                               "chunk boundary would not mean anything.");
      }
      shapes_agree(group, entity, placement.source, *copy);
      // No precision check here, deliberately: opening the session refused a map whose fan-out
      // would truncate, over every group, so a copy reaching this line has been through that door.
      copies.push_back(Copy{entity, key, source, placement.source.engine,
                            placement.source.layout.table_for(entity), target,
                            view.engines.at(copy->engine), copy->engine, copy->id,
                            copy->layout.table_for(entity), placement.write_epoch});
    }
  }
  return copies;
}

Row logical_copy_row(const Copy& copy, Row row) {
  if (copy.write_epoch) row.erase(std::string(EPOCH_COLUMN));
  return row;
}

std::vector<Value> key_of(const Copy& copy, const Row& row) {
  std::vector<Value> key;
  for (const std::string& column : copy.key) {
    const auto found = row.find(column);
    key.push_back(found == row.end() ? Value(Null{}) : found->second);
  }
  return key;
}

/// Turns a row count back into a key, or refuses if the source has lost rows.
std::optional<std::vector<Value>> resume_point(const Copy& copy, std::int64_t marker) {
  if (marker <= 0) return std::nullopt;
  auto position = copy.source->nth_key(copy.source_table, copy.key, marker);
  if (!position) {
    throw MigrationRefused(
        "the marker for " + copy.entity + " in " + copy.target_engine + " says " +
        std::to_string(marker) + " rows have been copied, and " + copy.source_engine + "." +
        copy.source_table +
        " does not have that many. Rows have left the source outside this library, so the marker "
        "describes a table that no longer exists and resuming from it would be guessing. Nothing "
        "has been copied by this call.");
  }
  return position;
}

EntityProgress backfill_one(const Copy& copy, const std::string& group, std::int64_t chunk_rows,
                            std::optional<std::int64_t> stop_after, const LogSink& log) {
  std::int64_t marker = copy.target->backfill_marker(copy.target_id, copy.entity);
  std::optional<std::vector<Value>> after = resume_point(copy, marker);
  std::int64_t rows_this_run = 0;
  std::int64_t chunks = 0;
  bool complete = false;
  while (!stop_after || chunks < *stop_after) {
    const std::vector<Row> rows = copy.source->key_range(
        copy.source_table, copy.key, after, std::nullopt, static_cast<std::size_t>(chunk_rows));
    if (rows.empty()) {
      complete = true;
      break;
    }
    // The chunk first, then the marker: a crash between them costs a recopy, which the target's
    // key semantics absorb; the other order would lose the chunk for good.
    std::vector<Row> payload;
    payload.reserve(rows.size());
    for (const Row& row : rows) {
      if (!copy.write_epoch) {
        payload.push_back(row);
        continue;
      }
      Row stamped = logical_copy_row(copy, row);
      stamped[std::string(EPOCH_COLUMN)] = *copy.write_epoch;
      payload.push_back(std::move(stamped));
    }
    copy.target->copy_in(copy.target_table, payload);
    marker += static_cast<std::int64_t>(rows.size());
    copy.target->record_backfill_marker(copy.target_id, copy.entity, marker);
    after = key_of(copy, rows.back());
    rows_this_run += static_cast<std::int64_t>(rows.size());
    ++chunks;
    Json fields = Json::object();
    fields.set("group", group);
    fields.set("entity", copy.entity);
    fields.set("engine", copy.target_engine);
    fields.set("table", copy.target_table);
    fields.set("chunk", chunks);
    fields.set("rows", static_cast<std::int64_t>(rows.size()));
    fields.set("rows_copied", marker);
    detail::emit(log, "sde.migration.backfill_progress", fields);
    if (static_cast<std::int64_t>(rows.size()) < chunk_rows) {
      // The end of the table, once: rows arriving above it from here on are the fan-out's.
      complete = true;
      break;
    }
  }
  return EntityProgress{copy.entity, copy.target_engine, copy.target_table, marker, rows_this_run,
                        chunks, complete};
}

/// The columns of the source row whose values the target does not match - over the source's
/// columns, because a copy table may carry a column the map does not name.
std::vector<std::string> differing_columns(const Row& source, const Row& target) {
  std::vector<std::string> out;
  for (const auto& [column, value] : source) {
    const auto there = target.find(column);
    if (there == target.end() || !(there->second == value)) out.push_back(column);
  }
  return out;
}

bool value_less(const Value& a, const Value& b) {
  if (a.index() != b.index()) return a.index() < b.index();
  return std::visit(
      [&b](const auto& item) -> bool {
        using T = std::decay_t<decltype(item)>;
        if constexpr (std::is_same_v<T, Null>) {
          return false;
        } else if constexpr (std::is_same_v<T, JsonDocument>) {
          return dump_json(item.document) < dump_json(std::get<T>(b).document);
        } else {
          return item < std::get<T>(b);
        }
      },
      a);
}

/// An order over one table's key values, for indexing a window of the target.
struct KeyLess {
  bool operator()(const std::vector<Value>& left, const std::vector<Value>& right) const {
    return std::lexicographical_compare(left.begin(), left.end(), right.begin(), right.end(),
                                        value_less);
  }
};

std::vector<Difference> missing_in_target(const Copy& copy, const std::vector<Row>& rows,
                                          const std::optional<std::vector<Value>>& low,
                                          const std::vector<Value>& high) {
  // One windowed read of the target, then a point read only for what the window says is missing:
  // the second look absorbs a fan-out that was in flight during the first.
  const std::vector<Row> mirror =
      copy.target->key_range(copy.target_table, copy.key, low, high, std::nullopt);
  std::map<std::vector<Value>, Row, KeyLess> index;
  for (const Row& row : mirror) index.emplace(key_of(copy, row), logical_copy_row(copy, row));
  std::vector<Difference> out;
  for (const Row& physical : rows) {
    const Row row = logical_copy_row(copy, physical);
    const auto there = index.find(key_of(copy, row));
    if (there != index.end() && differing_columns(row, there->second).empty()) continue;
    Row named;
    for (const std::string& column : copy.key) {
      const auto found = row.find(column);
      named[column] = found == row.end() ? Value(Null{}) : found->second;
    }
    const std::optional<Row> again = copy.target_engine_handle->get(copy.target_table, named);
    if (again) {
      std::vector<std::string> differs = differing_columns(row, logical_copy_row(copy, *again));
      if (differs.empty()) continue;
      out.push_back(Difference{copy.entity, copy.target_table, named, std::move(differs)});
      continue;
    }
    out.push_back(Difference{copy.entity, copy.target_table, named, {}});
  }
  return out;
}

/// `datetime.now(UTC).isoformat()`: microseconds only when there are any, and `+00:00`.
std::string now_iso() {
  const auto now = std::chrono::time_point_cast<std::chrono::microseconds>(
      std::chrono::system_clock::now());
  std::string text = detail::format_micros(now.time_since_epoch().count());
  if (text.size() > 7 && text.compare(text.size() - 7, 7, ".000000") == 0) {
    text.erase(text.size() - 7);
  }
  return text + "+00:00";
}

}  // namespace

// --- records ------------------------------------------------------------------------------------

Json EntityProgress::as_record() const {
  Json record = Json::object();
  record.set("entity", entity);
  record.set("engine", engine);
  record.set("table", table);
  record.set("rows_copied", rows_copied);
  record.set("rows_this_run", rows_this_run);
  record.set("chunks", chunks);
  record.set("complete", complete);
  return record;
}

bool BackfillProgress::complete() const noexcept {
  return std::all_of(entities.begin(), entities.end(),
                     [](const EntityProgress& entity) { return entity.complete; });
}

std::int64_t BackfillProgress::rows_this_run() const noexcept {
  std::int64_t total = 0;
  for (const EntityProgress& entity : entities) total += entity.rows_this_run;
  return total;
}

Json BackfillProgress::as_record() const {
  Json record = Json::object();
  record.set("group", group);
  record.set("complete", complete());
  record.set("rows_this_run", rows_this_run());
  Json items = Json::array();
  for (const EntityProgress& entity : entities) items.as_array().push_back(entity.as_record());
  record.set("entities", std::move(items));
  return record;
}

std::string BackfillProgress::for_a_human() const {
  std::string out = "backfill of " + group + ": " + (complete() ? "complete" : "more to do") +
                    ", " + std::to_string(rows_this_run()) + " rows this run";
  for (const EntityProgress& entity : entities) {
    out += "\n  " + entity.entity + " -> " + entity.engine + "." + entity.table + ": " +
           std::to_string(entity.rows_copied) + " rows copied (" +
           std::to_string(entity.rows_this_run) + " this run, " + std::to_string(entity.chunks) +
           " chunks)" + (entity.complete ? "" : ", more to do");
  }
  return out;
}

std::string Difference::for_a_human() const {
  std::string where = absent() ? "absent" : "differs in " + python_repr(columns);
  Json named = Json::object();
  for (const auto& [column, value] : key) {
    named.set(column, std::visit(
                          [](const auto& item) -> Json {
                            using T = std::decay_t<decltype(item)>;
                            if constexpr (std::is_same_v<T, Null>) {
                              return nullptr;
                            } else if constexpr (std::is_same_v<T, bool> ||
                                                 std::is_same_v<T, std::int64_t> ||
                                                 std::is_same_v<T, double> ||
                                                 std::is_same_v<T, std::string>) {
                              return item;
                            } else if constexpr (std::is_same_v<T, Bytes>) {
                              return item.to_hex();
                            } else if constexpr (std::is_same_v<T, JsonDocument>) {
                              return item.document;
                            } else {
                              return item.to_string();
                            }
                          },
                          value));
  }
  return entity + " " + python_repr(named) + " -> " + where;
}

bool VerifyReport::matched() const noexcept {
  return chunks_mismatched == 0 && tail_rows_missing_in_target == 0;
}

Json VerifyReport::as_record() const {
  Json record = Json::object();
  record.set("at", at);
  record.set("chunks_compared", chunks_compared);
  record.set("chunks_mismatched", chunks_mismatched);
  record.set("tail_rows_read", tail_rows_read);
  record.set("tail_rows_missing_in_target", tail_rows_missing_in_target);
  record.set("rows_source", rows_source);
  record.set("rows_target", rows_target);
  if (request) record.set("request", request->as_record());
  return record;
}

std::string VerifyReport::for_a_human() const {
  std::string out = "verify of " + group + " at " + at + ": " +
                    (matched() ? "matched" : "DID NOT MATCH");
  out += "\n  below the marker: " + std::to_string(chunks_compared) + " chunks compared, " +
         std::to_string(chunks_mismatched) + " mismatched" +
         (chunks_mismatched == 0 ? "" : "  <- the backfill did not copy these");
  out += "\n  above the marker: " + std::to_string(tail_rows_read) + " rows read, " +
         std::to_string(tail_rows_missing_in_target) + " missing in the copy" +
         (tail_rows_missing_in_target == 0 ? ""
                                           : "  <- the dual-write fan-out did not reach these");
  out += "\n  rows: " + std::to_string(rows_source) + " in the source, " +
         std::to_string(rows_target) +
         " in the copy (reported, not gated on: two counts of live tables are taken at different "
         "instants)";
  if (!differences.empty()) {
    out += "\n  the rows below are your own data. They are not part of what is reported to Smart "
           "Data Engines:";
    for (const Difference& difference : differences) out += "\n    " + difference.for_a_human();
    if (differences_suppressed > 0) {
      out += "\n    ... and " + std::to_string(differences_suppressed) + " more";
    }
  }
  return out;
}

// --- backfill and verify ------------------------------------------------------------------------

BackfillProgress backfill(Session& session, const std::string& group,
                          const BackfillOptions& options) {
  const Session::Use use(session);
  if (options.chunk_rows < 1) {
    throw MigrationRefused("a chunk of " + std::to_string(options.chunk_rows) +
                           " rows is not a chunk");
  }
  const MigrationView view{session.model(), session.placement(), session.engines(),
                           session.project_id(), session.log_};
  BackfillProgress progress{group, {}};
  for (const Copy& copy : plan(view, group)) {
    progress.entities.push_back(
        backfill_one(copy, group, options.chunk_rows, options.stop_after, view.log));
  }
  return progress;
}

VerifyReport verify(const MigrationView& view, const std::string& group,
                    const VerifyOptions& options) {
  if (options.request) {
    options.request->check_session(view.placement, view.project_id, group);
    if (view.model.version() != options.request->model_version()) {
      throw MigrationRefused("verification request names another session model");
    }
    if (options.at) options.request->check_time(*options.at);
  }
  if (options.chunk_rows < 1) {
    throw MigrationRefused("a chunk of " + std::to_string(options.chunk_rows) +
                           " rows is not a chunk");
  }
  VerifyReport report;
  report.group = group;
  for (const Copy& copy : plan(view, group)) {
    const std::int64_t marker = copy.target->backfill_marker(copy.target_id, copy.entity);
    report.rows_source += static_cast<std::int64_t>(copy.source->count(copy.source_table));
    report.rows_target += static_cast<std::int64_t>(copy.target->count(copy.target_table));
    std::optional<std::vector<Value>> after;
    std::int64_t seen = 0;
    while (true) {
      const bool below = seen < marker;
      const std::int64_t want = below ? std::min(options.chunk_rows, marker - seen) : options.chunk_rows;
      const std::vector<Row> rows = copy.source->key_range(copy.source_table, copy.key, after,
                                                           std::nullopt,
                                                           static_cast<std::size_t>(want));
      if (rows.empty()) break;
      const std::vector<Value> high = key_of(copy, rows.back());
      const std::vector<Difference> missing = missing_in_target(copy, rows, after, high);
      if (below) {
        ++report.chunks_compared;
        if (!missing.empty()) ++report.chunks_mismatched;
      } else {
        report.tail_rows_read += static_cast<std::int64_t>(rows.size());
        report.tail_rows_missing_in_target += static_cast<std::int64_t>(missing.size());
      }
      for (const Difference& difference : missing) {
        if (report.differences.size() < kDifferencesKept) {
          report.differences.push_back(difference);
        } else {
          ++report.differences_suppressed;
        }
      }
      after = high;
      seen += static_cast<std::int64_t>(rows.size());
    }
  }
  report.at = options.at ? *options.at : now_iso();
  if (options.request) options.request->check_time(report.at);
  report.request = options.request;
  return report;
}

VerifyReport verify(Session& session, const std::string& group, const VerifyOptions& options) {
  const Session::Use use(session);
  const MigrationView view{session.model(), session.placement(), session.engines(),
                           session.project_id(), session.log_};
  return verify(view, group, options);
}

// --- an operator's inspection, and the comparison under barriers ----------------------------------

InspectionContext::InspectionContext(const Model& model, const PlacementMap& placement,
                                     std::map<std::string, Engine*> engines,
                                     std::string project_id)
    : model_(model), placement_(placement), engines_(std::move(engines)),
      project_id_(std::move(project_id)) {
  if (placement.contract() < 4) {
    throw MigrationRefused("operator inspection requires a generation-bearing placement map");
  }
  (void)detail::check_map_project(placement, project_id_);
  if (model.version() != placement.model_version()) {
    throw MigrationRefused("operator inspection needs the model named by its map");
  }
  // A group without a write generation (contract 6) is out of every operator's reach.
  std::set<std::string> missing;
  for (const auto& [name, spot] : placement.groups()) {
    if (!spot.write_epoch) continue;
    for (const Materialization* material : spot.all()) {
      if (engines_.count(material->engine) == 0) missing.insert(material->engine);
    }
  }
  if (!missing.empty()) {
    throw MigrationRefused("operator inspection is missing engines " +
                           python_repr(std::vector<std::string>(missing.begin(), missing.end())));
  }
}

MigrationView InspectionContext::view() const {
  return MigrationView{model_, placement_, engines_, project_id_, {}};
}

Json FrozenTable::as_record() const {
  Json record = Json::object();
  record.set("engine", engine);
  record.set("materialization", materialization);
  record.set("table", table);
  record.set("identity", identity);
  record.set("project_id", project_id);
  record.set("epoch", epoch);
  record.set("hold_id", hold_id);
  return record;
}

bool FrozenVerifyReport::matched() const noexcept {
  return comparison.matched() && comparison.rows_source == comparison.rows_target;
}

Json FrozenVerifyReport::as_record() const {
  Json record = Json::object();
  record.set("protocol", 1);
  record.set("comparison", comparison.as_record());
  Json items = Json::array();
  for (const FrozenTable& barrier : barriers) items.as_array().push_back(barrier.as_record());
  record.set("barriers", std::move(items));
  record.set("elapsed_ms", elapsed_ms);
  record.set("matched", matched());
  return record;
}

namespace {

void still_frozen(const FenceState& state, const FrozenTable& wanted) {
  if (state.identity != wanted.identity || state.project_id != wanted.project_id) {
    throw MigrationRefused("a frozen comparison table changed identity or project");
  }
  if (!state.complete() || state.epoch() != wanted.epoch ||
      std::find(state.holds.begin(), state.holds.end(), wanted.hold_id) == state.holds.end()) {
    throw MigrationRefused("a frozen comparison lost its named barrier or write generation");
  }
}

bool lower_hex(std::string_view text, std::size_t width) {
  return text.size() == width && std::all_of(text.begin(), text.end(), [](char c) {
           return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f');
         });
}

}  // namespace

FrozenVerifyReport verify_frozen(const InspectionContext& context, const std::string& group,
                                 const VerificationRequest& request, const std::string& hold_id,
                                 const std::map<std::string, std::int64_t>& epochs,
                                 const FrozenOptions& options) {
  if (!lower_hex(hold_id, 32)) {
    throw MigrationRefused("a frozen comparison hold id must be 32 lowercase hexadecimal digits");
  }
  if (options.chunk_rows < 1) {
    throw MigrationRefused("a frozen comparison needs a positive chunk size");
  }
  request.check_session(context.placement(), context.project_id(), group);
  if (options.at) request.check_time(*options.at);
  const GroupPlacement& spot = context.placement().placement_of(group);
  std::vector<const Materialization*> materials{&spot.source};
  for (const Materialization* copy : spot.also_write_targets()) materials.push_back(copy);
  std::set<std::string> ids;
  for (const Materialization* material : materials) ids.insert(material->id);
  std::set<std::string> named;
  for (const auto& [id, unused] : epochs) named.insert(id);
  if (spot.also_write.empty() || named != ids) {
    throw MigrationRefused("frozen comparison epochs must name exactly the source and copy ids");
  }
  std::map<std::string, std::int64_t> checked;
  for (const auto& [id, epoch] : epochs) checked[id] = check_epoch(epoch);

  std::sort(materials.begin(), materials.end(), [](const Materialization* a, const Materialization* b) {
    return std::tie(a->engine, a->id) < std::tie(b->engine, b->id);
  });
  std::vector<std::pair<WriteFence, FrozenTable>> planned;
  for (const Materialization* material : materials) {
    const Capabilities offered = context.engines().at(material->engine)->capabilities();
    if (offered.fences == nullptr || offered.schema == nullptr) {
      throw MigrationRefused("frozen comparison requires native write fences and schema checks");
    }
    Keys keys;
    for (const auto& [entity, unused] : material->layout.tables) {
      keys[entity] = context.model().entity(entity).key;
    }
    (void)offered.schema->validate_schema(material->layout, keys);
    std::vector<std::string> tables;
    for (const auto& [entity, table] : material->layout.tables) tables.push_back(table);
    std::sort(tables.begin(), tables.end());
    for (const std::string& table : tables) {
      WriteFence fence = offered.fences->write_fence(table, context.project_id());
      const FenceState state = fence.state();
      const std::int64_t epoch = checked.at(material->id);
      if (std::find(state.retired.begin(), state.retired.end(), hold_id) != state.retired.end()) {
        throw MigrationRefused("a frozen comparison cannot reuse a retired barrier id");
      }
      if (!state.complete() || state.epoch() != epoch) {
        throw MigrationRefused("a frozen comparison table is not at the expected write generation");
      }
      planned.emplace_back(fence, FrozenTable{material->engine, material->id, table,
                                              state.identity, context.project_id(), epoch,
                                              hold_id});
    }
  }
  const auto started = std::chrono::steady_clock::now();
  for (auto& [fence, wanted] : planned) {
    // A constraint's presence does not drain an old insert: always drain, also when resuming.
    still_frozen(fence.freeze(hold_id), wanted);
  }
  VerifyOptions compare;
  compare.chunk_rows = options.chunk_rows;
  compare.request = request;
  compare.at = options.at;
  VerifyReport comparison = verify(context.view(), group, compare);
  for (auto& [fence, wanted] : planned) still_frozen(fence.state(), wanted);
  FrozenVerifyReport report;
  report.comparison = std::move(comparison);
  for (const auto& [fence, wanted] : planned) report.barriers.push_back(wanted);
  report.elapsed_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
                          std::chrono::steady_clock::now() - started)
                          .count();
  return report;
}

}  // namespace sde
