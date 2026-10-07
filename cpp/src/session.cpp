#include "sde/session.hpp"

#include <algorithm>
#include <chrono>
#include <iterator>
#include <string>

#include "generation.hpp"
#include "session_use.hpp"
#include "python_compat.hpp"
#include "sde/errors.hpp"
#include "sde/layout.hpp"
#include "sde/migration.hpp"
#include "sde/routing.hpp"

namespace sde {

namespace {

using detail::python_repr;

constexpr std::size_t kMaxBatchRows = 1000;
constexpr std::size_t kMaxBatchValues = 60'000;

std::int64_t steady_ns() {
  return std::chrono::duration_cast<std::chrono::nanoseconds>(
             std::chrono::steady_clock::now().time_since_epoch())
      .count();
}

std::map<std::string, std::string> as_map(const NeutralColumns& columns) {
  return std::map<std::string, std::string>(columns.begin(), columns.end());
}

std::vector<std::string> names_of(const Row& row) {
  std::vector<std::string> names;
  for (const auto& [name, unused] : row) names.push_back(name);
  return names;
}

/// The reference names an exception by its class in a divergence line, never by its message: the
/// message of an engine's error can carry a value of the row.
std::string error_class(const std::exception& error) {
  if (dynamic_cast<const ResourceBusy*>(&error) != nullptr) return "ResourceBusy";
  if (dynamic_cast<const ResourceClosed*>(&error) != nullptr) return "ResourceClosed";
  if (dynamic_cast<const EngineError*>(&error) != nullptr) return "EngineError";
  if (dynamic_cast<const BulkWriteRefused*>(&error) != nullptr) return "BulkWriteRefused";
  if (dynamic_cast<const QueryRefused*>(&error) != nullptr) return "QueryRefused";
  if (dynamic_cast<const ModelPlanningError*>(&error) != nullptr) return "ModelPlanningError";
  if (dynamic_cast<const MigrationRefused*>(&error) != nullptr) return "MigrationRefused";
  if (dynamic_cast<const SdeError*>(&error) != nullptr) return "SdeError";
  return "Exception";
}

/// The batch's field names, checked whole before any engine is called. Values are never in these
/// messages.
std::vector<std::string> batch_columns(const std::vector<Row>& rows, std::size_t extra_columns) {
  if (rows.size() > kMaxBatchRows) {
    throw BulkWriteRefused("a batch may contain at most " + std::to_string(kMaxBatchRows) + " rows");
  }
  std::vector<std::string> columns;
  for (std::size_t i = 0; i < rows.size(); ++i) {
    if (rows[i].empty()) {
      throw BulkWriteRefused("each batch row must be a nonempty mapping with string fields");
    }
    std::vector<std::string> here = names_of(rows[i]);
    if (i == 0) {
      columns = std::move(here);
      if (rows.size() * (columns.size() + extra_columns) > kMaxBatchValues) {
        throw BulkWriteRefused("a batch may contain at most " + std::to_string(kMaxBatchValues) +
                               " values, including generation");
      }
    } else if (here != columns) {
      throw BulkWriteRefused("all batch rows must have the same fields");
    }
  }
  return columns;
}

/// The first required field a row gives null, in code point order.
const std::string* null_required(const Row& values, const std::vector<std::string>& required) {
  for (const std::string& name : required) {
    const auto found = values.find(name);
    if (found != values.end() && is_null(found->second)) return &name;
  }
  return nullptr;
}

/// What a read filtered on, for telemetry: field names only, never a value.
Filter predicates(const ReadPlan& plan) {
  std::set<std::string> equal;
  std::set<std::string> bounded;
  for (const ReadFilter& filter : plan.filters) {
    (filter.operation == ReadOperation::eq ? equal : bounded).insert(filter.column.name);
  }
  Filter out;
  out.equal.assign(equal.begin(), equal.end());
  if (!bounded.empty()) out.range = *bounded.begin();
  return out;
}

/// `refused` when the catalogue denied the login - PostgreSQL's SQLSTATE 42501, ClickHouse's access
/// error 497, which a login without the `system.parts` grant receives - and `failed` otherwise.
std::string storage_refusal(const std::exception& error) {
  const std::string_view text = error.what();
  return text.find("42501") != std::string_view::npos ||
                 text.find("Code: 497") != std::string_view::npos
             ? "refused"
             : "failed";
}

}  // namespace

Session::Session(const Model& model, const PlacementMap& placement,
                 std::map<std::string, Engine*> engines, SessionOptions options)
    : model_(model),
      placement_(placement),
      engines_(std::move(engines)),
      recorder_(options.recorder),
      names_(options.names),
      project_id_(std::move(options.project_id)),
      log_(std::move(options.log)),
      groups_(model.groups()) {
  detail::check_project_id(project_id_);

  // The one place the client's names and the hashed ones meet: everything downstream speaks
  // digests, and the application keeps saying its own names.
  if (names_ != nullptr) {
    std::map<std::string, std::string> original_entities;
    for (const auto& [original, hashed] : names_->entities) original_entities[hashed] = original;
    for (const auto& [entity, declared_fields] : names_->fields) {
      std::map<std::string, std::string> mapping = declared_fields;
      if (const auto relations = names_->relations.find(entity); relations != names_->relations.end()) {
        for (const auto& [original_relation, hashed_relation] : relations->second) {
          const std::string& source = names_->entity(entity);
          const auto relation = std::find_if(
              model.relations().begin(), model.relations().end(), [&](const Relation& r) {
                return r.from == source && r.name == hashed_relation;
              });
          const Entity& target = model.entity(relation->to);
          const auto& target_fields = names_->fields.at(original_entities.at(target.name));
          for (const auto& [original_key, hashed_key] : target_fields) {
            if (std::find(target.key.begin(), target.key.end(), hashed_key) != target.key.end()) {
              mapping[original_relation + "_" + original_key] = hashed_relation + "_" + hashed_key;
            }
          }
        }
      }
      auto& reverse = reverse_fields_[names_->entity(entity)];
      for (const auto& [original, hashed] : mapping) reverse[hashed] = original;
      input_fields_[entity] = std::move(mapping);
    }
    for (const auto& [original, unused] : names_->entities) declared_.push_back(original);
  }

  for (const OperationShape& shape : model.shapes()) {
    shapes_[{shape.entity, shape.kind, shape.fields}] = &shape;
  }
  // What a row of each entity must and may carry, once per session. The write-generation column is
  // left to `stamp_values`, which refuses it with its own reason: it is reserved, not undeclared.
  for (const Group& group : groups_) {
    const auto columns = group_columns(model, group);
    for (const std::string& member : group.members) {
      const Entity& spec = model.entity(member);
      Admission admission;
      for (const auto& [name, unused] : columns.at(member)) admission.declared.insert(name);
      if (placement.contract() >= 4) admission.declared.insert(std::string(EPOCH_COLUMN));
      for (const Field& field : spec.fields) {
        const bool keyed = std::find(spec.key.begin(), spec.key.end(), field.name) != spec.key.end();
        if (!field.nullable || keyed) admission.required.push_back(field.name);
      }
      std::sort(admission.required.begin(), admission.required.end());
      admission_[member] = std::move(admission);
    }
  }

  std::set<std::string> missing;
  for (const auto& [name, spot] : placement.groups()) {
    for (const Materialization* material : spot.all()) {
      if (engines_.count(material->engine) == 0) missing.insert(material->engine);
    }
  }
  if (!missing.empty()) {
    throw EngineError("the placement map refers to engines that were not supplied: " +
                      python_repr(std::vector<std::string>(missing.begin(), missing.end())) +
                      ". A session cannot route an operation to an engine it has no adapter for, "
                      "and guessing at a connection is not something a library should do.");
  }

  // A copy that silently holds different values from its source is refused before the first write
  // rather than found by `verify` after one. Here rather than in the map loader, because a map names
  // engines and carries no dialect: this is the first door holding the adapters.
  for (const Group& group : groups_) {
    const auto spot = placement.groups().find(group.name);
    if (spot == placement.groups().end() || spot->second.also_write.empty()) continue;
    const auto columns = group_columns(model, group);
    const std::string_view source_dialect = engines_.at(spot->second.source.engine)->dialect();
    for (const Materialization* copy : spot->second.also_write_targets()) {
      for (const auto& [entity, neutral] : columns) {
        if (auto refusal = precision_refusal(group.name, entity, as_map(neutral), source_dialect,
                                             engines_.at(copy->engine)->dialect())) {
          throw MigrationRefused(*refusal + " This map fans writes out to " + copy->engine +
                                 ", so it would happen on every write rather than once during a "
                                 "copy, and nothing would report it.");
        }
      }
    }
  }

  // On the path every start takes - a rolled-back map file is read at process start - and costing
  // nothing for an unsigned map.
  physical_ = detail::validate_generations(model, placement, engines_, project_id_);
  report_physical();
  forward_only_ = enforce_forward_only(placement, engines_, log_);
}

Session::~Session() = default;

std::int64_t Session::now() const { return recorder_ != nullptr ? steady_ns() : 0; }

// --- the hashing boundary -----------------------------------------------------------------------

std::string Session::target_of(std::string_view entity) const {
  if (names_ == nullptr) return std::string(entity);
  return names_->entity(entity);
}

Row Session::fields_in(std::string_view entity, const Row& values) const {
  if (names_ == nullptr) return values;
  const auto mapping = input_fields_.find(std::string(entity));
  Row out;
  for (const auto& [field, value] : values) {
    const auto hashed = mapping == input_fields_.end() ? std::map<std::string, std::string>::const_iterator{}
                                                       : mapping->second.find(field);
    if (mapping == input_fields_.end() || hashed == mapping->second.end()) {
      throw ModelPlanningError(std::string(entity) + " declares no field " + field +
                               ". With hashed identifiers a field the model does not declare "
                               "cannot be translated, so it is refused here rather than sent to "
                               "an engine under a name nothing will recognise.");
    }
    out[hashed->second] = value;
  }
  return out;
}

Row Session::fields_out(std::string_view entity, Row row) const {
  if (names_ == nullptr) return row;
  const auto reverse = reverse_fields_.find(names_->entity(entity));
  if (reverse == reverse_fields_.end()) return row;
  Row out;
  for (auto& [field, value] : row) {
    const auto original = reverse->second.find(field);
    out[original == reverse->second.end() ? field : original->second] = std::move(value);
  }
  return out;
}

std::string Session::client_name(std::string_view entity, const std::string& field) const {
  if (names_ == nullptr) return field;
  const auto reverse = reverse_fields_.find(names_->entity(entity));
  if (reverse == reverse_fields_.end()) return field;
  const auto original = reverse->second.find(field);
  return original == reverse->second.end() ? field : original->second;
}

// --- structure ----------------------------------------------------------------------------------

const Group& Session::group_of(std::string_view entity) const {
  const std::string target = target_of(entity);
  for (const Group& group : groups_) {
    if (group.contains(target)) return group;
  }
  throw ModelPlanningError(python_repr(entity) +
                           " is not in this model. A session routes what the model declares; an "
                           "entity that is not declared has no group, no placement and no table.");
}

const OperationShape& Session::shape(const std::string& entity, std::string_view kind,
                                     const std::vector<std::string>& fields) const {
  const auto found = shapes_.find({entity, std::string(kind), fields});
  if (found == shapes_.end()) {
    throw ModelPlanningError("the model admits no " + std::string(kind) + " on " + entity +
                             " over " + python_repr(fields) +
                             ". Shapes are enumerated from the model, so an operation with no "
                             "shape is one the planner never saw and therefore never routed, which "
                             "makes it a modelling gap rather than a runtime error.");
  }
  return *found->second;
}

std::pair<Engine*, const Materialization*> Session::target(const OperationShape& shape,
                                                           bool fresh) const {
  // Named, not a temporary: `resolve` returns into the map, and GCC cannot tell the context apart.
  const RouteContext context{in_write_transaction_, fresh};
  const Materialization& materialization = resolve(placement_, shape, context);
  return {engines_.at(materialization.engine), &materialization};
}

void Session::admit(std::string_view entity, const std::string& target, const Row& values) const {
  // A field declared without `nullable` and every key field must be present and not null, and a
  // field the entity does not declare is refused by name. The engines would not agree on any of
  // it: measured on 2 October 2026, PostgreSQL stored NULL in a required field and ClickHouse a
  // value nobody wrote. Section 8b; `errors/078` to `082`.
  const Admission& admission = admission_.at(target);
  const auto declared = [&](const std::string& name) { return admission.declared.count(name) != 0; };
  const bool all_declared =
      std::all_of(values.begin(), values.end(), [&](const auto& item) { return declared(item.first); });
  const bool all_present = std::all_of(admission.required.begin(), admission.required.end(),
                                       [&](const std::string& name) { return values.count(name) != 0; });
  if (all_declared && all_present) {
    if (const std::string* name = null_required(values, admission.required)) {
      throw ModelPlanningError(std::string(entity) + "." + client_name(entity, *name) +
                               " is required and this row gives it null");
    }
    return;
  }
  for (const auto& [name, unused] : values) {
    if (!declared(name)) {
      throw ModelPlanningError(std::string(entity) + " declares no field " +
                               client_name(entity, name));
    }
  }
  for (const std::string& name : admission.required) {
    if (values.count(name) == 0) {
      throw ModelPlanningError(std::string(entity) + "." + client_name(entity, name) +
                               " is required and this row leaves it out");
    }
  }
}

// --- schema -------------------------------------------------------------------------------------

void Session::ensure_schema() {
  const Use use(*this);
  if (placement_.contract() >= 4) {
    physical_ = detail::validate_generations(model_, placement_, engines_, project_id_);
    report_physical();
    return;
  }
  std::vector<PhysicalFinding> findings;
  for (const Group& group : groups_) {
    const GroupPlacement& spot = placement_.placement_of(group.name);
    Keys keys;
    for (const std::string& member : group.members) keys[member] = model_.entity(member).key;
    for (const Materialization* material : spot.all()) {
      const std::vector<PhysicalFinding> found =
          engines_.at(material->engine)->ensure_schema(material->layout, keys);
      findings.insert(findings.end(), found.begin(), found.end());
    }
  }
  physical_ = std::move(findings);
  report_physical();
  Json fields = Json::object();
  fields.set("groups", static_cast<std::int64_t>(groups_.size()));
  detail::emit(log_, "sde.schema.applied", fields);
}

void Session::report_physical() const {
  if (physical_.empty()) return;
  std::set<std::string> tables;
  for (const PhysicalFinding& finding : physical_) tables.insert(finding.table);
  Json fields = Json::object();
  fields.set("findings", static_cast<std::int64_t>(physical_.size()));
  Json named = Json::array();
  for (const std::string& table : tables) named.as_array().push_back(table);
  fields.set("tables", std::move(named));
  detail::emit(log_, "sde.schema.physical_mismatch", fields);
}

// --- data ---------------------------------------------------------------------------------------

void Session::save(std::string_view entity, const Row& given) {
  const Use use(*this);
  const std::string target_entity = target_of(entity);
  const Row values = fields_in(entity, given);
  const OperationShape& write = shape(target_entity, "write");
  admit(entity, target_entity, values);
  const auto [engine, materialization] = target(write, false);
  const std::string& table = materialization->layout.table_for(target_entity);
  const std::int64_t started = now();
  bool failed = false;
  try {
    engine->insert(table, detail::stamp_values(placement_, write.group, values));
    fan_out(target_entity, write.group, {values}, false);
  } catch (...) {
    // A failed write is the operation whose latency and errors matter most to a placement.
    failed = true;
    observe(write, started, 1, failed);
    throw;
  }
  // The fan-out is inside the timed region, deliberately: it is what the client pays.
  observe(write, started, 1, failed);
}

void Session::save_many(std::string_view entity, const std::vector<Row>& given) {
  const Use use(*this);
  const std::string target_entity = target_of(entity);
  const OperationShape& write = shape(target_entity, "bulk_write");
  const auto [engine, materialization] = target(write, false);
  const GroupPlacement& spot = placement_.placement_of(write.group);
  const std::vector<std::string> columns =
      batch_columns(given, spot.write_epoch.has_value() ? 1 : 0);
  if (columns.empty()) return;
  std::vector<Row> rows;
  rows.reserve(given.size());
  try {
    for (const Row& row : given) rows.push_back(fields_in(entity, row));
  } catch (const ModelPlanningError& error) {
    throw BulkWriteRefused(error.what());
  }
  const Entity& spec = model_.entity(target_entity);
  const auto grouped = group_columns(model_, group_of(entity));
  std::set<std::string> declared;
  for (const auto& [name, unused] : grouped.at(target_entity)) declared.insert(name);
  std::set<std::string> required(spec.key.begin(), spec.key.end());
  for (const Field& field : spec.fields) {
    if (!field.nullable) required.insert(field.name);
  }
  const std::vector<std::string> fields = names_of(rows.front());
  const bool all_declared = std::all_of(fields.begin(), fields.end(),
                                        [&](const std::string& name) { return declared.count(name) != 0; });
  const bool all_required = std::all_of(required.begin(), required.end(), [&](const std::string& name) {
    return std::find(fields.begin(), fields.end(), name) != fields.end();
  });
  if (!all_declared || !all_required) {
    throw BulkWriteRefused(
        "batch fields must be declared and include the key and all non-nullable fields");
  }
  for (std::size_t index = 0; index < rows.size(); ++index) {
    if (const std::string* name = null_required(rows[index], admission_.at(target_entity).required)) {
      throw BulkWriteRefused("row " + std::to_string(index) + ": " + std::string(entity) + "." +
                             client_name(entity, *name) + " is required and this row gives it null");
    }
  }
  BulkWritable* writer = engine->capabilities().bulk;
  if (writer == nullptr) {
    throw BulkWriteRefused("this adapter does not support bulk writes (insert_many)");
  }
  for (const Materialization* copy : spot.also_write_targets()) {
    if (engines_.at(copy->engine)->capabilities().bulk == nullptr) {
      throw BulkWriteRefused("this adapter does not support bulk writes (insert_many)");
    }
  }
  std::vector<Row> stamped;
  stamped.reserve(rows.size());
  for (const Row& row : rows) stamped.push_back(detail::stamp_values(placement_, write.group, row));
  const std::string& table = materialization->layout.table_for(target_entity);
  const std::int64_t started = now();
  try {
    writer->insert_many(table, stamped);
    fan_out(target_entity, write.group, rows, true);
  } catch (...) {
    observe(write, started, rows.size(), true);
    throw;
  }
  observe(write, started, rows.size(), false);
}

void Session::fan_out(const std::string& entity, const std::string& group,
                      const std::vector<Row>& rows, bool batch) {
  // Additionally, never authoritatively: a failure here does not interrupt the client's operation.
  // Inside a write transaction the fan-out waits for the commit - the copy is another engine,
  // outside the source's transaction, so a row written there now would survive a rollback.
  const GroupPlacement& spot = placement_.placement_of(group);
  for (const Materialization* copy : spot.also_write_targets()) {
    Deferred write{copy->engine, copy->layout.table_for(entity), group, copy->id, steady_ns(), rows,
                   batch};
    if (in_write_transaction_) {
      deferred_.push_back(std::move(write));
      continue;
    }
    replay_one(write);
  }
}

void Session::replay_one(const Deferred& write) {
  bool failed = false;
  try {
    Engine& engine = *engines_.at(write.engine);
    if (write.batch) {
      std::vector<Row> stamped;
      stamped.reserve(write.rows.size());
      for (const Row& row : write.rows) {
        stamped.push_back(detail::stamp_values(placement_, write.group, row));
      }
      BulkWritable* writer = engine.capabilities().bulk;
      if (writer == nullptr) {
        throw BulkWriteRefused("this adapter does not support bulk writes (insert_many)");
      }
      writer->insert_many(write.table, stamped);
    } else {
      engine.insert(write.table, detail::stamp_values(placement_, write.group, write.rows.front()));
    }
  } catch (const std::exception& error) {
    // The only place this library swallows a write failure: the row is in the source, which is the
    // copy that counts, and `verify` is the gate that refuses to switch reads while any remain.
    failed = true;
    Json fields = Json::object();
    fields.set("engine", write.engine);
    fields.set("table", write.table);
    fields.set("error", error_class(error));
    detail::emit(log_, "sde.migration.divergence", fields);
  }
  if (recorder_ != nullptr) {
    recorder_->record_fan_out(write.group, write.materialization, steady_ns() - write.queued_ns,
                              failed);
  }
}

std::optional<Row> Session::get(std::string_view entity, const Row& key, bool fresh) {
  const Use use(*this);
  const std::string target_entity = target_of(entity);
  const Row given = fields_in(entity, key);
  const Entity& spec = model_.entity(target_entity);
  std::vector<std::string> expected = spec.key;
  std::sort(expected.begin(), expected.end());
  if (names_of(given) != expected) {
    std::vector<std::string> wanted;
    for (const std::string& name : expected) wanted.push_back(client_name(entity, name));
    std::sort(wanted.begin(), wanted.end());
    std::vector<std::string> received;
    for (const auto& [name, unused] : given) received.push_back(client_name(entity, name));
    std::sort(received.begin(), received.end());
    throw ModelPlanningError("a point read of " + std::string(entity) + " needs exactly its key " +
                             python_repr(wanted) + ", and was given " + python_repr(received) +
                             ". A partial key is a range read, which is a different shape and may "
                             "well be routed somewhere else.");
  }
  const OperationShape& read = shape(target_entity, "point_read", expected);
  const auto [engine, materialization] = target(read, fresh);
  const std::string& table = materialization->layout.table_for(target_entity);
  const std::int64_t started = now();
  std::optional<Row> row;
  try {
    row = engine->get(table, given);
  } catch (...) {
    observe(read, started, 0, true);
    throw;
  }
  observe(read, started, row ? 1 : 0, false);
  if (!row) return std::nullopt;
  return fields_out(entity, detail::logical_row(placement_, std::move(*row)));
}

std::pair<std::string, ReadPlan> Session::prepare_read(
    std::string_view entity, const std::optional<Row>& where, const std::optional<Range>& bounds,
    const std::optional<std::string>& order_by, bool descending, const std::optional<Row>& after,
    PageLimit limit, bool paginate) const {
  // A name the hashing map does not know is the map's refusal (`DeclarationError`), as in the
  // reference; a name the model does not declare is the query's.
  const std::string target_entity = target_of(entity);
  const Entity* spec = model_.find_entity(target_entity);
  if (spec == nullptr) throw QueryRefused("query refers to an entity this model does not declare");
  const auto translated = [&](const std::optional<Row>& values) -> std::optional<Row> {
    if (!values) return std::nullopt;
    try {
      return fields_in(entity, *values);
    } catch (const ModelPlanningError& error) {
      throw QueryRefused(error.what());
    }
  };
  const auto field = [&](const std::string& name) {
    const std::optional<Row> mapped = translated(Row{{name, Null{}}});
    return mapped->begin()->first;
  };
  std::optional<Range> range;
  if (bounds) range = Range{field(bounds->field), bounds->low, bounds->high};
  std::vector<ReadColumn> columns;
  const auto grouped = group_columns(model_, group_of(entity));
  for (const auto& [name, type] : as_map(grouped.at(target_entity))) {
    columns.push_back(ReadColumn{name, type});
  }
  ReadOptions options;
  options.where = translated(where);
  options.bounds = range;
  if (order_by) options.order_by = field(*order_by);
  options.descending = descending;
  options.after = translated(after);
  options.limit = limit;
  options.paginate = paginate;
  return {target_entity, plan_read(columns, spec->key, options)};
}

namespace {

void read_projection(const Materialization& material, const std::string& entity,
                     const ReadPlan& plan, bool count) {
  std::set<std::string> required;
  for (const ReadFilter& filter : plan.filters) required.insert(filter.column.name);
  if (!count) {
    for (const ReadColumn& column : plan.columns) required.insert(column.name);
  }
  const auto available = material.layout.columns.find(entity);
  for (const std::string& name : required) {
    if (available == material.layout.columns.end() || available->second.count(name) == 0) {
      throw QueryRefused(
          "the routed materialization does not contain every field this query needs; request "
          "fresh=True to read the source or have the placement revised");
    }
  }
}

}  // namespace

ScanPage Session::scan(std::string_view entity, const ScanOptions& options) {
  const Use use(*this);
  const auto [target_entity, plan] =
      prepare_read(entity, options.where, options.bounds, options.order_by, options.descending,
                   options.after, options.limit, true);
  const OperationShape* read = nullptr;
  if (!options.bounds) {
    read = &shape(target_entity, "full_scan");
  } else {
    const auto ranged = std::find_if(plan.filters.begin(), plan.filters.end(), [](const ReadFilter& f) {
      return f.operation == ReadOperation::ge || f.operation == ReadOperation::lt;
    });
    read = &shape(target_entity, "range_read", {ranged->column.name});
  }
  const auto [engine, material] = target(*read, options.fresh);
  read_projection(*material, target_entity, plan, false);
  Queryable* reader = engine->capabilities().query;
  if (reader == nullptr) {
    throw QueryRefused("this adapter does not support logical reads (select_rows)");
  }
  const std::string& table = material->layout.table_for(target_entity);
  const std::int64_t started = now();
  ScanPage page;
  try {
    std::vector<Row> rows = reader->select_rows(table, plan);
    const auto limit = static_cast<std::size_t>(plan.limit);
    if (rows.size() > limit) {
      Row position;
      for (const ReadColumn& column : plan.order) position[column.name] = rows[limit - 1].at(column.name);
      page.next_after = fields_out(entity, std::move(position));
      rows.resize(limit);
    }
    for (Row& row : rows) page.rows.push_back(fields_out(entity, std::move(row)));
  } catch (...) {
    observe(*read, started, 0, true, &plan);
    throw;
  }
  observe(*read, started, page.rows.size(), false, &plan);
  return page;
}

std::uint64_t Session::count(std::string_view entity, const CountOptions& options) {
  const Use use(*this);
  const auto [target_entity, plan] =
      prepare_read(entity, options.where, options.bounds, std::nullopt, false, std::nullopt,
                   PageLimit{}, false);
  const OperationShape& aggregate = shape(target_entity, "aggregate");
  const auto [engine, material] = target(aggregate, options.fresh);
  read_projection(*material, target_entity, plan, true);
  const Capabilities offered = engine->capabilities();
  if (offered.count == nullptr) {
    throw QueryRefused(offered.count_refusal.empty()
                           ? "this adapter does not support counts (count_rows)"
                           : offered.count_refusal);
  }
  const std::string& table = material->layout.table_for(target_entity);
  const std::int64_t started = now();
  std::uint64_t result = 0;
  try {
    result = offered.count->count_rows(table, plan);
  } catch (...) {
    observe(aggregate, started, 0, true, &plan);
    throw;
  }
  observe(aggregate, started, 1, false, &plan);
  return result;
}

NumericSummary Session::summarize(std::string_view entity, std::string_view field,
                                  const SummaryOptions& options) {
  const Use use(*this);
  const auto [target_entity, plan] =
      prepare_read(entity, options.where, options.bounds, std::nullopt, false, std::nullopt,
                   PageLimit{}, false);
  std::string name;
  try {
    name = fields_in(entity, Row{{std::string(field), Null{}}}).begin()->first;
  } catch (const ModelPlanningError& error) {
    throw QueryRefused(error.what());
  }
  const auto column = std::find_if(plan.columns.begin(), plan.columns.end(),
                                   [&](const ReadColumn& c) { return c.name == name; });
  if (column == plan.columns.end()) {
    throw QueryRefused("summary refers to a field this entity does not declare");
  }
  (void)summary_scale(*column, options.mean_scale);
  const OperationShape& aggregate = shape(target_entity, "aggregate");
  const auto [engine, material] = target(aggregate, options.fresh);
  read_projection(*material, target_entity, plan, true);
  const auto available = material->layout.columns.find(target_entity);
  if (available == material->layout.columns.end() || available->second.count(name) == 0) {
    throw QueryRefused("the routed materialization does not contain the summary field");
  }
  const Capabilities offered = engine->capabilities();
  if (offered.summary == nullptr) {
    throw QueryRefused(offered.summary_refusal.empty()
                           ? "this adapter does not support numeric summaries (summarize_rows)"
                           : offered.summary_refusal);
  }
  const std::string& table = material->layout.table_for(target_entity);
  const std::int64_t started = now();
  NumericSummary summary;
  try {
    summary = numeric_summary(offered.summary->summarize_rows(table, plan, *column), *column,
                              options.mean_scale);
  } catch (...) {
    observe(aggregate, started, 0, true, &plan);
    throw;
  }
  observe(aggregate, started, 1, false, &plan);
  return summary;
}

StorageMeasurement Session::measure_storage() {
  const Use use(*this);
  std::map<std::string, std::vector<std::pair<std::string, const Materialization*>>> by_engine;
  for (const auto& [name, spot] : placement_.groups()) {
    by_engine[spot.source.engine].emplace_back(name, &spot.source);
  }
  StorageMeasurement out;
  for (const auto& [engine_name, members] : by_engine) {
    StorageMeasurable* reader = engines_.at(engine_name)->capabilities().storage;
    if (reader == nullptr) {
      for (const auto& [name, unused] : members) out.unavailable[name] = "unsupported";
      continue;
    }
    std::set<std::string> tables;
    for (const auto& [name, source] : members) {
      for (const auto& [entity, table] : source->layout.tables) tables.insert(table);
    }
    std::map<std::string, std::pair<std::int64_t, std::int64_t>> found;
    try {
      found = reader->storage_sizes(std::vector<std::string>(tables.begin(), tables.end()));
    } catch (const ResourceBusy&) {
      throw;  // misuse of an adapter is the caller's to see, not an unknown size
    } catch (const ResourceClosed&) {
      throw;
    } catch (const std::exception& error) {
      for (const auto& [name, unused] : members) out.unavailable[name] = storage_refusal(error);
      continue;
    }
    for (const auto& [name, source] : members) {
      StorageSize size{name, source->id, engine_name, 0, 0};
      bool complete = true;
      for (const auto& [entity, table] : source->layout.tables) {
        const auto measured = found.find(table);
        if (measured == found.end()) {
          complete = false;
          break;
        }
        size.total_bytes += measured->second.first;
        size.secondary_index_bytes += measured->second.second;
      }
      if (!complete) {
        out.unavailable[name] = "missing_table";
        continue;
      }
      if (recorder_ != nullptr) {
        recorder_->record_storage(name, size.total_bytes, size.secondary_index_bytes);
      }
      out.sizes.push_back(std::move(size));
    }
  }
  for (const auto& [name, reason] : out.unavailable) {
    Json fields = Json::object();
    fields.set("engine", placement_.placement_of(name).source.engine);
    fields.set("reason", reason);
    detail::emit(log_, "sde.telemetry.storage_unavailable", fields);
  }
  std::sort(out.sizes.begin(), out.sizes.end(),
            [](const StorageSize& a, const StorageSize& b) { return a.group < b.group; });
  return out;
}

void Session::observe(const OperationShape& shape, std::int64_t started, std::uint64_t rows,
                      bool failed, const ReadPlan* plan) {
  if (recorder_ == nullptr) return;
  const std::int64_t elapsed = steady_ns() - started;
  if (plan == nullptr) {
    recorder_->record(shape, elapsed, rows, failed);
    return;
  }
  const Filter filter = predicates(*plan);
  recorder_->record(shape, elapsed, rows, failed, &filter);
}

// --- transactions -------------------------------------------------------------------------------

void Session::transaction(const std::vector<std::string>& entities,
                          const std::function<void()>& body) {
  const Use use(*this);
  // Named as the client names them, so that an error and the entities passed are in one language.
  std::vector<std::string> names = entities;
  if (names.empty()) {
    if (!declared_.empty()) {
      names = declared_;
    } else {
      for (const Entity& entity : model_.entities()) names.push_back(entity.name);
    }
  }
  std::set<std::string> groups;
  for (const std::string& name : names) groups.insert(group_of(name).name);
  if (groups.size() > 1) {
    std::vector<std::string> sorted = names;
    std::sort(sorted.begin(), sorted.end());
    std::map<std::string, std::vector<std::string>> by_group;
    for (const std::string& name : sorted) by_group[group_of(name).name].push_back(name);
    std::string layout;
    for (const auto& [group, members] : by_group) {
      if (!layout.empty()) layout += "; ";
      layout += group + ": ";
      for (std::size_t i = 0; i < members.size(); ++i) {
        if (i > 0) layout += ", ";
        layout += members[i];
      }
    }
    throw ModelPlanningError(
        "a transaction cannot span colocation groups (" + layout +
        "). One group is one engine and one engine's transaction; there is no distributed "
        "transaction here and there will not be one. If these entities have to commit together, "
        "declare it with `atomic_with` on either side, and the planner will place them in the same "
        "engine - which turns the requirement into a placement constraint instead of a two-phase "
        "commit.");
  }
  const Group& group = group_of(names.front());
  Engine& engine = *engines_.at(placement_.placement_of(group.name).source.engine);
  const bool previous = in_write_transaction_;
  const std::size_t outer = deferred_.size();
  in_write_transaction_ = true;
  bool committed = false;
  const auto settle = [&] {
    in_write_transaction_ = previous;
    std::vector<Deferred> pending(std::make_move_iterator(deferred_.begin() + static_cast<std::ptrdiff_t>(outer)),
                                  std::make_move_iterator(deferred_.end()));
    deferred_.resize(outer);
    if (committed && !previous) {
      // Replayed after the source transaction has committed, and only by the outermost one.
      for (const Deferred& write : pending) replay_one(write);
    } else if (committed) {
      // A nested block returning to a transaction still open has committed nothing yet.
      for (Deferred& write : pending) deferred_.push_back(std::move(write));
    }
    // Rolled back: the rows never existed in the source, so they must never exist in the copy.
  };
  try {
    engine.transaction(body);
    committed = true;
  } catch (...) {
    settle();
    throw;
  }
  settle();
}

}  // namespace sde
