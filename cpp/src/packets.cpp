#include "sde/packets.hpp"

#include <algorithm>
#include <array>
#include <cstdio>
#include <initializer_list>
#include <map>
#include <set>
#include <string>
#include <utility>

#include "encoding.hpp"
#include "generation.hpp"
#include "placement_internal.hpp"
#include "python_compat.hpp"
#include "sde/canonical.hpp"
#include "sde/errors.hpp"
#include "sde/layout.hpp"

namespace sde {

namespace {

// The three packets share their rules, and each refusal names the packet it is about.
constexpr std::string_view kCutover = "cutover";
constexpr std::string_view kStaging = "staging";
constexpr std::string_view kIndex = "index build";

std::string cat(std::initializer_list<std::string_view> parts) {
  std::string out;
  for (std::string_view part : parts) out += part;
  return out;
}

[[noreturn]] void refuse(std::string message) { throw MigrationRefused(std::move(message)); }

bool lowercase_hex(std::string_view text, std::size_t width) {
  return text.size() == width && std::all_of(text.begin(), text.end(), [](char c) {
           return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f');
         });
}

/// `_record`: an object, or `<subject> <name> must be an object`. Absent is not an object.
const Json& record(const Json* value, std::string_view name, std::string_view subject) {
  if (value == nullptr || !value->is_object()) {
    refuse(cat({subject, " ", name, " must be an object"}));
  }
  return *value;
}

/// `_hex`: exactly `width` lowercase hexadecimal digits.
std::string hex(const Json* value, std::size_t width, std::string_view name,
                std::string_view subject) {
  if (value == nullptr || !value->is_string() || !lowercase_hex(value->as_string(), width)) {
    refuse(cat({subject, " ", name, " must be ", std::to_string(width),
                " lowercase hexadecimal digits"}));
  }
  return value->as_string();
}

std::string hex(std::string_view value, std::size_t width, std::string_view name,
                std::string_view subject) {
  if (!lowercase_hex(value, width)) {
    refuse(cat({subject, " ", name, " must be ", std::to_string(width),
                " lowercase hexadecimal digits"}));
  }
  return std::string(value);
}

/// `type(value) is int` after the packet's numbers were made integral: an integral number, and not
/// a boolean. Nothing past 64 bits, which no rule here accepts anyway.
std::optional<std::int64_t> integer(const Json& value) {
  if (!value.is_number() || !value.as_number().integer) return std::nullopt;
  return value.to_int64();
}

/// `_positive`: an integer in [1, MAX_EPOCH], or `cutover <name> must be a positive safe integer`.
std::int64_t positive(std::optional<std::int64_t> value, std::string_view name) {
  if (!value || *value < 1 || *value > MAX_EPOCH) {
    refuse(cat({"cutover ", name, " must be a positive safe integer"}));
  }
  return *value;
}

bool exactly(const Json& object, std::initializer_list<std::string_view> keys) {
  return object.is_object() && object.as_object().size() == keys.size() &&
         std::all_of(keys.begin(), keys.end(),
                     [&](std::string_view key) { return object.contains(key); });
}

/// A copy of an object without some of its members.
Json without(const Json& object, std::initializer_list<std::string_view> keys) {
  Json out = Json::object();
  for (const auto& member : object.as_object()) {
    if (std::find(keys.begin(), keys.end(), member.first) == keys.end()) {
      out.as_object().push_back(member);
    }
  }
  return out;
}

bool same_bytes(const Json& left, const Json& right) {
  return canonical_bytes(left) == canonical_bytes(right);
}

template <class Value>
std::set<std::string> keys_of(const std::map<std::string, Value>& values) {
  std::set<std::string> out;
  for (const auto& entry : values) out.insert(entry.first);
  return out;
}

/// `_signature`: the block a packet and each map inside it carry - exactly `alg` and `value`, and
/// optionally `key_id`; Ed25519; the canonical base64 of 64 bytes. Checked before any key is tried,
/// because a malformed block is the packet's defect, not a key that does not match.
void signature_block(const Json& document, std::string_view subject) {
  const Json& signature = record(document.find("signature"), "signature", subject);
  if (!exactly(signature, {"alg", "value"}) && !exactly(signature, {"alg", "value", "key_id"})) {
    refuse(cat({subject, " signatures have missing or unknown fields"}));
  }
  const std::string malformed = cat({subject, " signatures must use ed25519 and canonical base64"});
  const Json& alg = *signature.find("alg");
  const Json& value = *signature.find("value");
  if (!alg.is_string() || alg.as_string() != "ed25519" || !value.is_string()) refuse(malformed);
  const std::optional<std::string> decoded = detail::base64_decode_as_python(value.as_string());
  if (!decoded || decoded->size() != 64 || detail::base64_encode(*decoded) != value.as_string()) {
    refuse(malformed);
  }
  if (const Json* key_id = signature.find("key_id"); key_id != nullptr && !key_id->is_string()) {
    refuse(cat({subject, " signature key_id must be a string"}));
  }
}

/// The packet's own signature, over its canonical bytes without the block, by the map's rule.
std::optional<std::string> verify_packet(const Json& body, const PublicKeys& public_keys) {
  return detail::verify_payload(*body.find("signature"), public_keys,
                                [&] { return canonical_bytes(without(body, {"signature"})); });
}

/// Before a candidate map is loaded, in document order: every layout is written out. A layout
/// derived with `{"auto": true}` would be derived by whichever library loads the map, so it is not
/// a physical design anybody signed.
void refuse_derived_layouts(const Json& document, std::string_view subject,
                            const std::string& needs_layouts) {
  for (const auto& member : record(document.find("groups"), "groups", subject).as_object()) {
    const Json& spot = record(&member.second, "group", subject);
    const Json* derived = spot.find("derived");
    if (derived != nullptr && !derived->is_array()) {
      refuse(cat({subject, " derived copies must be an array"}));
    }
    std::vector<const Json*> materials{spot.find("source")};
    if (derived != nullptr) {
      for (const Json& copy : derived->as_array()) materials.push_back(&copy);
    }
    for (const Json* raw : materials) {
      const Json& layout =
          record(record(raw, "materialization", subject).find("layout"), "layout", subject);
      if (const Json* automatic = layout.find("auto");
          automatic != nullptr && detail::python_truthy(*automatic)) {
        refuse(needs_layouts);
      }
    }
  }
}

/// A candidate map, which must be signed by one of the same keys.
PlacementMap load_candidate(const Json& document, const Model& model,
                            const PublicKeys& public_keys) {
  LoadOptions options;
  options.model = &model;
  options.public_keys = public_keys;
  options.require_signature = true;
  return load_map(document, options);
}

/// After a map of a staging or an index build loaded: every layout names its tables and columns.
void refuse_empty_layouts(const PlacementMap& map, const std::string& needs_layouts) {
  for (const auto& entry : map.groups()) {
    for (const Materialization* material : entry.second.all()) {
      if (material->layout.tables.empty() || material->layout.columns.empty()) {
        refuse(needs_layouts);
      }
    }
  }
}

std::string numbered_name(std::string_view prefix, std::string_view id, std::int64_t position) {
  char digits[8];
  std::snprintf(digits, sizeof digits, "%06lld", static_cast<long long>(position));
  return cat({prefix, id, "_", digits});
}

std::string name_of(const Json& index) {
  const Json* name = index.find("name");
  if (name == nullptr) return "None";
  return name->is_string() ? name->as_string() : detail::python_repr(*name);
}

Json first(const Json::Array& items, std::size_t count) {
  const auto end = items.begin() + static_cast<std::ptrdiff_t>(std::min(count, items.size()));
  return Json(Json::Array(items.begin(), end));
}

// ── Cutover ─────────────────────────────────────────────────────────────────────────────────────

struct CheckedCutover {
  Json body;
  std::string plan_id;
  std::string project_id;
  std::string group;
  int protocol = 0;
  std::int64_t pause_budget_ms = 0;
  std::string query_impact_digest;
  std::optional<std::string> verified_with;
  std::vector<PlacementMap> maps;  ///< before, success, abort
  std::optional<VerificationRequest> verification;
  std::int64_t source_epoch = 0;
};

CheckedCutover check_cutover(const Json& raw, const Model& model, const std::string& project_id,
                             const PublicKeys& public_keys) {
  CheckedCutover out;
  out.body = detail::integral_numbers(raw);
  const Json& body = record(&out.body, "plan", kCutover);
  if (!exactly(body, {"kind", "protocol", "plan_id", "project_id", "group", "pause_budget_ms",
                      "query_impact_digest", "verification", "before", "success", "abort",
                      "signature"})) {
    refuse("cutover plan has missing or unknown fields");
  }
  const std::optional<std::int64_t> protocol = integer(*body.find("protocol"));
  if (!protocol || (*protocol != CUTOVER_PROTOCOL && *protocol != CUTOVER_RELAYOUT_PROTOCOL)) {
    refuse("unsupported cutover plan protocol");
  }
  out.protocol = static_cast<int>(*protocol);
  if (const Json& kind = *body.find("kind");
      !kind.is_string() || kind.as_string() != "sde-cutover") {
    refuse("unsupported cutover document kind");
  }
  out.plan_id = hex(body.find("plan_id"), 32, "plan_id", kCutover);
  out.project_id = hex(body.find("project_id"), 32, "project_id", kCutover);
  if (out.project_id != project_id) {
    refuse("cutover plan belongs to another locally configured project");
  }
  out.pause_budget_ms = positive(integer(*body.find("pause_budget_ms")), "pause_budget_ms");
  out.query_impact_digest = hex(body.find("query_impact_digest"), 64, "query_impact_digest",
                                kCutover);
  const Json& group = *body.find("group");
  if (!group.is_string() || group.as_string().empty()) {
    refuse("cutover group must be a nonempty string");
  }
  out.group = group.as_string();
  signature_block(body, kCutover);
  out.verified_with = verify_packet(body, public_keys);

  // Every candidate is an object before any of them is read.
  constexpr std::array<std::string_view, 3> kCandidates = {"before", "success", "abort"};
  std::array<const Json*, 3> documents{};
  for (std::size_t i = 0; i < kCandidates.size(); ++i) {
    documents[i] = &record(body.find(kCandidates[i]), kCandidates[i], kCutover);
  }
  for (const Json* document : documents) {
    signature_block(*document, kCutover);
    refuse_derived_layouts(*document, kCutover, "cutover maps require explicit physical layouts");
    PlacementMap parsed = load_candidate(*document, model, public_keys);
    // Generations arrived in contract 4 and every later contract keeps them. The three candidates
    // share every top-level attribute, contract included, which the comparison below enforces.
    if (parsed.contract() < GENERATIONS_SINCE) {
      refuse(cat({"cutover protocol ", std::to_string(out.protocol),
                  " requires placement map contract ", std::to_string(GENERATIONS_SINCE),
                  " or later"}));
    }
    (void)detail::check_map_project(parsed, project_id);
    (void)positive(parsed.map_version(), "map_version");
    out.maps.push_back(std::move(parsed));
  }
  const PlacementMap& before = out.maps[0];
  const PlacementMap& success = out.maps[1];
  const PlacementMap& aborted = out.maps[2];
  if (!(before.map_version() < success.map_version() &&
        success.map_version() < aborted.map_version())) {
    refuse("cutover versions must increase from before to success to abort");
  }
  VerificationRequest request = VerificationRequest::from_record(*body.find("verification"));
  if (!request.requires_signature()) {
    refuse("cutover verification must require the signed before map");
  }
  request.check_session(before, project_id, out.group);
  const GroupPlacement& spot = before.placement_of(out.group);
  // A group without a write generation (contract 6) never gets this far: the map refuses a copy in
  // such a group, and a cutover needs one.
  if (spot.derived.size() != 1 || spot.also_write != std::vector<std::string>{spot.derived[0].id}) {
    refuse("cutover requires exactly one derived copy maintained by fan-out");
  }
  const Materialization& source = spot.source;
  const Materialization& target = spot.derived[0];
  const bool same = source.engine == target.engine;
  if (out.protocol == CUTOVER_PROTOCOL && same) {
    refuse("cutover source and target must use different engine bindings");
  }
  if (out.protocol == CUTOVER_RELAYOUT_PROTOCOL && !same) {
    refuse("a relayout (cutover protocol 2) activates a copy in the source's own engine binding");
  }
  const std::int64_t epoch = spot.write_epoch.value();
  if (epoch > MAX_EPOCH - 2) refuse("cutover needs two available write generations");

  std::set<std::string> affected;
  for (const OperationShape& shape : model.shapes()) {
    if (shape.group == out.group) affected.insert(shape.id);
  }
  for (const std::string& shape : affected) {
    if (const auto route = before.routing().find(shape);
        route != before.routing().end() && route->second != source.id) {
      refuse("cutover before-map reads must still use the source");
    }
  }
  const Json& before_raw = *documents[0];
  const Json& before_groups = *before_raw.find("groups");
  std::map<std::string, std::string> expected_routes;
  for (const auto& [shape, materialization] : before.routing()) {
    if (!affected.contains(shape)) expected_routes.emplace(shape, materialization);
  }
  const std::string stable =
      canonical_bytes(without(before_raw, {"signature", "map_version", "groups", "routing"}));
  struct Terminal {
    std::size_t candidate;
    const Materialization* material;
    std::int64_t epoch;
  };
  for (const Terminal& terminal_case :
       {Terminal{1, &target, epoch + 2}, Terminal{2, &source, epoch + 1}}) {
    const Json& document = *documents[terminal_case.candidate];
    const PlacementMap& parsed = out.maps[terminal_case.candidate];
    if (canonical_bytes(without(document, {"signature", "map_version", "groups", "routing"})) !=
            stable ||
        keys_of(parsed.groups()) != keys_of(before.groups())) {
      refuse("cutover cannot change other map attributes or groups");
    }
    if (parsed.routing() != expected_routes) {
      refuse("cutover terminal routing must preserve the unaffected groups");
    }
    const Json& groups = *document.find("groups");
    for (const auto& entry : before.groups()) {
      if (entry.first != out.group &&
          !same_bytes(*groups.find(entry.first), *before_groups.find(entry.first))) {
        refuse("cutover cannot change an unaffected group");
      }
    }
    const Json& terminal = *groups.find(out.group);
    if (!exactly(terminal, {"source", "write_epoch"}) ||
        integer(*terminal.find("write_epoch")) != terminal_case.epoch) {
      refuse("cutover terminal group must contain only its source and decision generation");
    }
    const Json& candidate = *terminal.find("source");
    if (const Json* id = candidate.find("id");
        id == nullptr || !id->is_string() || id->as_string().empty()) {
      refuse("cutover terminal source must have a nonempty string id");
    }
    const Json& original_group = *before_groups.find(out.group);
    const Json& original = terminal_case.candidate == 2
                               ? *original_group.find("source")
                               : original_group.find("derived")->as_array().front();
    if (!same_bytes(without(candidate, {"id"}), without(original, {"id", "lag_budget_ms"})) ||
        parsed.placement_of(out.group).source.engine != terminal_case.material->engine) {
      refuse("cutover terminal map changed the authorized physical materialization");
    }
  }
  out.verification = std::move(request);
  out.source_epoch = epoch;
  return out;
}

// ── Staging ─────────────────────────────────────────────────────────────────────────────────────

struct CheckedStaging {
  Json body;
  std::string stage_id;
  std::string project_id;
  std::string group;
  int protocol = 0;
  std::optional<std::string> verified_with;
  std::vector<PlacementMap> maps;  ///< current, prepared
};

CheckedStaging check_staging(const Json& raw, const Model& model, const std::string& project_id,
                             const PublicKeys& public_keys) {
  CheckedStaging out;
  out.body = detail::integral_numbers(raw);
  const Json& body = record(&out.body, "authorization", kStaging);
  if (!exactly(body, {"kind", "protocol", "stage_id", "project_id", "group", "current",
                      "prepared", "signature"})) {
    refuse("staging authorization has missing or unknown fields");
  }
  const std::optional<std::int64_t> protocol = integer(*body.find("protocol"));
  const Json& kind = *body.find("kind");
  if (!protocol || (*protocol != STAGING_PROTOCOL && *protocol != STAGING_RELAYOUT_PROTOCOL) ||
      !kind.is_string() || kind.as_string() != "sde-stage") {
    refuse("unsupported staging authorization kind or protocol");
  }
  out.protocol = static_cast<int>(*protocol);
  out.stage_id = hex(body.find("stage_id"), 32, "stage_id", kStaging);
  out.project_id = hex(body.find("project_id"), 32, "project_id", kStaging);
  if (out.project_id != project_id) {
    refuse("staging authorization belongs to another local project");
  }
  const Json& group = *body.find("group");
  if (!group.is_string() || group.as_string().empty()) {
    refuse("staging group must be a nonempty string");
  }
  out.group = group.as_string();
  signature_block(body, kStaging);
  out.verified_with = verify_packet(body, public_keys);
  for (std::string_view name : {std::string_view("current"), std::string_view("prepared")}) {
    const Json& document = record(body.find(name), name, kStaging);
    signature_block(document, kStaging);
    refuse_derived_layouts(document, kStaging, "staging maps need explicit physical layouts");
    PlacementMap parsed = load_candidate(document, model, public_keys);
    // Contract 4 introduced the generations this protocol rests on; later contracts keep them.
    if (parsed.contract() < GENERATIONS_SINCE) {
      refuse(cat({"staging protocol ", std::to_string(out.protocol), " requires map contract ",
                  std::to_string(GENERATIONS_SINCE), " or later"}));
    }
    (void)detail::check_map_project(parsed, project_id);
    refuse_empty_layouts(parsed, "staging maps need explicit physical layouts");
    out.maps.push_back(std::move(parsed));
  }
  const PlacementMap& current = out.maps[0];
  const PlacementMap& prepared = out.maps[1];
  // The prepared map may raise the contract, because the fresh copy is the one place a physical
  // design can first appear. It may not lower it: a lower number would tell an older library it may
  // ignore keys the current map already relies on.
  if (prepared.contract() < current.contract()) {
    refuse("staging cannot lower the placement map contract");
  }
  // Contract 6 is for a group without a write generation, and no such group can appear through a
  // staging: the staged group keeps its generation and every other group is carried byte for byte.
  if (prepared.contract() >= UNFENCED_SINCE && current.contract() < UNFENCED_SINCE) {
    refuse(cat({"staging cannot raise the placement map contract to ",
                std::to_string(UNFENCED_SINCE),
                ": a group without a write generation cannot appear through a staging"}));
  }
  if (current.map_version() >= prepared.map_version()) {
    refuse("staging must allocate a newer prepared map");
  }
  if (!current.groups().contains(out.group) ||
      keys_of(current.groups()) != keys_of(prepared.groups())) {
    refuse("staging cannot add or remove colocation groups");
  }
  const GroupPlacement& old = current.groups().at(out.group);
  const GroupPlacement& next = prepared.groups().at(out.group);
  if (!old.write_epoch) {
    refuse(cat({"group ", out.group,
                " carries no write generation, so it cannot be staged: a staging ends in a cutover "
                "that cuts off old writers by their generation, and its engine cannot fence "
                "writes"}));
  }
  if (!old.derived.empty() || !old.also_write.empty()) {
    refuse("staging begins with a source-only group");
  }
  if (next.derived.size() != 1 || next.also_write != std::vector<std::string>{next.derived[0].id}) {
    refuse("staging prepares exactly one maintained copy");
  }
  const std::int64_t epoch = *old.write_epoch;
  if (epoch > MAX_EPOCH - 2 || next.write_epoch != epoch) {
    refuse("staging retains the source generation and needs two spare generations");
  }
  const bool same = next.derived[0].engine == old.source.engine;
  if (out.protocol == STAGING_PROTOCOL && same) {
    refuse("staging target must use another engine binding");
  }
  if (out.protocol == STAGING_RELAYOUT_PROTOCOL && !same) {
    // The copy's tables are fresh stage names, and the map refuses a copy in the source's engine
    // that reuses a source table - so a relayout cannot be the source under a new name.
    refuse("a relayout (staging protocol 2) prepares its copy in the source's own engine binding");
  }
  const Json& current_raw = *body.find("current");
  const Json& prepared_raw = *body.find("prepared");
  const Json& old_groups = *current_raw.find("groups");
  const Json& new_groups = *prepared_raw.find("groups");
  const Json& old_group = *old_groups.find(out.group);
  const Json& new_group = *new_groups.find(out.group);
  if (!exactly(old_group, {"source", "write_epoch"}) ||
      !exactly(new_group, {"source", "write_epoch", "derived", "also_write"})) {
    refuse("staging group shape is not source-only to one maintained copy");
  }
  if (!same_bytes(*old_group.find("source"), *new_group.find("source"))) {
    refuse("staging cannot change the existing source");
  }
  if (!same_bytes(without(current_raw, {"signature", "map_version", "groups", "contract"}),
                  without(prepared_raw, {"signature", "map_version", "groups", "contract"})) ||
      current.routing() != prepared.routing()) {
    refuse("staging cannot change routing or other map attributes");
  }
  for (const auto& entry : current.groups()) {
    if (entry.first != out.group &&
        !same_bytes(*old_groups.find(entry.first), *new_groups.find(entry.first))) {
      refuse("staging cannot change an unaffected group");
    }
  }
  const auto members = std::find_if(model.groups().begin(), model.groups().end(),
                                    [&](const Group& candidate) {
                                      return candidate.name == out.group;
                                    });
  std::map<std::string, std::string> expected;
  std::int64_t position = 0;
  for (const std::string& entity : members->members) {  // sorted, by code point
    expected.emplace(entity, staging_table_name(out.stage_id, ++position));
  }
  if (next.derived[0].layout.tables != expected) {
    refuse("staging needs fresh physical names bound to its stage id and entity order");
  }
  const std::map<std::string, NeutralColumns> columns = group_columns(model, *members);
  for (const Materialization* material : {&old.source, &next.derived[0]}) {
    if (keys_of(material->layout.tables) != keys_of(columns) ||
        keys_of(material->layout.columns) != keys_of(columns)) {
      refuse("staging layouts must cover exactly the group's entities");
    }
    for (const auto& [entity, fields] : columns) {
      std::set<std::string> declared;
      for (const auto& field : fields) declared.insert(field.first);
      if (keys_of(material->layout.columns.at(entity)) != declared) {
        refuse("staging layout columns must match the logical group");
      }
    }
  }
  std::set<std::string> used;
  for (const auto& entry : current.groups()) {
    for (const Materialization* material : entry.second.all()) {
      for (const auto& table : material->layout.tables) used.insert(table.second);
    }
  }
  for (const auto& table : expected) {
    if (used.contains(table.second)) refuse("staging cannot reuse a current physical name");
  }
  return out;
}

// ── Index build ─────────────────────────────────────────────────────────────────────────────────

struct CheckedIndex {
  Json body;
  std::string index_id;
  std::string project_id;
  std::string group;
  int protocol = 0;
  std::int64_t build_budget_ms = 0;
  std::optional<std::string> verified_with;
  std::vector<PlacementMap> maps;  ///< current, prepared
  std::size_t kept = 0;
  std::set<std::string> removed;
};

/// Every table and index name any layout of a map document uses.
std::set<std::string> names_in(const Json& document) {
  std::set<std::string> found;
  for (const auto& group : document.find("groups")->as_object()) {
    std::vector<const Json*> materials{group.second.find("source")};
    if (const Json* derived = group.second.find("derived"); derived != nullptr) {
      for (const Json& copy : derived->as_array()) materials.push_back(&copy);
    }
    for (const Json* material : materials) {
      const Json& layout = *material->find("layout");
      if (const Json* tables = layout.find("tables"); tables != nullptr) {
        for (const auto& table : tables->as_object()) {
          found.insert(table.second.is_string() ? table.second.as_string()
                                                : detail::python_repr(table.second));
        }
      }
      if (const Json* indexes = layout.find("indexes"); indexes != nullptr && indexes->is_array()) {
        for (const Json& index : indexes->as_array()) found.insert(name_of(index));
      }
    }
  }
  return found;
}

/// A layout's indexes as written: absent and null are none.
Json::Array indexes_of(const Json& material) {
  const Json* indexes = material.find("layout")->find("indexes");
  return indexes != nullptr && indexes->is_array() ? indexes->as_array() : Json::Array{};
}

CheckedIndex check_index(const Json& raw, const Model& model, const std::string& project_id,
                         const PublicKeys& public_keys) {
  CheckedIndex out;
  out.body = detail::integral_numbers(raw);
  const Json& body = record(&out.body, "authorization", kIndex);
  if (!exactly(body, {"kind", "protocol", "index_id", "project_id", "group", "current",
                      "prepared", "build_budget_ms", "signature"})) {
    refuse("index build authorization has missing or unknown fields");
  }
  const std::optional<std::int64_t> protocol = integer(*body.find("protocol"));
  const Json& kind = *body.find("kind");
  if (!protocol || (*protocol != INDEX_PROTOCOL && *protocol != INDEX_CHANGE_PROTOCOL) ||
      !kind.is_string() || kind.as_string() != "sde-index") {
    refuse("unsupported index build authorization kind or protocol");
  }
  out.protocol = static_cast<int>(*protocol);
  out.index_id = hex(body.find("index_id"), 32, "index_id", kIndex);
  out.project_id = hex(body.find("project_id"), 32, "project_id", kIndex);
  if (out.project_id != project_id) {
    refuse("index build authorization belongs to another local project");
  }
  const Json& group = *body.find("group");
  if (!group.is_string() || group.as_string().empty()) {
    refuse("index build group must be a nonempty string");
  }
  out.group = group.as_string();
  const std::optional<std::int64_t> budget = integer(*body.find("build_budget_ms"));
  if (!budget || *budget < 1 || *budget > MAX_BUILD_BUDGET_MS) {
    refuse(cat({"index build budget must be an integer from 1 through ",
                std::to_string(MAX_BUILD_BUDGET_MS), " ms"}));
  }
  out.build_budget_ms = *budget;
  signature_block(body, kIndex);
  out.verified_with = verify_packet(body, public_keys);
  for (std::string_view name : {std::string_view("current"), std::string_view("prepared")}) {
    const Json& document = record(body.find(name), name, kIndex);
    signature_block(document, kIndex);
    refuse_derived_layouts(document, kIndex, "index build maps need explicit physical layouts");
    PlacementMap parsed = load_candidate(document, model, public_keys);
    if (parsed.contract() < GENERATIONS_SINCE) {
      refuse(cat({"index build protocol ", std::to_string(out.protocol), " requires map contract ",
                  std::to_string(GENERATIONS_SINCE), " or later"}));
    }
    (void)detail::check_map_project(parsed, project_id);
    refuse_empty_layouts(parsed, "index build maps need explicit physical layouts");
    out.maps.push_back(std::move(parsed));
  }
  const PlacementMap& current = out.maps[0];
  const PlacementMap& prepared = out.maps[1];
  // The prepared map may raise the contract, because an index method first appears here - a BRIN
  // index under a contract-4 current map. It may not lower it.
  if (prepared.contract() < current.contract()) {
    refuse("an index build cannot lower the placement map contract");
  }
  if (prepared.contract() >= UNFENCED_SINCE && current.contract() < UNFENCED_SINCE) {
    refuse(cat({"an index build cannot raise the placement map contract to ",
                std::to_string(UNFENCED_SINCE),
                ": a group without a write generation cannot appear through an index build"}));
  }
  if (current.map_version() >= prepared.map_version()) {
    refuse("an index build must allocate a newer prepared map");
  }
  if (!current.groups().contains(out.group) ||
      keys_of(current.groups()) != keys_of(prepared.groups())) {
    refuse("an index build cannot add or remove colocation groups");
  }
  const GroupPlacement& old = current.groups().at(out.group);
  const GroupPlacement& next = prepared.groups().at(out.group);
  if (!old.write_epoch) {
    refuse(cat({"group ", out.group,
                " carries no write generation, so its indexes are not built in place: its engine "
                "has no physical design to change"}));
  }
  if (!old.derived.empty() || !old.also_write.empty() || !next.derived.empty() ||
      !next.also_write.empty()) {
    refuse("an index build begins and ends with a source-only group");
  }
  if (next.write_epoch != old.write_epoch) {
    // Nothing is fenced: the tables stay and every running process keeps writing to them.
    refuse("an index build keeps the source's write generation");
  }
  const Json& current_raw = *body.find("current");
  const Json& prepared_raw = *body.find("prepared");
  const Json& old_groups = *current_raw.find("groups");
  const Json& new_groups = *prepared_raw.find("groups");
  const Json& old_group = *old_groups.find(out.group);
  const Json& new_group = *new_groups.find(out.group);
  if (!exactly(old_group, {"source", "write_epoch"}) ||
      !exactly(new_group, {"source", "write_epoch"})) {
    refuse("an index build group is a source and its write generation only");
  }
  const Json& old_source = *old_group.find("source");
  const Json& new_source = *new_group.find("source");
  const auto without_indexes = [](const Json& material) {
    Json copy = material;
    copy.set("layout", without(*material.find("layout"), {"indexes"}));
    return copy;
  };
  if (!same_bytes(without_indexes(old_source), without_indexes(new_source))) {
    refuse("an index build changes nothing about the source but its indexes; a new key order, "
           "partition, table or engine is a relayout or a move");
  }
  const Json::Array in_force = indexes_of(old_source);
  const Json::Array after = indexes_of(new_source);
  Json::Array kept;
  if (out.protocol == INDEX_PROTOCOL) {
    kept = in_force;
    if (after.size() <= kept.size() || !same_bytes(first(after, kept.size()), Json(kept))) {
      refuse("an index build keeps every index in force, in order, and adds at least one after "
             "them");
    }
  } else {
    std::set<std::string> remaining;
    for (const Json& index : after) remaining.insert(name_of(index));
    for (const Json& index : in_force) {
      if (remaining.contains(name_of(index))) {
        kept.push_back(index);
      } else {
        out.removed.insert(name_of(index));
      }
    }
    if (out.removed.empty()) refuse("index build protocol 2 removes at least one index in force");
    if (!same_bytes(first(after, kept.size()), Json(kept))) {
      refuse("an index change keeps the other indexes in force, in order, before the new ones");
    }
  }
  out.kept = kept.size();
  std::set<std::string> added;
  for (std::size_t i = kept.size(); i < after.size(); ++i) {
    const Json* name = after[i].find("name");
    const std::string fresh =
        index_build_name(out.index_id, static_cast<std::int64_t>(i - kept.size() + 1));
    if (name == nullptr || !name->is_string() || name->as_string() != fresh) {
      refuse("new indexes need fresh names bound to the index build id and their position");
    }
    added.insert(fresh);
  }
  for (const std::string& name : names_in(current_raw)) {
    if (added.contains(name)) refuse("an index build cannot reuse a name the current map uses");
  }
  if (!same_bytes(without(current_raw, {"signature", "map_version", "groups", "contract"}),
                  without(prepared_raw, {"signature", "map_version", "groups", "contract"})) ||
      current.routing() != prepared.routing()) {
    refuse("an index build cannot change routing or other map attributes");
  }
  for (const auto& entry : current.groups()) {
    if (entry.first != out.group &&
        !same_bytes(*old_groups.find(entry.first), *new_groups.find(entry.first))) {
      refuse("an index build cannot change an unaffected group");
    }
  }
  return out;
}

/// The record a plan keeps: the packet's canonical bytes read back, as the reference's `as_record`.
Json canonical_record(const Json& body) { return parse_json(canonical_bytes(body)); }

std::string fingerprint_of(const Json& body) {
  return sha256_hex(canonical_bytes(without(body, {"signature"})));
}

}  // namespace

// ── Cutover ─────────────────────────────────────────────────────────────────────────────────────

CutoverPlan::CutoverPlan(PlacementMap before, PlacementMap success, PlacementMap abort,
                         VerificationRequest verification)
    : before_(std::move(before)),
      success_(std::move(success)),
      abort_(std::move(abort)),
      verification_(std::move(verification)) {}

std::string CutoverPlan::candidate_payload(std::string_view outcome) const {
  if (outcome != "success" && outcome != "abort") {
    refuse("cutover outcome must be success or abort");
  }
  return canonical_bytes(*document_.find(outcome));
}

void CutoverPlan::check_current(const PlacementMap& current) const {
  (void)detail::check_map_project(current, project_id_);
  if (!current.is_signed() || current.fingerprint() != before_.fingerprint()) {
    refuse("cutover plan does not name the current placement map");
  }
}

CutoverPlan load_cutover_plan(const Json& raw, const Model& model, const std::string& project_id,
                              const PublicKeys& public_keys) {
  try {
    CheckedCutover checked = check_cutover(raw, model, project_id, public_keys);
    CutoverPlan plan(std::move(checked.maps[0]), std::move(checked.maps[1]),
                     std::move(checked.maps[2]), std::move(*checked.verification));
    plan.plan_id_ = std::move(checked.plan_id);
    plan.project_id_ = std::move(checked.project_id);
    plan.group_ = std::move(checked.group);
    plan.protocol_ = checked.protocol;
    plan.pause_budget_ms_ = checked.pause_budget_ms;
    plan.query_impact_digest_ = std::move(checked.query_impact_digest);
    plan.verified_with_ = std::move(checked.verified_with);
    plan.source_epoch_ = checked.source_epoch;
    plan.fingerprint_ = fingerprint_of(checked.body);
    plan.document_ = canonical_record(checked.body);
    return plan;
  } catch (const MapError& error) {
    throw MigrationRefused(std::string("cutover document refused: ") + error.what());
  } catch (const CanonicalError& error) {
    throw MigrationRefused(std::string("cutover document refused: ") + error.what());
  }
}

// ── Staging ─────────────────────────────────────────────────────────────────────────────────────

std::string staging_table_name(std::string_view stage_id, std::int64_t position) {
  (void)hex(stage_id, 32, "stage_id", kStaging);
  if (position < 1 || position > 999999) {
    refuse("staging entity position must be an integer from 1 through 999999");
  }
  return numbered_name("sde_m_", stage_id, position);
}

StagingPlan::StagingPlan(PlacementMap current, PlacementMap prepared)
    : current_(std::move(current)), prepared_(std::move(prepared)) {}

std::string StagingPlan::prepared_payload() const {
  return canonical_bytes(*document_.find("prepared"));
}

void StagingPlan::check_current(const PlacementMap& current) const {
  (void)detail::check_map_project(current, project_id_);
  if (!current.is_signed() || current.fingerprint() != current_.fingerprint()) {
    refuse("staging authorization does not name the signed current map");
  }
}

StagingPlan load_staging_plan(const Json& raw, const Model& model, const std::string& project_id,
                              const PublicKeys& public_keys) {
  try {
    CheckedStaging checked = check_staging(raw, model, project_id, public_keys);
    StagingPlan plan(std::move(checked.maps[0]), std::move(checked.maps[1]));
    plan.stage_id_ = std::move(checked.stage_id);
    plan.project_id_ = std::move(checked.project_id);
    plan.group_ = std::move(checked.group);
    plan.protocol_ = checked.protocol;
    plan.verified_with_ = std::move(checked.verified_with);
    plan.fingerprint_ = fingerprint_of(checked.body);
    plan.document_ = canonical_record(checked.body);
    return plan;
  } catch (const MapError& error) {
    throw MigrationRefused(std::string("staging authorization refused: ") + error.what());
  } catch (const CanonicalError& error) {
    throw MigrationRefused(std::string("staging authorization refused: ") + error.what());
  }
}

// ── Index build ─────────────────────────────────────────────────────────────────────────────────

std::string index_build_name(std::string_view index_id, std::int64_t position) {
  (void)hex(index_id, 32, "index_id", kIndex);
  if (position < 1 || position > 999999) {
    refuse("index build position must be an integer from 1 through 999999");
  }
  return numbered_name("sde_i_", index_id, position);
}

IndexPlan::IndexPlan(PlacementMap current, PlacementMap prepared)
    : current_(std::move(current)), prepared_(std::move(prepared)) {}

std::string IndexPlan::prepared_payload() const {
  return canonical_bytes(*document_.find("prepared"));
}

void IndexPlan::check_current(const PlacementMap& current) const {
  (void)detail::check_map_project(current, project_id_);
  if (!current.is_signed() || current.fingerprint() != current_.fingerprint()) {
    refuse("index build authorization does not name the signed current map");
  }
}

IndexPlan load_index_plan(const Json& raw, const Model& model, const std::string& project_id,
                          const PublicKeys& public_keys) {
  try {
    CheckedIndex checked = check_index(raw, model, project_id, public_keys);
    IndexPlan plan(std::move(checked.maps[0]), std::move(checked.maps[1]));
    plan.index_id_ = std::move(checked.index_id);
    plan.project_id_ = std::move(checked.project_id);
    plan.group_ = std::move(checked.group);
    plan.protocol_ = checked.protocol;
    plan.build_budget_ms_ = checked.build_budget_ms;
    // From the loaded maps, in their order, not from the document's dictionaries.
    const std::vector<Index>& after =
        plan.prepared_.placement_of(plan.group_).source.layout.indexes;
    plan.added_.assign(after.begin() + static_cast<std::ptrdiff_t>(checked.kept), after.end());
    for (const Index& index : plan.current_.placement_of(plan.group_).source.layout.indexes) {
      if (checked.removed.contains(index.name)) plan.removed_.push_back(index);
    }
    plan.verified_with_ = std::move(checked.verified_with);
    plan.fingerprint_ = fingerprint_of(checked.body);
    plan.document_ = canonical_record(checked.body);
    return plan;
  } catch (const MapError& error) {
    throw MigrationRefused(std::string("index build authorization refused: ") + error.what());
  } catch (const CanonicalError& error) {
    throw MigrationRefused(std::string("index build authorization refused: ") + error.what());
  }
}

}  // namespace sde
