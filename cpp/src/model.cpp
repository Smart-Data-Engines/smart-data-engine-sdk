#include "sde/model.hpp"

#include <algorithm>
#include <map>
#include <set>
#include <utility>

#include "model_internal.hpp"
#include "sde/canonical.hpp"
#include "sde/errors.hpp"

namespace sde {

namespace {

constexpr std::string_view kVocabulary[] = {"bool",  "bytes",  "date",   "float32",
                                            "float64", "int32", "int64",  "json",
                                            "string",  "timestamp", "timestamptz", "uuid"};

bool is_digits(std::string_view text) noexcept {
  if (text.empty()) return false;
  return std::all_of(text.begin(), text.end(), [](char c) { return c >= '0' && c <= '9'; });
}

/// Whether two canonical decimal numerals (no leading zeros) compare `left <= right`, without
/// converting either: a precision is unbounded in the vocabulary, as it is in the reference.
bool numeral_at_most(std::string_view left, std::string_view right) noexcept {
  if (left.size() != right.size()) return left.size() < right.size();
  return left <= right;
}

bool canonical_numeral(std::string_view text) noexcept {
  return is_digits(text) && (text.size() == 1 || text.front() != '0');
}

bool well_formed_decimal(std::string_view type) noexcept {
  constexpr std::string_view prefix = "decimal(";
  if (type.size() <= prefix.size() + 1 || type.substr(0, prefix.size()) != prefix ||
      type.back() != ')') {
    return false;
  }
  const std::string_view body = type.substr(prefix.size(), type.size() - prefix.size() - 1);
  const std::size_t comma = body.find(',');
  if (comma == std::string_view::npos || body.find(',', comma + 1) != std::string_view::npos) {
    return false;
  }
  const std::string_view digits = body.substr(0, comma);
  const std::string_view scale = body.substr(comma + 1);
  if (!canonical_numeral(digits) || !canonical_numeral(scale)) return false;
  if (digits == "0") return false;            // at least one digit of precision
  return numeral_at_most(scale, digits);      // 0 <= scale <= digits
}

std::string in_quotes(std::string_view text) { return "'" + std::string(text) + "'"; }

std::string list_of(const std::vector<std::string>& names) {
  std::string out = "[";
  for (std::size_t i = 0; i < names.size(); ++i) {
    if (i != 0) out += ", ";
    out += in_quotes(names[i]);
  }
  return out + "]";
}

Json string_array(const std::vector<std::string>& names) {
  Json::Array out;
  out.reserve(names.size());
  for (const std::string& name : names) out.emplace_back(name);
  return Json(std::move(out));
}

/// The seven refusals of section 4a, in the order that section writes them - here and not at each
/// front door, because a rule enforced at one of two doors holds for one of two callers.
void refuse_a_declaration_that_is_not_a_model(const std::vector<Entity>& entities,
                                              const std::vector<Relation>& relations) {
  if (entities.empty()) {
    throw DeclarationError("no entities declared, so there is no model to build");
  }
  std::set<std::string> seen;
  for (const Entity& entity : entities) {
    if (entity.fields.empty()) {
      throw DeclarationError(entity.name +
                             " has no fields. An entity that stores nothing cannot be placed, so "
                             "there is nothing for a map to say about it.");
    }
    if (!seen.insert(entity.name).second) {
      throw DeclarationError("two entities are called " + in_quotes(entity.name) +
                             ". Entity names reach the canonical IR and the colocation graph, so "
                             "a duplicate makes 'which entity is this' unanswerable in the "
                             "document whose job is to answer it.");
    }
  }
  std::map<std::string, std::set<std::string>> relations_of;
  for (const Relation& relation : relations) {
    for (const std::string* side : {&relation.from, &relation.to}) {
      if (seen.count(*side) == 0) {
        throw DeclarationError("relation " + in_quotes(relation.name) + " names unknown entity " +
                               in_quotes(*side));
      }
    }
    if (!relations_of[relation.from].insert(relation.name).second) {
      throw DeclarationError(relation.from + "." + relation.name +
                             " is declared twice. Two relations of one name on one entity are two "
                             "edges the colocation graph cannot tell apart.");
    }
  }
  for (const Entity& entity : entities) {
    std::set<std::string> fields;
    std::set<std::string> duplicates;
    for (const Field& field : entity.fields) {
      if (!fields.insert(field.name).second) duplicates.insert(field.name);
    }
    if (!duplicates.empty()) {
      throw DeclarationError(entity.name + " declares the fields " +
                             list_of({duplicates.begin(), duplicates.end()}) +
                             " more than once. The layout would have two columns of one name, "
                             "and the refusal would arrive from the client's engine at CREATE "
                             "TABLE.");
    }
    if (entity.key.empty()) {
      throw DeclarationError(
          entity.name +
          " declares no key. A key is what makes a row addressable, migratable and verifiable - "
          "a backfill compares rows by it - so an entity without one is a group that cannot be "
          "moved, and that is worth knowing when the model is declared rather than in the middle "
          "of a migration. No key is invented for you.");
    }
    std::vector<std::string> missing;
    for (const std::string& part : entity.key) {
      if (fields.count(part) == 0) missing.push_back(part);
    }
    if (!missing.empty()) {
      throw DeclarationError(entity.name + ": key names " + list_of(missing) +
                             ", which are not fields of " + entity.name);
    }
    std::set<std::string> in_key;
    std::set<std::string> repeated;
    for (const std::string& part : entity.key) {
      if (!in_key.insert(part).second) repeated.insert(part);
    }
    if (!repeated.empty()) {
      throw DeclarationError(entity.name + ": key names " +
                             list_of({repeated.begin(), repeated.end()}) +
                             " more than once, which is a composite key with one column in two "
                             "positions.");
    }
    std::vector<std::string> bad_pii;
    for (const std::string& name : entity.pii) {
      if (fields.count(name) == 0) bad_pii.push_back(name);
    }
    if (!bad_pii.empty()) {
      throw DeclarationError(entity.name + ": pii names " + list_of(bad_pii) +
                             ", which are not fields of " + entity.name +
                             ". A pii entry that is not a field silently protects nothing, and "
                             "the exclusion of personal data from a derived copy is meant to be "
                             "readable rather than trusted.");
    }
  }
}

std::vector<Group> colocation_groups(const std::vector<Entity>& entities,
                                     const std::vector<Relation>& relations,
                                     const std::vector<std::vector<std::string>>& atomic) {
  std::map<std::string, std::string> parent;
  for (const Entity& entity : entities) parent[entity.name] = entity.name;
  auto find = [&](std::string name) {
    while (parent[name] != name) {
      parent[name] = parent[parent[name]];
      name = parent[name];
    }
    return name;
  };
  // Attach to the alphabetically smaller root, so a component's representative does not depend on
  // the order the edges were visited in.
  auto unite = [&](const std::string& a, const std::string& b) {
    const std::string root_a = find(a);
    const std::string root_b = find(b);
    if (root_a != root_b) parent[std::max(root_a, root_b)] = std::min(root_a, root_b);
  };
  for (const Relation& relation : relations) unite(relation.from, relation.to);
  for (const auto& group : atomic) {
    for (std::size_t i = 1; i < group.size(); ++i) unite(group.front(), group[i]);
  }
  std::map<std::string, std::vector<std::string>> buckets;
  for (const auto& [name, unused] : parent) buckets[find(name)].push_back(name);
  std::vector<Group> groups;
  for (auto& [root, members] : buckets) {
    std::sort(members.begin(), members.end());
    groups.push_back(Group{members.front(), members});
  }
  std::sort(groups.begin(), groups.end(),
            [](const Group& left, const Group& right) { return left.name < right.name; });
  return groups;
}

std::vector<OperationShape> enumerate_shapes(const std::vector<Entity>& entities,
                                             const std::vector<Relation>& relations,
                                             const std::vector<Group>& groups) {
  auto group_name = [&](const std::string& entity) -> const std::string& {
    for (const Group& group : groups) {
      if (group.contains(entity)) return group.name;
    }
    throw DeclarationError(entity + " is not in any group, which means it is not in the model");
  };
  std::vector<OperationShape> shapes;
  for (const Entity& entity : entities) {
    const std::string& group = group_name(entity.name);
    std::vector<std::string> key = entity.key;
    std::sort(key.begin(), key.end());
    shapes.push_back(OperationShape{group, "point_read", entity.name, key, std::nullopt, {}});
    for (const char* kind : {"write", "bulk_write", "full_scan", "aggregate"}) {
      shapes.push_back(OperationShape{group, kind, entity.name, {}, std::nullopt, {}});
    }
    for (const Field& field : entity.fields) {
      if (is_ordered_type(field.type)) {
        shapes.push_back(
            OperationShape{group, "range_read", entity.name, {field.name}, std::nullopt, {}});
      }
    }
  }
  for (const Relation& relation : relations) {
    shapes.push_back(OperationShape{group_name(relation.from), "relation_walk", relation.from,
                                    {relation.name}, relation.to, {}});
  }
  std::sort(shapes.begin(), shapes.end(), [](const OperationShape& a, const OperationShape& b) {
    const std::string empty;
    return std::tie(a.group, a.entity, a.kind, a.fields, a.target ? *a.target : empty) <
           std::tie(b.group, b.entity, b.kind, b.fields, b.target ? *b.target : empty);
  });
  for (OperationShape& shape : shapes) shape.id = digest16(canonical_bytes(shape.as_ir()));
  return shapes;
}

}  // namespace

namespace types {
std::string decimal(int digits, int scale) {
  return "decimal(" + std::to_string(digits) + "," + std::to_string(scale) + ")";
}
}  // namespace types

bool is_neutral_type(std::string_view type) noexcept {
  for (const std::string_view name : kVocabulary) {
    if (type == name) return true;
  }
  return well_formed_decimal(type);
}

bool is_ordered_type(std::string_view type) noexcept {
  for (const std::string_view prefix :
       {"int32", "int64", "float32", "float64", "decimal", "date", "timestamp"}) {
    if (type.substr(0, prefix.size()) == prefix) return true;
  }
  return false;
}

namespace detail {

void check_type(std::string_view type, const std::string& where) {
  if (is_neutral_type(type)) return;
  // Anything beginning with "decimal" gets the decimal message: the reader's mistake is the missing
  // or misspelled precision, and "not in the vocabulary" would send them looking for another word.
  if (type.substr(0, 7) == "decimal") {
    throw DeclarationError(
        where + ": " + in_quotes(type) +
        " is not a well-formed decimal. The written form is decimal(digits,scale) - precision then "
        "scale, no spaces, both required. No spaces because whitespace inside a type name is "
        "exactly the sort of thing two libraries would disagree about, and both required because "
        "a decimal without precision is not a storable type in any engine we place data in.");
  }
  std::string vocabulary;
  for (const std::string_view name : kVocabulary) {
    if (!vocabulary.empty()) vocabulary += ", ";
    vocabulary += name;
  }
  throw DeclarationError(where + ": " + in_quotes(type) + " is not in the neutral type vocabulary (" +
                         vocabulary + ", decimal(p,s))");
}

}  // namespace detail

const Field* Entity::field(std::string_view field_name) const noexcept {
  for (const Field& candidate : fields) {
    if (candidate.name == field_name) return &candidate;
  }
  return nullptr;
}

bool Group::contains(std::string_view entity) const noexcept {
  return std::find(members.begin(), members.end(), entity) != members.end();
}

bool is_write_kind(std::string_view kind) noexcept {
  return kind == "write" || kind == "bulk_write";
}

Json OperationShape::as_ir() const {
  Json shape = Json::object();
  shape.set("group", group);
  shape.set("kind", kind);
  shape.set("entity", entity);
  shape.set("fields", string_array(fields));
  shape.set("target", target ? Json(*target) : Json(nullptr));
  return shape;
}

const Entity* Model::find_entity(std::string_view name) const noexcept {
  for (const Entity& candidate : entities_) {
    if (candidate.name == name) return &candidate;
  }
  return nullptr;
}

const Entity& Model::entity(std::string_view name) const {
  if (const Entity* found = find_entity(name)) return *found;
  throw DeclarationError("the model declares no entity " + in_quotes(name));
}

const Group& Model::group_of(std::string_view entity) const {
  for (const Group& group : groups_) {
    if (group.contains(entity)) return group;
  }
  throw DeclarationError(std::string(entity) +
                         " is not in any group, which means it is not in the model");
}

const OperationShape* Model::find_shape(std::string_view id) const noexcept {
  for (const OperationShape& shape : shapes_) {
    if (shape.id == id) return &shape;
  }
  return nullptr;
}

Model assemble_model(std::vector<Entity> entities, std::vector<Relation> relations,
                     std::vector<std::vector<std::string>> atomic,
                     std::optional<Json> cost_ceiling) {
  refuse_a_declaration_that_is_not_a_model(entities, relations);

  // Sorted here, by the names that reach the IR, whatever order the caller supplied: hashing
  // rebuilds entities with digests for names, and an IR ordered by the hidden names would encode
  // what hashing exists to hide.
  std::sort(entities.begin(), entities.end(),
            [](const Entity& a, const Entity& b) { return a.name < b.name; });
  for (Entity& entity : entities) {
    std::sort(entity.fields.begin(), entity.fields.end(),
              [](const Field& a, const Field& b) { return a.name < b.name; });
    std::sort(entity.pii.begin(), entity.pii.end());
  }
  std::sort(relations.begin(), relations.end(), [](const Relation& a, const Relation& b) {
    return std::tie(a.from, a.name, a.to) < std::tie(b.from, b.name, b.to);
  });
  for (auto& group : atomic) std::sort(group.begin(), group.end());
  std::sort(atomic.begin(), atomic.end());

  Json::Array ir_entities;
  for (const Entity& entity : entities) {
    Json::Array fields;
    for (const Field& field : entity.fields) {
      Json item = Json::object();
      item.set("name", field.name);
      item.set("type", field.type);
      item.set("nullable", field.nullable);
      fields.push_back(std::move(item));
    }
    Json::Array key;
    for (std::size_t position = 0; position < entity.key.size(); ++position) {
      Json part = Json::object();
      part.set("field", entity.key[position]);
      part.set("position", position);
      key.push_back(std::move(part));
    }
    Json item = Json::object();
    item.set("name", entity.name);
    item.set("fields", Json(std::move(fields)));
    item.set("key", Json(std::move(key)));
    item.set("pii", string_array(entity.pii));
    item.set("residency", entity.residency ? Json(*entity.residency) : Json(nullptr));
    ir_entities.push_back(std::move(item));
  }
  Json::Array ir_relations;
  for (const Relation& relation : relations) {
    Json item = Json::object();
    item.set("name", relation.name);
    item.set("from", relation.from);
    item.set("to", relation.to);
    ir_relations.push_back(std::move(item));
  }
  Json::Array ir_atomic;
  for (const auto& group : atomic) ir_atomic.push_back(string_array(group));

  Json ir = Json::object();
  ir.set("contract", IR_CONTRACT);
  ir.set("entities", Json(std::move(ir_entities)));
  ir.set("relations", Json(std::move(ir_relations)));
  ir.set("atomic", Json(std::move(ir_atomic)));
  ir.set("cost_ceiling", cost_ceiling ? *cost_ceiling : Json(nullptr));

  Model model;
  model.ir_bytes_ = canonical_bytes(ir);
  model.version_ = digest16(model.ir_bytes_);
  model.ir_ = std::move(ir);
  model.groups_ = colocation_groups(entities, relations, atomic);
  model.shapes_ = enumerate_shapes(entities, relations, model.groups_);
  model.entities_ = std::move(entities);
  model.relations_ = std::move(relations);
  model.atomic_ = std::move(atomic);
  model.cost_ceiling_ = std::move(cost_ceiling);
  return model;
}

}  // namespace sde
