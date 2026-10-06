#pragma once

/// The logical model (format contract sections 3 to 6): what a client declares, the canonical IR it
/// becomes, the version that IR hashes to, and the colocation groups and operation shapes everything
/// else is keyed on.
///
/// A model is immutable once built, so one model can be shared by every thread of a process.

#include <cstddef>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

#include "sde/json.hpp"

namespace sde {

/// The IR's own contract version - the number inside every IR, and therefore inside every
/// `model_version`. It is not the placement map's number (section 7).
inline constexpr int IR_CONTRACT = 1;

/// The neutral type vocabulary (section 3), as the names the IR records.
namespace types {
inline constexpr std::string_view boolean = "bool";
inline constexpr std::string_view int32 = "int32";
inline constexpr std::string_view int64 = "int64";
inline constexpr std::string_view float32 = "float32";
inline constexpr std::string_view float64 = "float64";
inline constexpr std::string_view string = "string";
inline constexpr std::string_view bytes = "bytes";
inline constexpr std::string_view uuid = "uuid";
inline constexpr std::string_view date = "date";
inline constexpr std::string_view timestamp = "timestamp";
inline constexpr std::string_view timestamptz = "timestamptz";
inline constexpr std::string_view json = "json";
/// `decimal(digits,scale)`, the only parameterised type; both are required and there are no spaces.
[[nodiscard]] std::string decimal(int digits, int scale);
}  // namespace types

/// Whether a type name is in the vocabulary of section 3, `decimal(p,s)` written exactly included.
[[nodiscard]] bool is_neutral_type(std::string_view type) noexcept;
/// Whether a range over a field of this type is enumerated as a shape (section 6).
[[nodiscard]] bool is_ordered_type(std::string_view type) noexcept;

struct Field {
  std::string name;
  std::string type;
  bool nullable = false;
};

struct Entity {
  std::string name;
  std::vector<Field> fields;     ///< sorted by name, in code point order
  std::vector<std::string> key;  ///< in key order, which is the model's and is never sorted
  std::vector<std::string> pii;  ///< sorted
  std::optional<std::string> residency;

  [[nodiscard]] const Field* field(std::string_view field_name) const noexcept;
};

struct Relation {
  std::string name;
  std::string from;
  std::string to;
};

/// A connected component of the colocation graph (section 5), named after its alphabetically
/// first member.
struct Group {
  std::string name;
  std::vector<std::string> members;  ///< sorted

  [[nodiscard]] bool contains(std::string_view entity) const noexcept;
};

/// The kinds of section 6, and the subset that are writes - one definition, because two copies of
/// it in one process is how an operation becomes a write for routing and a read for scoring.
inline constexpr std::string_view SHAPE_KINDS[] = {"point_read", "range_read",    "aggregate",
                                                   "full_scan",  "relation_walk", "write",
                                                   "bulk_write"};
[[nodiscard]] bool is_write_kind(std::string_view kind) noexcept;

/// One kind of operation, without any of its values (section 6). A shape never contains a value:
/// it is assembled from the structure of an operation and never sees the arguments.
struct OperationShape {
  std::string group;
  std::string kind;
  std::string entity;
  std::vector<std::string> fields;
  std::optional<std::string> target;
  std::string id;  ///< digest16 of the canonical shape

  [[nodiscard]] Json as_ir() const;
};

class Model {
 public:
  [[nodiscard]] const std::vector<Entity>& entities() const noexcept { return entities_; }
  [[nodiscard]] const std::vector<Relation>& relations() const noexcept { return relations_; }
  [[nodiscard]] const std::vector<std::vector<std::string>>& atomic() const noexcept {
    return atomic_;
  }
  /// `{"amount": "500.00", "currency": "EUR"}` - the amount is a string, because money is a
  /// decimal and the canonical encoding has no floating point.
  [[nodiscard]] const std::optional<Json>& cost_ceiling() const noexcept { return cost_ceiling_; }

  [[nodiscard]] const Entity* find_entity(std::string_view name) const noexcept;
  /// The entity of this name; `DeclarationError` when the model has none.
  [[nodiscard]] const Entity& entity(std::string_view name) const;

  [[nodiscard]] const Json& ir() const noexcept { return ir_; }
  [[nodiscard]] const std::string& ir_bytes() const noexcept { return ir_bytes_; }
  /// `lowercase_hex(sha256(canonical_bytes(ir)))[:16]` (section 2).
  [[nodiscard]] const std::string& version() const noexcept { return version_; }

  [[nodiscard]] const std::vector<Group>& groups() const noexcept { return groups_; }
  [[nodiscard]] const Group& group_of(std::string_view entity) const;
  /// Every shape the model admits, sorted by `(group, entity, kind, fields, target)`.
  [[nodiscard]] const std::vector<OperationShape>& shapes() const noexcept { return shapes_; }
  [[nodiscard]] const OperationShape* find_shape(std::string_view id) const noexcept;

 private:
  friend Model assemble_model(std::vector<Entity>, std::vector<Relation>,
                              std::vector<std::vector<std::string>>, std::optional<Json>);
  Model() = default;

  std::vector<Entity> entities_;
  std::vector<Relation> relations_;
  std::vector<std::vector<std::string>> atomic_;
  std::optional<Json> cost_ceiling_;
  Json ir_;
  std::string ir_bytes_;
  std::string version_;
  std::vector<Group> groups_;
  std::vector<OperationShape> shapes_;
};

/// The one place a model's IR is assembled, which both front doors - `ModelBuilder` and the neutral
/// loader - go through, so the refusals of section 4a hold for every caller alike. Entities,
/// relations and atomic groups may arrive in any order; sorting is done here, by the names that
/// reach the IR.
[[nodiscard]] Model assemble_model(std::vector<Entity> entities, std::vector<Relation> relations,
                                   std::vector<std::vector<std::string>> atomic,
                                   std::optional<Json> cost_ceiling);

/// A declaration of one entity for `ModelBuilder`.
struct EntityDeclaration {
  std::string name;
  std::vector<Field> fields;
  std::vector<std::string> key;
  std::vector<std::string> pii = {};
  std::optional<std::string> residency = std::nullopt;
};

/// The idiomatic way to declare a model in C++:
///
///     auto model = sde::ModelBuilder{}
///         .entity({"Order", {{"id", "uuid"}, {"account", "string"}}, {"id"}})
///         .entity({"Fill", {{"id", "uuid"}, {"order_id", "uuid"}}, {"id"}})
///         .relation("order", "Fill", "Order")
///         .build();
///
/// `atomic` declares entities that commit together; declarations are merged and transitive, as
/// section 4 requires.
class ModelBuilder {
 public:
  ModelBuilder& entity(EntityDeclaration declaration);
  ModelBuilder& relation(std::string name, std::string from, std::string to);
  ModelBuilder& atomic(std::vector<std::string> members);
  ModelBuilder& cost_ceiling(std::string amount, std::string currency);
  [[nodiscard]] Model build() const;

 private:
  std::vector<EntityDeclaration> entities_;
  std::vector<Relation> relations_;
  std::vector<std::vector<std::string>> atomic_;
  std::optional<Json> cost_ceiling_;
};

/// Builds a model from the neutral declaration of section 4a - the document every conformance
/// vector declares its model in and the one the control plane stores. Every structural defect is a
/// `DeclarationError` naming what is wrong, never an exception from a lookup.
[[nodiscard]] Model load_neutral_model(const Json& declaration);
[[nodiscard]] Model load_neutral_model(std::string_view json_text);

/// The model as that neutral declaration, so a client declaring in C++ never writes their model a
/// second time for the control plane. Not the IR: the two differ exactly where a key is written.
[[nodiscard]] Json neutral_declaration(const Model& model);

}  // namespace sde
