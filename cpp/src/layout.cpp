#include "sde/layout.hpp"

#include <algorithm>
#include <iterator>

#include "python_compat.hpp"
#include "sde/errors.hpp"
#include "sde/unicode.hpp"

namespace sde {

namespace {

using detail::python_repr;

struct TypeName {
  std::string_view neutral;
  std::string_view native;
};

constexpr TypeName kPostgres[] = {
    {"bool", "boolean"},      {"int32", "integer"}, {"int64", "bigint"},
    {"float32", "real"},      {"float64", "double precision"},
    {"string", "text"},       {"bytes", "bytea"},   {"uuid", "uuid"},
    {"date", "date"},         {"timestamp", "timestamp"},
    {"timestamptz", "timestamptz"}, {"json", "jsonb"}};

// No `bytes` and no `json`, and the absence is a refusal: the driver hands binary back as hex text,
// and a `String` returns the text where PostgreSQL returns a parsed document - the same field would
// change type when its group moved. Six digits of time, to match PostgreSQL exactly.
constexpr TypeName kClickHouse[] = {
    {"bool", "Bool"},        {"int32", "Int32"},     {"int64", "Int64"},
    {"float32", "Float32"},  {"float64", "Float64"}, {"string", "String"},
    {"uuid", "UUID"},        {"date", "Date32"},     {"timestamp", "DateTime64(6)"},
    {"timestamptz", "DateTime64(6, 'UTC')"}};

// Only the types the fixed shape uses, spelled as the C types the engine's API takes.
constexpr TypeName kOrderbook[] = {
    {"string", "char*"}, {"int32", "uint32_t"}, {"int64", "int64_t"}};

std::string known_dialects() {
  return python_repr(std::vector<std::string>(std::begin(DIALECTS), std::end(DIALECTS)));
}

bool known_dialect(std::string_view dialect) {
  return std::find(std::begin(DIALECTS), std::end(DIALECTS), dialect) != std::end(DIALECTS);
}

template <std::size_t N>
std::optional<std::string_view> lookup(const TypeName (&table)[N], std::string_view neutral) {
  for (const TypeName& entry : table) {
    if (entry.neutral == neutral) return entry.native;
  }
  return std::nullopt;
}

bool is_decimal(std::string_view neutral) { return neutral.rfind("decimal(", 0) == 0; }

void refuse_a_malformed_decimal(std::string_view neutral) {
  if (is_decimal(neutral) && !is_neutral_type(neutral)) {
    throw DeclarationError(python_repr(neutral) +
                           " is not a well-formed decimal, so no engine is asked whether it can "
                           "store one. The model's own validation owns that refusal.");
  }
}

const std::string* column_of(const NeutralColumns& columns, std::string_view name) {
  for (const auto& [column, type] : columns) {
    if (column == name) return &type;
  }
  return nullptr;
}

std::vector<std::string> sorted_names(std::vector<std::string> names) {
  std::sort(names.begin(), names.end());
  return names;
}

}  // namespace

std::string snake_case(std::string_view name) {
  const std::string text = nfc(name);
  auto lower = [](char c) { return c >= 'a' && c <= 'z'; };
  auto upper = [](char c) { return c >= 'A' && c <= 'Z'; };
  auto digit = [](char c) { return c >= '0' && c <= '9'; };
  // `(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])`, on bytes: both classes are ASCII, and in
  // UTF-8 an ASCII byte is never part of another character.
  std::string split;
  split.reserve(text.size() + 4);
  for (std::size_t i = 0; i < text.size(); ++i) {
    if (i > 0) {
      const char before = text[i - 1];
      const char here = text[i];
      const char after = i + 1 < text.size() ? text[i + 1] : '\0';
      if (((lower(before) || digit(before)) && upper(here)) ||
          (upper(before) && upper(here) && lower(after))) {
        split.push_back('_');
      }
    }
    split.push_back(text[i]);
  }
  return to_lower(split);
}

std::string column_type(std::string_view neutral, std::string_view dialect) {
  if (!known_dialect(dialect)) {
    throw DeclarationError("no type table for dialect " + python_repr(dialect) +
                           "; this library knows " + known_dialects() + ".");
  }
  if (is_decimal(neutral)) {
    refuse_a_malformed_decimal(neutral);
    const std::string_view inside = neutral.substr(8, neutral.size() - 9);
    const auto comma = inside.find(',');
    const std::string digits(inside.substr(0, comma));
    const std::string scale(inside.substr(comma + 1));
    if (dialect == "postgres") return "numeric(" + digits + "," + scale + ")";
    if (dialect == "clickhouse") return "Decimal(" + digits + ", " + scale + ")";
    throw DeclarationError("no " + std::string(dialect) + " type for " + python_repr(neutral) +
                           ". This engine has no decimal type at all, so this is a gap in the "
                           "model for this engine rather than in the adapter.");
  }
  std::optional<std::string_view> native;
  if (dialect == "postgres") native = lookup(kPostgres, neutral);
  if (dialect == "clickhouse") native = lookup(kClickHouse, neutral);
  if (dialect == "orderbook") native = lookup(kOrderbook, neutral);
  if (!native) {
    throw DeclarationError("no " + std::string(dialect) + " type for " + python_repr(neutral) +
                           ". Every member of the neutral vocabulary needs one; this is a gap in "
                           "the adapter rather than a problem with the model.");
  }
  return std::string(*native);
}

bool can_store(std::string_view neutral, std::string_view dialect) {
  if (!known_dialect(dialect)) {
    throw DeclarationError("no type table for dialect " + python_repr(dialect) +
                           "; this library knows " + known_dialects() +
                           ". Answering false would say 'that engine cannot store it' about an "
                           "engine this library has never heard of.");
  }
  refuse_a_malformed_decimal(neutral);
  try {
    static_cast<void>(column_type(neutral, dialect));
  } catch (const DeclarationError&) {
    return false;
  }
  return true;
}

bool schema_is_fixed(std::string_view dialect) noexcept { return dialect == "orderbook"; }

std::map<std::string, NeutralColumns> group_columns(const Model& model, const Group& group) {
  std::map<std::string, NeutralColumns> out;
  for (const std::string& member : group.members) {
    const Entity& spec = model.entity(member);
    NeutralColumns columns;
    for (const Field& field : spec.fields) columns.emplace_back(field.name, field.type);
    // Relations are sorted by (from, name, to) in the model, so these come in name order.
    for (const Relation& relation : model.relations()) {
      if (relation.from != member) continue;
      const Entity& target = model.entity(relation.to);
      for (const std::string& key_field : target.key) {
        const std::string name = relation.name + "_" + key_field;
        const std::string& type = target.field(key_field)->type;
        // A dict assignment in the reference: a field of the same name keeps its place and takes
        // the key's type.
        auto existing = std::find_if(columns.begin(), columns.end(),
                                     [&](const auto& column) { return column.first == name; });
        if (existing != columns.end()) {
          existing->second = type;
        } else {
          columns.emplace_back(name, type);
        }
      }
    }
    out.emplace(member, std::move(columns));
  }
  return out;
}

std::map<std::string, std::set<std::string>> group_nullable(const Model& model,
                                                            const Group& group) {
  std::map<std::string, std::set<std::string>> out;
  for (const std::string& member : group.members) {
    std::set<std::string>& fields = out[member];
    for (const Field& field : model.entity(member).fields) {
      if (field.nullable) fields.insert(field.name);
    }
  }
  return out;
}

std::vector<std::string> stored_types(const Model& model, const Group& group) {
  std::set<std::string> types;
  for (const auto& [entity, columns] : group_columns(model, group)) {
    for (const auto& [column, type] : columns) types.insert(type);
  }
  return {types.begin(), types.end()};
}

std::optional<std::string> fixed_schema_mismatch(
    const std::map<std::string, NeutralColumns>& columns, std::string_view dialect,
    const std::map<std::string, std::set<std::string>>& nullable) {
  if (!schema_is_fixed(dialect)) return std::nullopt;

  if (columns.size() != 1) {
    std::vector<std::string> entities;
    for (const auto& [entity, unused] : columns) entities.push_back(entity);
    return "this engine stores one thing, and this group has " + std::to_string(columns.size()) +
           " entities (" + python_repr(entities) +
           "). A colocation group is what shares an engine, so a group of two cannot go "
           "somewhere with room for one.";
  }
  const auto& [entity, declared] = *columns.begin();

  std::vector<std::string> missing;
  std::vector<std::string> wrong;
  std::set<std::string> in_shape;
  for (const auto& [name, kind] : ORDERBOOK_SHAPE) {
    in_shape.emplace(name);
    const std::string* type = column_of(declared, name);
    if (type == nullptr) {
      missing.emplace_back(name);
    } else if (*type != kind) {
      wrong.push_back(std::string(name) + " is declared " + python_repr(*type) +
                      " and this engine stores " + python_repr(kind));
    }
  }
  std::vector<std::string> extra;
  for (const auto& [name, type] : declared) {
    if (in_shape.count(name) == 0) extra.push_back(name);
  }
  const std::set<std::string> allowed_null(std::begin(ORDERBOOK_NULLABLE),
                                           std::end(ORDERBOOK_NULLABLE));
  std::set<std::string> null;
  if (const auto found = nullable.find(entity); found != nullable.end()) {
    for (const std::string& name : found->second) {
      if (in_shape.count(name) != 0) null.insert(name);
    }
  }
  std::vector<std::string> stores_no_null;
  for (const std::string& name : null) {
    if (allowed_null.count(name) == 0) stores_no_null.push_back(name);
  }
  std::vector<std::string> assigned;
  for (const std::string& name : allowed_null) {
    if (column_of(declared, name) != nullptr && null.count(name) == 0) assigned.push_back(name);
  }
  missing = sorted_names(std::move(missing));
  extra = sorted_names(std::move(extra));
  wrong = sorted_names(std::move(wrong));
  if (missing.empty() && extra.empty() && wrong.empty() && stores_no_null.empty() &&
      assigned.empty()) {
    return std::nullopt;
  }

  std::vector<std::string> problems;
  if (!missing.empty()) problems.push_back(entity + " declares no " + python_repr(missing));
  if (!extra.empty()) {
    problems.push_back(entity + " declares " + python_repr(extra) +
                       ", which this engine has nowhere to put");
  }
  if (!wrong.empty()) {
    std::string joined;
    for (std::size_t i = 0; i < wrong.size(); ++i) joined += (i == 0 ? "" : "; ") + wrong[i];
    problems.push_back(joined);
  }
  if (!stores_no_null.empty()) {
    problems.push_back(entity + " declares " + python_repr(stores_no_null) +
                       " nullable, and this engine stores no null there");
  }
  if (!assigned.empty()) {
    problems.push_back(entity + " declares " + python_repr(assigned) +
                       " required, and this engine assigns it: a row is written without it and "
                       "read back with the engine's value");
  }
  std::string expected;
  bool first = true;
  for (const auto& [name, kind] : ORDERBOOK_SHAPE) {
    if (!first) expected += ", ";
    first = false;
    expected += std::string(name) + ": " + std::string(kind);
    if (allowed_null.count(std::string(name)) != 0) expected += ", nullable";
  }
  std::string joined;
  for (std::size_t i = 0; i < problems.size(); ++i) joined += (i == 0 ? "" : ". ") + problems[i];
  return joined +
         ". This engine's schema is fixed in the engine and not chosen by us, so a model either "
         "is that shape or cannot be stored here. The shape is exactly: " +
         expected + ".";
}

PhysicalLayout default_layout(const Model& model, const Group& group, std::string_view dialect) {
  if (!known_dialect(dialect)) {
    throw DeclarationError("no default layout for dialect " + python_repr(dialect) +
                           ". Adding one is an adapter's job, and it has to be added deliberately "
                           "rather than approximated from an existing one - the known dialects "
                           "are " +
                           known_dialects() + ".");
  }
  const std::map<std::string, NeutralColumns> neutral = group_columns(model, group);

  PhysicalLayout layout;
  if (schema_is_fixed(dialect)) {
    if (auto mismatch = fixed_schema_mismatch(neutral, dialect, group_nullable(model, group))) {
      throw DeclarationError(*mismatch);
    }
    const std::string& entity = neutral.begin()->first;
    layout.tables.emplace(entity, ORDERBOOK_TABLE);
    auto& columns = layout.columns[entity];
    for (const auto& [name, kind] : ORDERBOOK_SHAPE) {
      columns.emplace(name, column_type(kind, dialect));
    }
    return layout;
  }

  for (const std::string& member : group.members) {
    layout.tables.emplace(member, snake_case(member));
    auto& columns = layout.columns[member];
    for (const auto& [column, type] : neutral.at(member)) {
      columns[column] = column_type(type, dialect);
    }
    if (dialect == "clickhouse") {
      // A key stays non-nullable, as in a PostgreSQL primary key; any other optional field keeps
      // its nullability in the native type.
      const Entity& spec = model.entity(member);
      for (const Field& field : spec.fields) {
        const bool in_key = std::find(spec.key.begin(), spec.key.end(), field.name) != spec.key.end();
        if (field.nullable && !in_key) columns[field.name] = "Nullable(" + columns[field.name] + ")";
      }
    } else {
      // One index per relation, because a foreign key without one makes every relation walk a
      // sequential scan. Anything more is a decision with a cost, and it is the control plane's.
      for (const Relation& relation : model.relations()) {
        if (relation.from != member) continue;
        Index index;
        index.entity = member;
        index.name = snake_case(member) + "_" + relation.name + "_idx";
        for (const std::string& key_field : model.entity(relation.to).key) {
          index.columns.push_back(relation.name + "_" + key_field);
        }
        layout.indexes.push_back(std::move(index));
      }
    }
  }
  return layout;
}

}  // namespace sde
