#include "sde/hashing.hpp"

#include <algorithm>
#include <fstream>
#include <iterator>
#include <set>

#include "crypto.hpp"
#include "sde/errors.hpp"
#include "sde/unicode.hpp"

namespace sde {

namespace {

std::string in_quotes(std::string_view text) { return "'" + std::string(text) + "'"; }

}  // namespace

const std::string& NameMap::entity(std::string_view name) const {
  const auto found = entities.find(std::string(name));
  if (found == entities.end()) {
    throw DeclarationError(in_quotes(name) +
                           " is not in this model, so it has no hashed name. Either it was never "
                           "declared, or the model was rebuilt without it.");
  }
  return found->second;
}

const std::string& NameMap::field(std::string_view entity_name, std::string_view name) const {
  const auto of_entity = fields.find(std::string(entity_name));
  if (of_entity != fields.end()) {
    const auto found = of_entity->second.find(std::string(name));
    if (found != of_entity->second.end()) return found->second;
  }
  throw DeclarationError(std::string(entity_name) + "." + std::string(name) +
                         " is not a field of this model, so it has no hashed name");
}

const std::string& NameMap::relation(std::string_view entity_name, std::string_view name) const {
  const auto of_entity = relations.find(std::string(entity_name));
  if (of_entity != relations.end()) {
    const auto found = of_entity->second.find(std::string(name));
    if (found != of_entity->second.end()) return found->second;
  }
  throw DeclarationError(std::string(entity_name) + "." + std::string(name) +
                         " is not a relation of this model, so it has no hashed name");
}

std::string hashed_name(std::string_view salt, std::string_view prefix,
                        std::initializer_list<std::string_view> parts) {
  // NFC before the HMAC, and U+0000 between the parts: ("User", "id") must not collide with
  // ("Use", "rid"), and a name written in two normal forms must give one digest.
  std::string message;
  bool first = true;
  for (const std::string_view part : parts) {
    if (!first) message.push_back('\0');
    first = false;
    message += nfc(part);
  }
  const auto digest = detail::hmac_sha256(salt, message);
  return std::string(prefix) + detail::to_hex(digest).substr(0, DIGEST_CHARS);
}

std::pair<Model, NameMap> hash_identifiers(const Model& model, std::string_view salt) {
  if (salt.size() < 16) throw DeclarationError("the salt must be at least 16 bytes");
  NameMap names;
  std::set<std::string> taken;
  for (const Entity& entity : model.entities()) {
    std::string hashed = hashed_name(salt, "e_", {entity.name});
    if (!taken.insert(hashed).second) {
      std::string clash;
      for (const auto& [name, digest] : names.entities) {
        if (digest == hashed) clash = name;
      }
      throw DeclarationError(in_quotes(entity.name) + " and " + in_quotes(clash) +
                             " hash to the same name. Refused rather than merged: a model with "
                             "one entity where there were two would place both in one engine and "
                             "write both into one table. Change the salt.");
    }
    names.entities.emplace(entity.name, std::move(hashed));
  }

  std::vector<Entity> entities;
  for (const Entity& entity : model.entities()) {
    auto& mapping = names.fields[entity.name];
    std::set<std::string> digests;
    for (const Field& field : entity.fields) {
      std::string hashed = hashed_name(salt, "f_", {entity.name, field.name});
      digests.insert(hashed);
      mapping.emplace(field.name, std::move(hashed));
    }
    if (digests.size() != mapping.size()) {
      throw DeclarationError("two fields of " + in_quotes(entity.name) +
                             " hash to the same name. Refused rather than merged. Change the salt.");
    }
    Entity hashed;
    hashed.name = names.entities.at(entity.name);
    for (const Field& field : entity.fields) {
      hashed.fields.push_back(Field{mapping.at(field.name), field.type, field.nullable});
    }
    for (const std::string& part : entity.key) hashed.key.push_back(mapping.at(part));
    for (const std::string& name : entity.pii) hashed.pii.push_back(mapping.at(name));
    // Residency is a jurisdiction and a hard placement constraint, not an identifier: a hashed
    // constraint would be unenforceable.
    hashed.residency = entity.residency;
    entities.push_back(std::move(hashed));
  }

  std::vector<Relation> relations;
  for (const Relation& relation : model.relations()) {
    std::string hashed = hashed_name(salt, "r_", {relation.from, relation.name});
    names.relations[relation.from][relation.name] = hashed;
    relations.push_back(Relation{std::move(hashed), names.entities.at(relation.from),
                                 names.entities.at(relation.to)});
  }

  std::vector<std::vector<std::string>> atomic;
  for (const auto& group : model.atomic()) {
    std::vector<std::string> members;
    for (const std::string& member : group) members.push_back(names.entities.at(member));
    atomic.push_back(std::move(members));
  }

  // The cost ceiling is a number and a currency, not an identifier.
  Model hashed_model =
      assemble_model(std::move(entities), std::move(relations), std::move(atomic), model.cost_ceiling());
  return {std::move(hashed_model), std::move(names)};
}

std::string read_salt_file(const std::filesystem::path& path) {
  std::ifstream stream(path, std::ios::binary);
  if (!stream) throw DeclarationError("cannot read the salt file " + path.string());
  return std::string(std::istreambuf_iterator<char>(stream), std::istreambuf_iterator<char>());
}

}  // namespace sde
