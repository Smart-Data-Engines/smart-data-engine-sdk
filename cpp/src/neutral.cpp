/// The neutral declaration of format contract section 4a: the loader, the way out of a model, and
/// the C++ builder - three front doors onto one `assemble_model`.

#include <algorithm>
#include <map>
#include <set>
#include <utility>

#include "model_internal.hpp"
#include "sde/errors.hpp"
#include "sde/model.hpp"

namespace sde {

namespace {

std::string in_quotes(std::string_view text) { return "'" + std::string(text) + "'"; }

std::string describe(const Json& value) {
  switch (value.kind()) {
    case Json::Kind::null: return "null";
    case Json::Kind::boolean: return value.as_bool() ? "true" : "false";
    case Json::Kind::number: return value.as_number().lexeme;
    case Json::Kind::string: return in_quotes(value.as_string());
    case Json::Kind::array: return "an array";
    case Json::Kind::object: return "an object";
  }
  return "a value";
}

/// The shapes a person can plausibly hand the loader, named rather than crashed on. The first is
/// the reason this exists: the IR and the neutral form differ exactly where a key is written, and
/// handing over the IR deserves its own refusal (errors/036).
void check_shape(const Json& raw, const std::string& name) {
  const Json* fields = raw.find("fields");
  if (fields == nullptr || !fields->is_array()) {
    throw DeclarationError(name + ": 'fields' is a list, and this one is " +
                           (fields == nullptr ? std::string("absent") : describe(*fields)));
  }
  for (const Json& field : fields->as_array()) {
    const Json* field_name = field.find("name");
    if (!field.is_object() || field_name == nullptr || !field_name->is_string()) {
      throw DeclarationError(name + ": a field is an object with a name, not " + describe(field));
    }
    const Json* type = field.find("type");
    if (type == nullptr || !type->is_string()) {
      throw DeclarationError(name + "." + field_name->as_string() + ": a field needs a type");
    }
    const Json* nullable = field.find("nullable");
    if (nullable != nullptr && !nullable->is_bool()) {
      // Python reads this as truthy and TypeScript as `=== true`, so `1` meant two things in two
      // libraries; the only spelling that means one thing everywhere is a boolean.
      throw DeclarationError(name + "." + field_name->as_string() +
                             ": 'nullable' is true or false, not " + describe(*nullable));
    }
  }
  const Json* key = raw.find("key");
  if (key != nullptr && !key->is_null() && !key->is_array()) {
    throw DeclarationError(name + ": 'key' is a list of field names, not " + describe(*key));
  }
  if (key != nullptr && key->is_array()) {
    for (const Json& part : key->as_array()) {
      if (part.is_object() && part.contains("field") && part.contains("position")) {
        throw DeclarationError(
            name + ": 'key' holds " + describe(part) +
            ", which is the IR's key form rather than the neutral one. The IR records a position "
            "because array order is not load-bearing anywhere else in it; a declaration states a "
            "key as a list of field names, in order. If you meant to hand over a model you already "
            "built, sde::neutral_declaration() produces this document from it.");
      }
      if (!part.is_string()) {
        throw DeclarationError(name + ": 'key' names fields as strings, not " + describe(part));
      }
    }
  }
  const Json* pii = raw.find("pii");
  if (pii != nullptr && !pii->is_null()) {
    bool strings = pii->is_array();
    if (strings) {
      for (const Json& item : pii->as_array()) strings = strings && item.is_string();
    }
    if (!strings) throw DeclarationError(name + ": 'pii' is a list of field names, not " + describe(*pii));
  }
  const Json* residency = raw.find("residency");
  if (residency != nullptr && !residency->is_null() && !residency->is_string()) {
    throw DeclarationError(name + ": 'residency' is a jurisdiction's name or null, not " +
                           describe(*residency));
  }
}

std::vector<std::string> strings_of(const Json* list) {
  std::vector<std::string> out;
  if (list == nullptr || !list->is_array()) return out;
  for (const Json& item : list->as_array()) out.push_back(item.as_string());
  return out;
}

std::optional<Json> checked_cost_ceiling(const Json* raw) {
  if (raw == nullptr || raw->is_null()) return std::nullopt;
  const Json* amount = raw->is_object() ? raw->find("amount") : nullptr;
  const Json* currency = raw->is_object() ? raw->find("currency") : nullptr;
  if (amount == nullptr || currency == nullptr || !amount->is_string() || !currency->is_string() ||
      raw->as_object().size() != 2) {
    throw DeclarationError(
        "cost_ceiling is {\"amount\": \"500.00\", \"currency\": \"EUR\"} - exactly those two keys, "
        "with the amount as a string, because money is a decimal and the canonical encoding has no "
        "floating point; this one is " + describe(*raw));
  }
  return *raw;
}

/// Atomic groups as declared in a neutral document: names checked, each group's members distinct,
/// at least two of them, and no entity in two groups. Section 4 says the IR's groups are merged and
/// transitive; a document whose groups overlap is refused rather than merged, so that no library
/// writes such a document into an IR unmerged and none has to guess how another merged it.
std::vector<std::vector<std::string>> checked_atomic(const Json* raw,
                                                     const std::set<std::string>& names) {
  std::vector<std::vector<std::string>> groups;
  if (raw == nullptr || raw->is_null()) return groups;
  if (!raw->is_array()) {
    throw DeclarationError("'atomic' is a list of groups of entity names, not " + describe(*raw));
  }
  std::map<std::string, std::size_t> placed;
  for (const Json& group : raw->as_array()) {
    bool strings = group.is_array();
    if (strings) {
      for (const Json& item : group.as_array()) strings = strings && item.is_string();
    }
    if (!strings) {
      throw DeclarationError("an atomic group is a list of entity names, not " + describe(group));
    }
    std::vector<std::string> members = strings_of(&group);
    std::sort(members.begin(), members.end());
    std::vector<std::string> unknown;
    for (const std::string& member : members) {
      if (names.count(member) == 0) unknown.push_back(member);
    }
    if (!unknown.empty()) {
      std::string listed;
      for (const std::string& name : unknown) listed += (listed.empty() ? "" : ", ") + in_quotes(name);
      throw DeclarationError("atomic group names unknown entities [" + listed + "]");
    }
    if (std::adjacent_find(members.begin(), members.end()) != members.end() || members.size() < 2) {
      throw DeclarationError(
          "an atomic group names two or more distinct entities; one that names fewer, or one twice, "
          "says nothing a placement could act on");
    }
    for (const std::string& member : members) {
      if (!placed.emplace(member, groups.size()).second) {
        throw DeclarationError(
            "atomic groups overlap on " + in_quotes(member) +
            ". Atomicity is transitive, so overlapping groups are one group: declare it once, with "
            "every member, as a library's own neutral declaration does");
      }
    }
    groups.push_back(std::move(members));
  }
  return groups;
}

}  // namespace

Model load_neutral_model(const Json& data) {
  if (!data.is_object()) {
    throw DeclarationError("a neutral model declaration is an object with an 'entities' list");
  }
  const Json* raw_entities = data.find("entities");
  if (raw_entities == nullptr || !raw_entities->is_array()) {
    throw DeclarationError("a neutral model declaration needs an 'entities' list");
  }
  std::vector<Entity> entities;
  for (const Json& raw : raw_entities->as_array()) {
    if (!raw.is_object()) {
      throw DeclarationError("an entity is an object, and this one is " + describe(raw));
    }
    const Json* raw_name = raw.find("name");
    if (raw_name == nullptr || !raw_name->is_string() || raw_name->as_string().empty()) {
      throw DeclarationError("an entity needs a name, and this one has " +
                             (raw_name == nullptr ? std::string("none") : describe(*raw_name)));
    }
    const std::string& name = raw_name->as_string();
    check_shape(raw, name);
    Entity entity;
    entity.name = name;
    for (const Json& field : raw.find("fields")->as_array()) {
      const std::string& field_name = field.find("name")->as_string();
      const std::string& type = field.find("type")->as_string();
      detail::check_type(type, name + "." + field_name);
      const Json* nullable = field.find("nullable");
      entity.fields.push_back(Field{field_name, type, nullable != nullptr && nullable->as_bool()});
    }
    // No default for the key: an absent or empty key stays empty and assemble_model refuses it.
    entity.key = strings_of(raw.find("key"));
    entity.pii = strings_of(raw.find("pii"));
    if (const Json* residency = raw.find("residency"); residency != nullptr && residency->is_string()) {
      entity.residency = residency->as_string();
    }
    entities.push_back(std::move(entity));
  }

  std::set<std::string> names;
  for (const Entity& entity : entities) names.insert(entity.name);

  std::vector<Relation> relations;
  if (const Json* raw_relations = data.find("relations");
      raw_relations != nullptr && !raw_relations->is_null()) {
    if (!raw_relations->is_array()) {
      throw DeclarationError("'relations' is a list of {name, from, to}, not " +
                             describe(*raw_relations));
    }
    for (const Json& raw : raw_relations->as_array()) {
      const Json* name = raw.find("name");
      const Json* from = raw.find("from");
      const Json* to = raw.find("to");
      if (!raw.is_object() || name == nullptr || from == nullptr || to == nullptr ||
          !name->is_string() || !from->is_string() || !to->is_string()) {
        throw DeclarationError("a relation is {\"name\", \"from\", \"to\"}, each a string, not " +
                               describe(raw));
      }
      for (const Json* side : {from, to}) {
        if (names.count(side->as_string()) == 0) {
          throw DeclarationError("relation " + in_quotes(name->as_string()) +
                                 " names unknown entity " + in_quotes(side->as_string()));
        }
      }
      relations.push_back(Relation{name->as_string(), from->as_string(), to->as_string()});
    }
  }

  auto atomic = checked_atomic(data.find("atomic"), names);
  auto cost_ceiling = checked_cost_ceiling(data.find("cost_ceiling"));
  return assemble_model(std::move(entities), std::move(relations), std::move(atomic),
                        std::move(cost_ceiling));
}

Model load_neutral_model(std::string_view json_text) {
  Json document;
  try {
    document = parse_json(json_text);
  } catch (const JsonError& error) {
    throw DeclarationError(std::string("a neutral model declaration is JSON: ") + error.what());
  }
  return load_neutral_model(document);
}

Json neutral_declaration(const Model& model) {
  Json::Array entities;
  for (const Entity& spec : model.entities()) {
    Json::Array fields;
    for (const Field& field : spec.fields) {
      Json item = Json::object();
      item.set("name", field.name);
      item.set("type", field.type);
      if (field.nullable) item.set("nullable", true);
      fields.push_back(std::move(item));
    }
    Json entity = Json::object();
    entity.set("name", spec.name);
    entity.set("fields", Json(std::move(fields)));
    Json::Array key;
    for (const std::string& part : spec.key) key.emplace_back(part);
    entity.set("key", Json(std::move(key)));
    if (!spec.pii.empty()) {
      Json::Array pii;
      for (const std::string& name : spec.pii) pii.emplace_back(name);
      entity.set("pii", Json(std::move(pii)));
    }
    if (spec.residency) entity.set("residency", *spec.residency);
    entities.push_back(std::move(entity));
  }
  Json document = Json::object();
  document.set("entities", Json(std::move(entities)));
  if (!model.relations().empty()) {
    Json::Array relations;
    for (const Relation& relation : model.relations()) {
      Json item = Json::object();
      item.set("name", relation.name);
      item.set("from", relation.from);
      item.set("to", relation.to);
      relations.push_back(std::move(item));
    }
    document.set("relations", Json(std::move(relations)));
  }
  if (!model.atomic().empty()) {
    Json::Array atomic;
    for (const auto& group : model.atomic()) {
      Json::Array members;
      for (const std::string& member : group) members.emplace_back(member);
      atomic.emplace_back(std::move(members));
    }
    document.set("atomic", Json(std::move(atomic)));
  }
  if (model.cost_ceiling()) document.set("cost_ceiling", *model.cost_ceiling());
  return document;
}

ModelBuilder& ModelBuilder::entity(EntityDeclaration declaration) {
  entities_.push_back(std::move(declaration));
  return *this;
}

ModelBuilder& ModelBuilder::relation(std::string name, std::string from, std::string to) {
  relations_.push_back(Relation{std::move(name), std::move(from), std::move(to)});
  return *this;
}

ModelBuilder& ModelBuilder::atomic(std::vector<std::string> members) {
  atomic_.push_back(std::move(members));
  return *this;
}

ModelBuilder& ModelBuilder::cost_ceiling(std::string amount, std::string currency) {
  Json ceiling = Json::object();
  ceiling.set("amount", std::move(amount));
  ceiling.set("currency", std::move(currency));
  cost_ceiling_ = std::move(ceiling);
  return *this;
}

Model ModelBuilder::build() const {
  std::set<std::string> names;
  std::vector<Entity> entities;
  for (const EntityDeclaration& declaration : entities_) {
    for (const Field& field : declaration.fields) {
      detail::check_type(field.type, declaration.name + "." + field.name);
    }
    names.insert(declaration.name);
    entities.push_back(Entity{declaration.name, declaration.fields, declaration.key,
                              declaration.pii, declaration.residency});
  }
  for (const Relation& relation : relations_) {
    for (const std::string* side : {&relation.from, &relation.to}) {
      if (names.count(*side) == 0) {
        throw DeclarationError("relation " + in_quotes(relation.name) + " names unknown entity " +
                               in_quotes(*side));
      }
    }
  }
  // Atomicity is symmetric and transitive (section 4): declarations that share an entity are one
  // group, merged here so that declaring a pair twice, or in either order, builds one model.
  std::map<std::string, std::string> parent;
  auto find = [&](std::string name) {
    while (parent[name] != name) {
      parent[name] = parent[parent[name]];
      name = parent[name];
    }
    return name;
  };
  for (const auto& group : atomic_) {
    for (const std::string& member : group) {
      if (names.count(member) == 0) {
        throw DeclarationError("atomic group names unknown entities [" + in_quotes(member) + "]");
      }
      parent.emplace(member, member);
    }
    if (group.size() < 2) {
      throw DeclarationError(
          "an atomic group names two or more distinct entities; one that names fewer says nothing "
          "a placement could act on");
    }
    for (std::size_t i = 1; i < group.size(); ++i) {
      const std::string a = find(group.front());
      const std::string b = find(group[i]);
      if (a != b) parent[std::max(a, b)] = std::min(a, b);
    }
  }
  std::map<std::string, std::vector<std::string>> merged;
  for (const auto& [member, unused] : parent) merged[find(member)].push_back(member);
  std::vector<std::vector<std::string>> atomic;
  for (auto& [root, members] : merged) atomic.push_back(std::move(members));
  return assemble_model(std::move(entities), relations_, std::move(atomic), cost_ceiling_);
}

}  // namespace sde
