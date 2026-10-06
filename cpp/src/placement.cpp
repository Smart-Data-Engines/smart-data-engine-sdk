#include "sde/placement.hpp"

#include <algorithm>
#include <limits>
#include <set>
#include <utility>

#include "crypto.hpp"
#include "encoding.hpp"
#include "physical_internal.hpp"
#include "python_compat.hpp"
#include "sde/canonical.hpp"
#include "sde/errors.hpp"
#include "sde/layout.hpp"
#include "sde/unicode.hpp"

namespace sde {

namespace {

using detail::python_repr;

/// Table names this library keeps its own bookkeeping in, and what each holds - so the refusal can
/// say what a collision would break rather than only that a name is taken.
constexpr std::pair<std::string_view, std::string_view> kReservedTables[] = {
    {WATERMARK_TABLE,
     "the highest map version applied against an engine, which is what stops an older map from "
     "being loaded over a newer one"},
    {BACKFILL_TABLE,
     "how many rows of each entity a migration has copied into an engine, which is what lets an "
     "interrupted backfill resume instead of starting over"},
};

/// A materialisation as read, before an `{"auto": true}` layout is derived from the model.
struct ReadMaterialization {
  Materialization material;
  bool auto_layout = false;
};

struct ReadGroup {
  GroupPlacement placement;
  bool source_auto = false;
  std::vector<bool> derived_auto;
};

std::vector<std::string> sorted(std::vector<std::string> names) {
  std::sort(names.begin(), names.end());
  return names;
}

/// Section 7f: every string and member name of the payload is scalar text in NFC. Refused rather
/// than normalised, because a database identifier keeps its exact bytes and two spellings that
/// normalise alike can name two different tables.
void require_canonical_text(const Json& value) {
  static const char* const kMessage =
      "placement map payload strings and member names must be Unicode scalar text in NFC";
  switch (value.kind()) {
    case Json::Kind::string:
      if (!is_nfc(value.as_string())) throw MapError(kMessage);
      break;
    case Json::Kind::array:
      for (const Json& item : value.as_array()) require_canonical_text(item);
      break;
    case Json::Kind::object:
      for (const auto& [key, item] : value.as_object()) {
        if (!is_nfc(key)) throw MapError(kMessage);
        require_canonical_text(item);
      }
      break;
    default:
      break;
  }
}

Json without_signature(const Json& document) {
  Json payload = document;
  payload.erase("signature");
  return payload;
}

bool lowercase_hex32(std::string_view text) {
  if (text.size() != 32) return false;
  return std::all_of(text.begin(), text.end(),
                     [](char c) { return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'); });
}

/// The first 200 code points of a message, as the reference's `str(exc)[:200]`.
std::string truncated(std::string_view message) {
  std::size_t pos = 0;
  for (int count = 0; count < 200 && pos < message.size(); ++count) {
    ++pos;
    while (pos < message.size() && (static_cast<unsigned char>(message[pos]) & 0xC0U) == 0x80U) {
      ++pos;
    }
  }
  return std::string(message.substr(0, pos));
}

// ── Signatures ──────────────────────────────────────────────────────────────────────────────────

/// The configured keys, by name, after refusing what cannot be a key - an empty set, or one of the
/// wrong length. Both are the caller's configuration rather than the map, and are refused as such:
/// a key pasted a byte short would otherwise look like a map that does not verify.
std::vector<std::pair<std::string, std::string>> key_set(const PublicKeys& public_keys) {
  if (!public_keys.is_bare() && public_keys.keys().empty()) {
    throw MapError(
        "an empty set of public keys was supplied. That is not the no-account mode - a map with "
        "no signature is - it is a configuration that can verify nothing. Pass the keys, or load "
        "an unsigned map.");
  }
  std::vector<std::pair<std::string, std::string>> pairs(public_keys.keys().begin(),
                                                         public_keys.keys().end());
  for (const auto& [name, key] : pairs) {
    if (key.size() != 32) {
      throw MapError("the public key " + python_repr(name.empty() ? "(unnamed)" : name) + " is " +
                     std::to_string(key.size()) +
                     " bytes and an Ed25519 public key is 32. Refused here rather than at "
                     "verification: a key pasted a byte short would look like a map that does not "
                     "verify, and the two have completely different fixes.");
    }
  }
  return pairs;
}

/// Verifies the signature, and reports which of the caller's keys did it: its name, or nothing for
/// a bare key. Every key is tried; `key_id` only orders the attempts, because the signature block is
/// outside what is signed and anybody can edit it.
std::optional<std::string> verify_signature(const Json& document, const PublicKeys& public_keys,
                                            int contract) {
  const Json& signature = *document.find("signature");
  const Json* alg = signature.is_object() ? signature.find("alg") : nullptr;
  if (alg == nullptr || !alg->is_string() || alg->as_string() != "ed25519") {
    throw MapError("only ed25519 signatures are understood");
  }
  const Json* value = signature.find("value");
  std::optional<std::string> decoded;
  if (value != nullptr && value->is_string()) {
    decoded = detail::base64_decode_as_python(value->as_string());
  }
  if (!decoded) throw MapError("the signature is not valid base64");

  std::optional<std::string> claimed;
  if (const Json* key_id = signature.find("key_id"); key_id != nullptr && key_id->is_string()) {
    claimed = key_id->as_string();
  }
  const auto keys = key_set(public_keys);
  std::vector<std::pair<std::string, std::string>> ordered;
  if (claimed) {
    for (const auto& pair : keys) {
      if (pair.first == *claimed) ordered.push_back(pair);
    }
  }
  for (const auto& pair : keys) {
    if (!claimed || pair.first != *claimed) ordered.push_back(pair);
  }

  std::string payload;
  try {
    payload = canonical_bytes(without_signature(document));
  } catch (const CanonicalError& error) {
    // A signature is over canonical bytes, so a payload that has none cannot carry a valid one.
    if (contract >= GENERATIONS_SINCE) {
      throw MapError("contract 4 requires a canonically encodable placement map");
    }
    throw MapError(std::string("this map is signed and its payload has no canonical form, so no "
                               "signature over it can verify: ") +
                   error.what());
  }
  for (const auto& [name, key] : ordered) {
    if (detail::ed25519_verify(key, payload, *decoded)) {
      return name.empty() ? std::nullopt : std::optional<std::string>(name);
    }
  }

  std::vector<std::string> tried;
  for (const auto& [name, key] : ordered) tried.push_back(name.empty() ? "(unnamed)" : name);
  throw MapError("the map's signature does not verify against any of the " +
                 std::to_string(ordered.size()) + " key(s) supplied (" + python_repr(tried) +
                 "); the map says it was signed with " +
                 (claimed ? python_repr(*claimed) : std::string("None")) +
                 ". This is refused rather than warned about: the map decides where your data is "
                 "written. If a rotation is in progress, the key named above is the one to add - "
                 "and while both are configured, maps signed with either are accepted.");
}

// ── Layouts and materialisations ────────────────────────────────────────────────────────────────

detail::Tables read_tables(const Json& tables, const std::string& where) {
  detail::Tables out;
  for (const auto& [entity, table] : tables.as_object()) {
    if (!table.is_string() || table.as_string().empty()) {
      throw MapError(where + ": tables[" + python_repr(entity) + "] must be a table name, not " +
                     python_repr(table));
    }
    out.emplace(entity, table.as_string());
  }
  return out;
}

detail::Columns read_columns(const Json* columns, const std::string& where) {
  detail::Columns out;
  if (columns == nullptr || columns->is_null()) return out;
  if (!columns->is_object()) {
    throw MapError(where + ": columns maps an entity to an object of column name -> type, not " +
                   python_repr(*columns));
  }
  for (const auto& [entity, types] : columns->as_object()) {
    if (!types.is_object()) {
      throw MapError(where + ": columns[" + python_repr(entity) +
                     "] must be an object of column name -> type, not " + python_repr(types));
    }
    auto& entity_columns = out[entity];
    for (const auto& [column, type] : types.as_object()) {
      if (!type.is_string() || type.as_string().empty()) {
        throw MapError(where + ": columns[" + python_repr(entity) + "][" + python_repr(column) +
                       "] must be a type name, not " + python_repr(type));
      }
      entity_columns.emplace(column, type.as_string());
    }
  }
  return out;
}

/// A layout, or the report that it asked to be derived. `{"auto": true}` derives a PostgreSQL
/// layout, always: a layout has no dialect in it and a map names engines rather than dialects, so
/// there is nothing here the right dialect could be known from (section 7).
std::optional<PhysicalLayout> read_layout(const Json& raw, const std::string& where, int contract) {
  if (!raw.is_object()) throw MapError(where + ": layout must be an object");
  if (const Json* flag = raw.find("auto"); flag != nullptr && flag->is_bool() && flag->as_bool()) {
    if (raw.as_object().size() != 1) {
      throw MapError(where +
                     ": a layout is either auto or explicit, not both. Two sources of truth for a "
                     "schema is how a column ends up existing in one place and not the other.");
    }
    return std::nullopt;
  }
  const Json* tables = raw.find("tables");
  if (tables == nullptr || !tables->is_object() || tables->as_object().empty()) {
    throw MapError(where +
                   ": layout needs a non-empty 'tables' mapping entity -> table name, or "
                   "{\"auto\": true} to have one derived from the model");
  }
  PhysicalLayout layout;
  layout.tables = read_tables(*tables, where);
  for (const auto& [name, holds] : kReservedTables) {
    std::vector<std::string> reserved;
    for (const auto& [entity, table] : layout.tables) {
      if (table == name) reserved.push_back(entity);
    }
    if (!reserved.empty()) {
      throw MapError(where + ": " + python_repr(reserved) + " would be stored in a table called " +
                     python_repr(name) + ", which this library keeps its own bookkeeping in - " +
                     std::string(holds) +
                     ". A client table under that name would be read as bookkeeping and written to "
                     "as bookkeeping. Rename the table; the name is yours to choose everywhere "
                     "else.");
    }
  }
  layout.columns = read_columns(raw.find("columns"), where);
  if (contract < PHYSICAL_DESIGN_SINCE) {
    // Before contract 5 `partition_by` had no meaning: it was parsed and no renderer applied it.
    // A contract-4 document may not start meaning something by it now (errors/037).
    if (const Json* partition = raw.find("partition_by");
        partition != nullptr && detail::python_truthy(*partition)) {
      throw MapError(where +
                     ": this layout declares partition_by in a document declaring map contract " +
                     std::to_string(contract) +
                     ", where no library renders it - the table would be created unpartitioned "
                     "and nothing would say so. Partitioning is map contract " +
                     std::to_string(PHYSICAL_DESIGN_SINCE) + "; remove the key or raise the contract.");
    }
    if (raw.contains("key_order")) {
      throw MapError(where + ": key_order is map contract " +
                     std::to_string(PHYSICAL_DESIGN_SINCE) + " and this document declares " +
                     std::to_string(contract) +
                     ". A library of that contract would ignore it and order the key as declared, "
                     "so one document would build two different tables.");
    }
  } else {
    // Absent is "no design"; a key present with null is a value that is not one, and is refused.
    const Json* key_order = raw.find("key_order");
    layout.key_order =
        detail::parse_key_order(key_order != nullptr ? *key_order : Json::object(), where, layout.tables);
    const Json* partition_by = raw.find("partition_by");
    layout.partition_by = detail::parse_partition_by(
        partition_by != nullptr ? *partition_by : Json::object(), where, layout.tables);
  }
  layout.indexes =
      detail::parse_indexes(raw.find("indexes"), where, layout.tables, layout.columns, contract);
  return layout;
}

std::int64_t read_lag_budget(const Json& lag, const std::string& where) {
  std::optional<std::int64_t> value;
  if (lag.is_number()) {
    value = lag.to_int64();
    if (!value && !lag.as_number().integer) {
      // An integral value in any spelling, as the reference's int() of an integral float.
      const Json integral = detail::integral_numbers(lag);
      value = integral.to_int64();
    }
  }
  if (!value || *value < 0) {
    throw MapError(where + ": lag_budget_ms must be a non-negative integer number of "
                           "milliseconds, not " +
                   python_repr(lag));
  }
  return *value;
}

ReadMaterialization read_materialization(const Json& raw, const std::string& where, bool source,
                                         int contract) {
  if (!raw.is_object()) throw MapError(where + ": expected an object");
  for (const char* required : {"id", "engine", "layout"}) {
    if (!raw.contains(required)) {
      throw MapError(where + ": materialisation is missing " + python_repr(required));
    }
  }
  const Json* lag = raw.find("lag_budget_ms");
  const bool has_lag = lag != nullptr && !lag->is_null();
  if (source && has_lag) {
    throw MapError(where +
                   ": the source materialisation cannot have a lag budget. The source is where "
                   "writes land, so it is by definition not behind anything.");
  }
  if (!source && !has_lag) {
    throw MapError(where +
                   ": a derived materialisation needs lag_budget_ms. Without it nobody - not the "
                   "client, not the monitoring - can tell whether it is healthy or hours behind.");
  }
  std::optional<PhysicalLayout> layout = read_layout(*raw.find("layout"), where, contract);

  ReadMaterialization out;
  const Json& id = *raw.find("id");
  const Json& engine = *raw.find("engine");
  if (!id.is_string()) {
    throw MapError(where + ": a materialisation id is a string, not " + python_repr(id));
  }
  if (!engine.is_string()) {
    throw MapError(where + ": an engine is named by a string, not " + python_repr(engine));
  }
  out.material.id = id.as_string();
  out.material.engine = engine.as_string();
  if (has_lag) out.material.lag_budget_ms = read_lag_budget(*lag, where);
  if (layout) {
    out.material.layout = std::move(*layout);
  } else {
    out.auto_layout = true;
  }
  return out;
}

std::vector<std::string> read_also_write(const Json* raw, const std::string& where, int contract,
                                         const Materialization& source,
                                         const std::vector<Materialization>& derived) {
  std::vector<std::string> out;
  if (raw == nullptr || raw->is_null()) return out;
  if (contract < ALSO_WRITE_SINCE) {
    throw MapError(where + ": this map declares format contract " + std::to_string(contract) +
                   " and uses 'also_write', which contract " + std::to_string(ALSO_WRITE_SINCE) +
                   " introduced. Refused rather than honoured, and the reason is who makes this "
                   "mistake: a producer that grew the key and forgot to raise the number. "
                   "Honouring it would mean a contract-1 library ignoring the fan-out while a "
                   "contract-2 one performs it - the same document, two sets of engines written "
                   "to - which is the whole failure the version exists to prevent.");
  }
  if (!raw->is_array() || raw->as_array().empty()) {
    throw MapError(where +
                   ": 'also_write' is a non-empty list of materialisation ids, or absent. Absent "
                   "means writes go to the source alone; an empty list would be a claim that "
                   "fan-out was considered and none chosen, which is a stronger thing to say and "
                   "not what the planner means when it omits the key.");
  }
  std::vector<std::string> derived_ids;
  for (const Materialization& material : derived) derived_ids.push_back(material.id);
  std::set<std::string> seen;
  for (const Json& entry : raw->as_array()) {
    if (!entry.is_string()) {
      throw MapError(where + ": 'also_write' holds materialisation ids, not " + python_repr(entry));
    }
    const std::string& id = entry.as_string();
    if (id == source.id) {
      throw MapError(where + ": 'also_write' names the source " + python_repr(id) +
                     ". The source is where writes already land - listing it would either write "
                     "the row twice or read as though the source were somehow optional, and both "
                     "are worse than the refusal.");
    }
    if (seen.count(id) != 0) {
      throw MapError(where + ": 'also_write' names " + python_repr(id) +
                     " twice. Two identical fan-out targets is either a duplicated row or a "
                     "document nobody meant to write.");
    }
    if (std::find(derived_ids.begin(), derived_ids.end(), id) == derived_ids.end()) {
      throw MapError(where + ": 'also_write' names " + python_repr(id) +
                     ", which is not a derived materialisation of this group. It has " +
                     python_repr(sorted(derived_ids)) +
                     ". A fan-out target that does not exist is a write with nowhere to go, and "
                     "during a migration that is a row the copy never receives.");
    }
    seen.insert(id);
    out.push_back(id);
  }
  return out;
}

ReadGroup read_group(const std::string& name, const Json& body, int contract) {
  const std::string where = "group " + python_repr(name);
  if (!body.is_object() || !body.contains("source")) {
    throw MapError(where + ": needs a 'source' materialisation");
  }
  ReadGroup out;
  out.placement.group = name;
  bool unfenced = false;
  if (contract >= GENERATIONS_SINCE) {
    unfenced = contract >= UNFENCED_SINCE && !body.contains("write_epoch");
    if (!unfenced) {
      const Json* epoch = body.find("write_epoch");
      const auto value = epoch != nullptr ? epoch->to_int64() : std::nullopt;
      if (!value || *value < 1 || *value > MAX_EPOCH) {
        throw MapError(contract < UNFENCED_SINCE
                           ? where + ": contract 4 requires a positive safe write_epoch"
                           : where + ": contract " + std::to_string(contract) +
                                 " requires a positive safe write_epoch, or no write_epoch key "
                                 "for a group whose engine cannot fence writes");
      }
      out.placement.write_epoch = *value;
    }
  } else if (body.contains("write_epoch")) {
    throw MapError(where + ": write_epoch requires placement map contract 4");
  }
  if (unfenced && (body.contains("derived") || body.contains("also_write"))) {
    throw MapError(where +
                   ": a group without a write generation has only a source. A maintained copy "
                   "and a fan-out exist only through a migration, and a migration needs the write "
                   "generations this group does not carry.");
  }

  ReadMaterialization source = read_materialization(*body.find("source"), where + ".source", true,
                                                    contract);
  out.placement.source = std::move(source.material);
  out.source_auto = source.auto_layout;

  if (const Json* derived = body.find("derived"); derived != nullptr && !derived->is_null()) {
    if (!derived->is_array()) {
      throw MapError(where + ": 'derived' is a list of materialisations, not " +
                     python_repr(*derived));
    }
    std::size_t position = 0;
    for (const Json& entry : derived->as_array()) {
      ReadMaterialization copy = read_materialization(
          entry, where + ".derived[" + std::to_string(position++) + "]", false, contract);
      out.placement.derived.push_back(std::move(copy.material));
      out.derived_auto.push_back(copy.auto_layout);
    }
  }

  std::set<std::string> ids{out.placement.source.id};
  for (const Materialization& copy : out.placement.derived) {
    if (!ids.insert(copy.id).second) throw MapError(where + ": two materialisations share an id");
  }
  out.placement.also_write = read_also_write(body.find("also_write"), where, contract,
                                             out.placement.source, out.placement.derived);
  return out;
}

// ── Rules that need the whole map, or the model ─────────────────────────────────────────────────

/// A derived copy in the source's engine naming the source's tables is not a copy: its lag would
/// always read zero and a read routed to it would silently read the source.
void refuse_shadowing(const GroupPlacement& placement) {
  std::set<std::string> source_tables;
  for (const auto& [entity, table] : placement.source.layout.tables) source_tables.insert(table);
  for (const Materialization& candidate : placement.derived) {
    if (candidate.engine != placement.source.engine) continue;
    std::set<std::string> shared;
    for (const auto& [entity, table] : candidate.layout.tables) {
      if (source_tables.count(table) != 0) shared.insert(table);
    }
    if (!shared.empty()) {
      throw MapError("group " + python_repr(placement.group) + ": materialisation " +
                     python_repr(candidate.id) +
                     " is in the same engine as the source and reuses its tables " +
                     python_repr(std::vector<std::string>(shared.begin(), shared.end())) +
                     ". That is not a copy of the group, it is the original with a second name in "
                     "the map, so its lag would always read as zero and a read routed to it would "
                     "silently be a read of the source.");
    }
  }
}

/// The physical design rules that need the model: a key order is a permutation of the key, and a
/// partition is on a key column of a time type. Materialisations in document order, entities in
/// name order (section 8a).
void check_physical(const GroupPlacement& placement, const Model& model) {
  for (const Materialization* material : placement.all()) {
    const PhysicalLayout& layout = material->layout;
    const std::string where =
        "group " + python_repr(placement.group) + ": " + python_repr(material->id);
    std::set<std::string> entities;
    for (const auto& [entity, unused] : layout.key_order) entities.insert(entity);
    for (const auto& [entity, unused] : layout.partition_by) entities.insert(entity);
    for (const std::string& entity : entities) {
      const Entity* spec = model.find_entity(entity);
      if (spec == nullptr) {
        throw MapError(where + ": the physical design names " + python_repr(entity) +
                       ", which this model does not declare, so there is no key to order or "
                       "partition by.");
      }
      std::map<std::string, std::string> field_types;
      for (const Field& field : spec->fields) field_types.emplace(field.name, field.type);
      const auto order = layout.key_order.find(entity);
      const auto partition = layout.partition_by.find(entity);
      detail::check_against_model(
          where, entity, spec->key, field_types,
          order != layout.key_order.end() ? &order->second : nullptr,
          partition != layout.partition_by.end() ? &partition->second : nullptr);
    }
  }
}

/// Every routing entry names a materialisation that exists - with a model, in the shape's own
/// group, since ids are unique only within one. Decided at load, so a broken entry is not found by
/// the one request that happens to route through it.
std::map<std::string, std::string> check_routing(const Json& routing,
                                                 const std::map<std::string, GroupPlacement>& groups,
                                                 const Model* model) {
  std::map<std::string, std::set<std::string>> declared;
  std::set<std::string> everywhere;
  for (const auto& [name, placement] : groups) {
    for (const Materialization* material : placement.all()) {
      declared[name].insert(material->id);
      everywhere.insert(material->id);
    }
  }
  std::map<std::string, const Json*> entries;
  for (const auto& [shape_id, target] : routing.as_object()) entries.emplace(shape_id, &target);

  std::map<std::string, std::string> out;
  for (const auto& [shape_id, target] : entries) {
    if (!target->is_string()) {
      throw MapError("the routing entry for shape " + python_repr(shape_id) +
                     " is not a materialisation id. Routing maps a shape id to one id, and "
                     "anything else is a table nobody can look up.");
    }
    if (everywhere.count(target->as_string()) == 0) {
      throw MapError("the routing table sends shape " + python_repr(shape_id) +
                     " to materialisation " + python_repr(target->as_string()) +
                     ", and no group in this map declares one with that id. The map is internally "
                     "inconsistent: declared ids are " +
                     python_repr(std::vector<std::string>(everywhere.begin(), everywhere.end())) +
                     ".");
    }
    out.emplace(shape_id, target->as_string());
  }
  if (model == nullptr) return out;

  for (const auto& [shape_id, target] : out) {
    const OperationShape* shape = model->find_shape(shape_id);
    if (shape == nullptr) {
      throw MapError("the routing table has an entry for shape " + python_repr(shape_id) +
                     ", and this model does not produce that shape. The model version matched, so "
                     "the two sides enumerated shapes differently - which is the divergence that "
                     "puts one library's write in a table another library never looks at.");
    }
    const auto group = declared.find(shape->group);
    if (group == declared.end() || group->second.count(target) == 0) {
      throw MapError("the routing table sends shape " + python_repr(shape_id) +
                     " - which belongs to group " + python_repr(shape->group) +
                     " - to materialisation " + python_repr(target) +
                     ", which that group does not declare. Materialisation ids are unique only "
                     "within a group, so this would read " +
                     shape->entity + " out of a copy that does not hold it.");
    }
  }
  return out;
}

}  // namespace

// ── Public types ────────────────────────────────────────────────────────────────────────────────

std::vector<const Materialization*> GroupPlacement::all() const {
  std::vector<const Materialization*> out{&source};
  for (const Materialization& copy : derived) out.push_back(&copy);
  return out;
}

const Materialization& GroupPlacement::by_id(std::string_view id) const {
  for (const Materialization* material : all()) {
    if (material->id == id) return *material;
  }
  throw MapError("group " + python_repr(group) + " has no materialisation " + python_repr(id) +
                 ", but the routing table points at it. The map is internally inconsistent.");
}

std::vector<const Materialization*> GroupPlacement::also_write_targets() const {
  std::vector<const Materialization*> out;
  for (const std::string& id : also_write) out.push_back(&by_id(id));
  return out;
}

PublicKeys PublicKeys::bare(std::string key) {
  PublicKeys keys;
  keys.bare_ = true;
  keys.keys_.emplace("", std::move(key));
  return keys;
}

PublicKeys PublicKeys::named(std::map<std::string, std::string> keys) {
  PublicKeys out;
  out.keys_ = std::move(keys);
  return out;
}

const GroupPlacement& PlacementMap::placement_of(std::string_view group) const {
  const auto found = groups_.find(std::string(group));
  if (found == groups_.end()) {
    throw MapError("no placement for group " + python_repr(group) +
                   ". Every group in the model needs one: a group with nowhere to live is not a "
                   "slow path, it is an unanswerable operation.");
  }
  return found->second;
}

// ── The loader ──────────────────────────────────────────────────────────────────────────────────

PlacementMap load_map(const Json& document, const LoadOptions& options) {
  try {
    PlacementMap map;
    // 1. The document, its contract, and its versions.
    if (!document.is_object()) throw MapError("a placement map is an object");
    Json raw = document;
    // From contract 4 an integral number is an integer however it is spelled, before the
    // signature is checked and the fingerprint taken (section 7d) - decided by the double the
    // reference reads the contract as, so `4.0` is contract 4 and `3.0` is not a version.
    if (const Json* candidate = raw.find("contract");
        candidate != nullptr && candidate->is_number() &&
        detail::python_float(candidate->as_number().lexeme) >= 4.0) {
      raw = detail::integral_numbers(raw);
    }
    const Json* contract_value = raw.find("contract");
    std::optional<std::int64_t> contract_number;
    if (contract_value != nullptr && contract_value->is_number() &&
        contract_value->as_number().integer) {
      contract_number = contract_value->to_int64();
      if (!contract_number) {
        // An integer past 64 bits is still a version number, just not one anybody implements.
        contract_number = contract_value->as_number().lexeme.front() == '-'
                              ? std::numeric_limits<std::int64_t>::min()
                              : std::numeric_limits<std::int64_t>::max();
      }
    }
    if (!contract_number) {
      throw MapError("this map declares format contract " +
                     (contract_value != nullptr ? python_repr(*contract_value)
                                                : std::string("None")) +
                     ", which is not a version number. This library reads " +
                     std::to_string(MAP_CONTRACT_FLOOR) + " to " + std::to_string(MAP_CONTRACT) +
                     ".");
    }
    const std::string declared = python_repr(*contract_value);
    if (*contract_number > MAP_CONTRACT) {
      throw MapError("this map declares format contract " + declared +
                     " and this library implements " + std::to_string(MAP_CONTRACT) +
                     ". Refusing rather than guessing: a version this library does not know may "
                     "say something with a key it has never heard of, and the difference between "
                     "two contract versions is exactly the kind of thing that would otherwise be "
                     "interpreted as a missing field meaning zero. Upgrade the library.");
    }
    if (*contract_number < MAP_CONTRACT_FLOOR) {
      throw MapError("this map declares format contract " + declared +
                     " and the oldest this library still reads is " +
                     std::to_string(MAP_CONTRACT_FLOOR) + ".");
    }
    const int contract = static_cast<int>(*contract_number);
    map.contract_ = contract;

    require_canonical_text(without_signature(raw));

    if (contract >= GENERATIONS_SINCE) {
      const Json* project = raw.find("project_id");
      if (project == nullptr || !project->is_string() || !lowercase_hex32(project->as_string())) {
        throw MapError("contract 4 requires a 32-digit lowercase hexadecimal project_id");
      }
      map.project_id_ = project->as_string();
    } else if (raw.contains("project_id")) {
      throw MapError("project_id requires placement map contract 4");
    }

    const Json* model_version = raw.find("model_version");
    if (model_version == nullptr || !model_version->is_string() ||
        model_version->as_string().empty()) {
      throw MapError("the map does not say which model version it is for");
    }
    if (options.model != nullptr && options.model->version() != model_version->as_string()) {
      throw MapError("this map is for model version " + model_version->as_string() +
                     " and your declared model is " + options.model->version() +
                     ". Something changed in your entities; ask for a new map rather than "
                     "running against this one, because the difference cannot be guessed.");
    }
    map.model_version_ = model_version->as_string();

    const Json* map_version = raw.find("map_version");
    const auto version = map_version != nullptr ? map_version->to_int64() : std::nullopt;
    if (!version || *version < 1) {
      throw MapError("this map declares map_version " +
                     (map_version != nullptr ? python_repr(*map_version) : std::string("None")) +
                     ", which is not a version number. It used to be read as "
                     "int(document.get('map_version', 0)), so a document without one loaded as "
                     "version 0 - and this is the number that decides whether an older map is "
                     "being replayed over a newer one against the client's own engines.");
    }
    map.map_version_ = *version;

    // 2. The signature, before the structure: a document whose origin cannot be established is
    // not worth a detailed reading.
    const Json* signature = raw.find("signature");
    map.signed_ = signature != nullptr && !signature->is_null();
    if (options.require_signature && !map.signed_) {
      throw MapError("a signature was required and this map has none");
    }
    if (map.signed_) {
      if (!options.public_keys) {
        throw MapError(
            "this map is signed, which is a claim that it came from us, and no public key was "
            "provided to check that claim. Either pass the key, or use an unsigned map - an "
            "unsigned map is a supported mode, an unverifiable claim is not.");
      }
      map.verified_with_ = verify_signature(raw, *options.public_keys, contract);
    }

    // 3. Every group, in name order - not the document's, which a parser may not even keep.
    const Json* groups_raw = raw.find("groups");
    if (groups_raw == nullptr || !groups_raw->is_object() || groups_raw->as_object().empty()) {
      throw MapError("the map places no groups");
    }
    std::map<std::string, const Json*> bodies;
    for (const auto& [name, body] : groups_raw->as_object()) bodies.emplace(name, &body);
    std::map<std::string, ReadGroup> read;
    for (const auto& [name, body] : bodies) read.emplace(name, read_group(name, *body, contract));

    const Json* routing_raw = raw.find("routing");
    const bool has_routing = routing_raw != nullptr && detail::python_truthy(*routing_raw);
    if (has_routing && !routing_raw->is_object()) {
      throw MapError("'routing' must be a mapping from shape id to materialisation id");
    }
    const Json routing = has_routing ? *routing_raw : Json::object();

    if (options.model != nullptr) {
      // 4. Coverage, both directions.
      const Model& model = *options.model;
      std::vector<std::string> missing;
      std::set<std::string> model_groups;
      for (const Group& group : model.groups()) {
        model_groups.insert(group.name);
        if (read.count(group.name) == 0) missing.push_back(group.name);
      }
      if (!missing.empty()) {
        throw MapError("the map does not place these groups: " + python_repr(missing) +
                       ". Every group in the model needs a home before anything can run.");
      }
      std::vector<std::string> unknown;
      for (const auto& [name, unused] : read) {
        if (model_groups.count(name) == 0) unknown.push_back(name);
      }
      if (!unknown.empty()) {
        throw MapError("the map places groups this model does not have: " + python_repr(unknown) +
                       ". The model version matched, so this is not a stale map: the two sides "
                       "derived colocation groups differently, and a group nobody declared has no "
                       "entities to hold.");
      }
      for (auto& [name, group] : read) {
        const Group* members = nullptr;
        for (const Group& candidate : model.groups()) {
          if (candidate.name == name) members = &candidate;
        }
        if (group.source_auto) group.placement.source.layout = default_layout(model, *members);
        for (std::size_t i = 0; i < group.derived_auto.size(); ++i) {
          if (group.derived_auto[i]) {
            group.placement.derived[i].layout = default_layout(model, *members);
          }
        }
      }
      for (const auto& [name, group] : read) refuse_shadowing(group.placement);
      // 5. The physical design rules that need the model.
      for (const auto& [name, group] : read) check_physical(group.placement, model);
    } else {
      for (const auto& [name, group] : read) {
        const bool any_auto =
            group.source_auto ||
            std::find(group.derived_auto.begin(), group.derived_auto.end(), true) !=
                group.derived_auto.end();
        if (any_auto) {
          throw MapError(
              "a layout asked to be derived with {\"auto\": true}, but no model was supplied to "
              "derive it from. Pass the model in LoadOptions to load_map().");
        }
      }
    }
    for (auto& [name, group] : read) map.groups_.emplace(name, std::move(group.placement));

    // 6. Routing, in shape-id order.
    map.routing_ = check_routing(routing, map.groups_, options.model);

    if (contract >= GENERATIONS_SINCE) {
      for (const auto& [name, placement] : map.groups_) {
        for (const Materialization* material : placement.all()) {
          for (const auto& [entity, columns] : material->layout.columns) {
            if (columns.count(std::string(EPOCH_COLUMN)) != 0) {
              throw MapError("the write-epoch column is reserved for the SDK");
            }
          }
          for (const auto& [entity, table] : material->layout.tables) {
            if (table == DRAIN_TABLE) {
              throw MapError("the write-fence drain log is reserved for the SDK");
            }
          }
        }
      }
    }

    try {
      map.fingerprint_ = sha256_hex(canonical_bytes(without_signature(raw)));
    } catch (const CanonicalError&) {
      if (contract >= GENERATIONS_SINCE) {
        throw MapError("contract 4 requires a canonically encodable placement map");
      }
      // A legacy unsigned map may carry annotations with no canonical form; it keeps working, and
      // the protocols that need a fingerprint refuse it instead.
      map.fingerprint_.reset();
    }

    if (options.log) {
      Json fields = Json::object();
      fields.set("model_version", map.model_version_);
      fields.set("map_version", map.map_version_);
      fields.set("signed", map.signed_);
      fields.set("forward_only", map.signed_);
      fields.set("key", map.verified_with_ ? Json(*map.verified_with_) : Json(nullptr));
      detail::emit(options.log, "sde.map.loaded", fields);
    }
    return map;
  } catch (const MapError& error) {
    if (options.log) {
      Json fields = Json::object();
      fields.set("error", "MapError");
      fields.set("reason", truncated(error.what()));
      detail::emit(options.log, "sde.map.rejected", fields);
    }
    throw;
  }
}

PlacementMap load_map(std::string_view json_text, const LoadOptions& options) {
  Json document;
  try {
    document = parse_json(json_text);
  } catch (const JsonError& error) {
    const MapError refusal(std::string("a placement map is a JSON document, and this text is not "
                                       "one: ") +
                           error.what());
    if (options.log) {
      Json fields = Json::object();
      fields.set("error", "MapError");
      fields.set("reason", truncated(refusal.what()));
      detail::emit(options.log, "sde.map.rejected", fields);
    }
    throw refusal;
  }
  return load_map(document, options);
}

}  // namespace sde
