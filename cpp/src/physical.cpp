#include <algorithm>
#include <set>

#include "physical_internal.hpp"
#include "python_compat.hpp"
#include "sde/engine.hpp"
#include "sde/errors.hpp"
#include "sde/physical.hpp"

namespace sde {

namespace {

template <std::size_t N>
bool one_of(std::string_view value, const std::string_view (&set)[N]) noexcept {
  return std::find(std::begin(set), std::end(set), value) != std::end(set);
}

template <std::size_t N>
std::string list_repr(const std::string_view (&set)[N]) {
  std::vector<std::string> names(std::begin(set), std::end(set));
  return detail::python_repr(names);
}

std::vector<std::string> sorted_keys(const detail::Tables& tables) {
  std::vector<std::string> keys;
  for (const auto& [name, unused] : tables) keys.push_back(name);
  return keys;  // std::map is already in code point (byte) order
}

}  // namespace

std::string PhysicalFinding::to_string() const {
  return table + ": " + aspect + " is " + found + " and the map declares " + declared;
}

const std::string& PhysicalLayout::table_for(std::string_view entity) const {
  const auto found = tables.find(std::string(entity));
  if (found == tables.end()) {
    throw MapError("the layout has no table for " + detail::python_repr(entity) +
                   ". The map claims to place a group that contains this entity, so this is a "
                   "defect in the map rather than something the library can work around.");
  }
  return found->second;
}

bool is_clickhouse_method(std::string_view method) noexcept {
  return one_of(method, CLICKHOUSE_METHODS);
}

bool is_postgres_method(std::string_view method) noexcept { return one_of(method, POSTGRES_METHODS); }

std::vector<std::string> effective_key(std::string_view where, std::string_view entity,
                                       const std::vector<std::string>& key,
                                       const std::map<std::string, std::vector<std::string>>& key_order) {
  const auto found = key_order.find(std::string(entity));
  if (found == key_order.end()) return key;
  if (auto refusal = detail::key_order_refusal(where, entity, key, found->second)) {
    throw MapError(*refusal);
  }
  return found->second;
}

std::vector<DeclaredTable> declared_tables(
    const PhysicalLayout& layout, const std::map<std::string, std::vector<std::string>>& keys) {
  std::vector<std::pair<std::string, std::string>> tables(layout.tables.begin(),
                                                          layout.tables.end());
  std::stable_sort(tables.begin(), tables.end(),
                   [](const auto& a, const auto& b) { return a.second < b.second; });
  std::vector<const Index*> indexes;
  for (const Index& index : layout.indexes) indexes.push_back(&index);
  std::stable_sort(indexes.begin(), indexes.end(),
                   [](const Index* a, const Index* b) { return a->name < b->name; });
  std::vector<DeclaredTable> out;
  for (const auto& [entity, table] : tables) {
    const std::string where = "table " + detail::python_repr(table);
    const auto keyed = keys.find(entity);
    const std::vector<std::string> key =
        keyed != keys.end() ? keyed->second : std::vector<std::string>{};
    DeclaredTable declared{table, effective_key(where, entity, key, layout.key_order), {}, {}};
    if (const auto partition = layout.partition_by.find(entity);
        partition != layout.partition_by.end()) {
      if (auto refusal = detail::partition_key_refusal(where, entity, key, partition->second)) {
        throw MapError(*refusal);
      }
      declared.partition = std::make_pair(
          std::string(detail::partition_function(partition->second.granularity)),
          partition->second.field);
    }
    for (const Index* index : indexes) {
      if (index->entity != entity) continue;
      const std::string type_full = index->method == "set"
                                        ? "set(" + std::to_string(index->max_rows.value_or(0)) + ")"
                                        : index->method;
      declared.indexes.push_back(
          DeclaredIndex{index->name, index->method, index->columns, index->granularity, type_full});
    }
    out.push_back(std::move(declared));
  }
  return out;
}

namespace detail {

std::optional<std::string> key_order_refusal(std::string_view where, std::string_view entity,
                                             const std::vector<std::string>& key,
                                             const std::vector<std::string>& ordered) {
  std::vector<std::string> a = ordered;
  std::vector<std::string> b = key;
  std::sort(a.begin(), a.end());
  std::sort(b.begin(), b.end());
  if (a == b) return std::nullopt;
  return std::string(where) + ": key_order[" + python_repr(entity) + "] is " +
         python_repr(ordered) + " and the key is " + python_repr(key) +
         ". A key order must be a permutation of the key.";
}

std::optional<std::string> partition_key_refusal(std::string_view where, std::string_view entity,
                                                 const std::vector<std::string>& key,
                                                 const Partition& partition) {
  if (std::find(key.begin(), key.end(), partition.field) != key.end()) return std::nullopt;
  return std::string(where) + ": partition_by[" + python_repr(entity) + "] partitions on " +
         python_repr(std::string_view(partition.field)) + ", outside the key " + python_repr(key) +
         "; duplicates of one key would survive merges in two partitions.";
}

std::string_view partition_function(std::string_view granularity) noexcept {
  if (granularity == "day") return "toDate";
  if (granularity == "month") return "toYYYYMM";
  if (granularity == "year") return "toYear";
  return {};
}

std::map<std::string, std::vector<std::string>> parse_key_order(const Json& raw,
                                                                 const std::string& where,
                                                                 const Tables& tables) {
  if (!raw.is_object()) {
    throw MapError(where + ": key_order maps an entity to a list of its key columns");
  }
  std::map<std::string, const Json*> by_name;
  for (const auto& [entity, columns] : raw.as_object()) by_name.emplace(entity, &columns);
  std::map<std::string, std::vector<std::string>> out;
  for (const auto& [entity, columns] : by_name) {  // in name order (section 8a)
    if (tables.count(entity) == 0) {
      throw MapError(where + ": key_order names " + python_repr(std::string_view(entity)) +
                     ", which has no table in this layout. It has " +
                     python_repr(sorted_keys(tables)) + ".");
    }
    bool valid = columns->is_array() && !columns->as_array().empty();
    if (valid) {
      for (const Json& column : columns->as_array()) {
        valid = valid && column.is_string() && !column.as_string().empty();
      }
    }
    if (!valid) {
      throw MapError(where + ": key_order[" + python_repr(std::string_view(entity)) +
                     "] must be a non-empty list of column names");
    }
    std::vector<std::string> names;
    for (const Json& column : columns->as_array()) names.push_back(column.as_string());
    std::set<std::string> distinct(names.begin(), names.end());
    if (distinct.size() != names.size()) {
      throw MapError(where + ": key_order[" + python_repr(std::string_view(entity)) +
                     "] names a column twice: " + python_repr(names) +
                     ". A key order is a permutation of the key, so each key column appears "
                     "exactly once.");
    }
    out.emplace(entity, std::move(names));
  }
  return out;
}

std::map<std::string, Partition> parse_partition_by(const Json& raw, const std::string& where,
                                                    const Tables& tables) {
  if (!raw.is_object()) {
    throw MapError(where + ": partition_by maps an entity to a {field, granularity} object");
  }
  std::map<std::string, const Json*> by_name;
  for (const auto& [entity, spec] : raw.as_object()) by_name.emplace(entity, &spec);
  std::map<std::string, Partition> out;
  for (const auto& [entity, spec] : by_name) {
    const std::string name = python_repr(std::string_view(entity));
    if (tables.count(entity) == 0) {
      throw MapError(where + ": partition_by names " + name +
                     ", which has no table in this layout. It has " +
                     python_repr(sorted_keys(tables)) + ".");
    }
    bool exact = spec->is_object() && spec->as_object().size() == 2 && spec->contains("field") &&
                 spec->contains("granularity");
    if (!exact) {
      std::string found;
      if (spec->is_object()) {
        std::vector<std::string> keys;
        for (const auto& [key, unused] : spec->as_object()) keys.push_back(key);
        std::sort(keys.begin(), keys.end());
        found = python_repr(keys);
      } else {
        found = python_type_name(*spec);
      }
      throw MapError(where + ": partition_by[" + name +
                     "] must be exactly {\"field\": ..., \"granularity\": ...}; found keys " + found +
                     ". The vocabulary is closed so that nothing in a map is pasted into DDL.");
    }
    const Json& field = *spec->find("field");
    const Json& granularity = *spec->find("granularity");
    if (!field.is_string() || field.as_string().empty()) {
      throw MapError(where + ": partition_by[" + name + "].field must name a column");
    }
    if (!granularity.is_string() || !one_of(granularity.as_string(), GRANULARITIES)) {
      throw MapError(where + ": partition_by[" + name + "].granularity is " +
                     python_repr(granularity) + "; it is one of " + list_repr(GRANULARITIES) + ".");
    }
    out.emplace(entity, Partition{field.as_string(), granularity.as_string()});
  }
  return out;
}

std::vector<Index> parse_indexes(const Json* raw, const std::string& where, const Tables& tables,
                                 const Columns& columns, int contract) {
  std::vector<Index> out;
  if (raw == nullptr || raw->is_null()) return out;
  if (!raw->is_array()) throw MapError(where + ": indexes is a list of index definitions");
  static const std::set<std::string> kLegacy = {"columns", "entity", "name"};
  static const std::set<std::string> kLater = {"granularity", "max_rows", "method"};
  std::set<std::string> names;
  std::size_t position = 0;
  for (const Json& index : raw->as_array()) {
    const std::string at = where + ": indexes[" + std::to_string(position++) + "]";
    if (!index.is_object()) throw MapError(at + " must be an object");
    std::vector<std::string> unknown;
    for (const auto& [key, unused] : index.as_object()) {
      const bool allowed =
          kLegacy.count(key) != 0 || (contract >= PHYSICAL_DESIGN_SINCE && kLater.count(key) != 0);
      if (!allowed) unknown.push_back(key);
    }
    std::sort(unknown.begin(), unknown.end());
    if (!unknown.empty()) {
      std::vector<std::string> later;
      for (const std::string& key : unknown) {
        if (kLater.count(key) != 0) later.push_back(key);
      }
      if (!later.empty()) {
        throw MapError(at + " uses " + python_repr(later) + ", which placement map contract " +
                       std::to_string(PHYSICAL_DESIGN_SINCE) +
                       " introduced, in a document declaring contract " + std::to_string(contract) +
                       ". A library of that contract would ignore the key and create a different "
                       "index from the same document, which is the difference the version exists "
                       "to prevent.");
      }
      throw MapError(at + " has keys this format does not define: " + python_repr(unknown));
    }
    const Json* entity = index.find("entity");
    const Json* name = index.find("name");
    const Json* cols = index.find("columns");
    if (entity == nullptr || !entity->is_string() || tables.count(entity->as_string()) == 0) {
      throw MapError(at + " names entity " + (entity == nullptr ? "None" : python_repr(*entity)) +
                     ", which has no table in this layout. It has " +
                     python_repr(sorted_keys(tables)) + ".");
    }
    if (name == nullptr || !name->is_string() || name->as_string().empty()) {
      throw MapError(at + " needs a non-empty name");
    }
    if (!names.insert(name->as_string()).second) {
      throw MapError(at + " reuses the index name " + python_repr(*name));
    }
    bool valid = cols != nullptr && cols->is_array() && !cols->as_array().empty();
    std::vector<std::string> column_names;
    if (valid) {
      for (const Json& column : cols->as_array()) {
        valid = valid && column.is_string() && !column.as_string().empty();
        if (column.is_string()) column_names.push_back(column.as_string());
      }
      std::set<std::string> distinct(column_names.begin(), column_names.end());
      valid = valid && distinct.size() == column_names.size();
    }
    if (!valid) throw MapError(at + " needs a non-empty list of distinct column names");
    const auto declared = columns.find(entity->as_string());
    if (declared != columns.end() && !declared->second.empty()) {
      std::vector<std::string> missing;
      for (const std::string& column : column_names) {
        if (declared->second.count(column) == 0) missing.push_back(column);
      }
      if (!missing.empty()) {
        throw MapError(at + " indexes " + python_repr(missing) + ", which " +
                       python_repr(*entity) +
                       " does not have in this layout. An index on a missing column is refused by "
                       "the engine at CREATE INDEX, in the client's process, rather than here.");
      }
    }
    Index parsed;
    parsed.entity = entity->as_string();
    parsed.name = name->as_string();
    parsed.columns = column_names;
    if (const Json* method = index.find("method")) {
      if (!method->is_string() || !one_of(method->as_string(), INDEX_METHODS)) {
        throw MapError(at + " has method " + python_repr(*method) + "; the methods are " +
                       list_repr(INDEX_METHODS));
      }
      parsed.method = method->as_string();
      parsed.method_written = true;
    }
    const Json* granularity = index.find("granularity");
    const Json* max_rows = index.find("max_rows");
    if (is_clickhouse_method(parsed.method)) {
      const auto value = granularity != nullptr ? granularity->to_int64() : std::nullopt;
      if (!value || *value < GRANULARITY_MIN || *value > GRANULARITY_MAX) {
        throw MapError(at + ": a " + parsed.method + " index needs an integer granularity from " +
                       std::to_string(GRANULARITY_MIN) + " to " + std::to_string(GRANULARITY_MAX) +
                       "; found " + (granularity == nullptr ? "None" : python_repr(*granularity)));
      }
      if (column_names.size() != 1) {
        throw MapError(at + ": a " + parsed.method + " index summarises exactly one column, found " +
                       python_repr(column_names));
      }
      parsed.granularity = *value;
    } else if (granularity != nullptr) {
      // Presence, not a value: a null here is still a key this method does not have.
      throw MapError(at + ": granularity belongs to data-skipping indexes, not " + parsed.method);
    }
    if (parsed.method == "set") {
      const auto value = max_rows != nullptr ? max_rows->to_int64() : std::nullopt;
      if (!value || *value < SET_ROWS_MIN || *value > SET_ROWS_MAX) {
        throw MapError(at + ": a set index needs an integer max_rows from " +
                       std::to_string(SET_ROWS_MIN) + " to " + std::to_string(SET_ROWS_MAX) +
                       "; found " + (max_rows == nullptr ? "None" : python_repr(*max_rows)) +
                       ". Zero means unlimited in ClickHouse, which is not a choice this format "
                       "offers.");
      }
      parsed.max_rows = *value;
    } else if (max_rows != nullptr) {
      throw MapError(at + ": max_rows belongs to set indexes, not " + parsed.method);
    }
    out.push_back(std::move(parsed));
  }
  return out;
}

void check_against_model(const std::string& where, const std::string& entity,
                         const std::vector<std::string>& key,
                         const std::map<std::string, std::string>& field_types,
                         const std::vector<std::string>* key_order, const Partition* partition) {
  if (key_order != nullptr) {
    std::vector<std::string> a = *key_order;
    std::vector<std::string> b = key;
    std::sort(a.begin(), a.end());
    std::sort(b.begin(), b.end());
    if (a != b) {
      throw MapError(where + ": key_order[" + python_repr(std::string_view(entity)) + "] is " +
                     python_repr(*key_order) + " and the declared key is " + python_repr(key) +
                     ". A key order reorders the key; it cannot add, drop or replace a column, "
                     "because the key is what makes a row the same row in every engine.");
    }
  }
  if (partition != nullptr) {
    const std::string& field = partition->field;
    if (std::find(key.begin(), key.end(), field) == key.end()) {
      throw MapError(where + ": partition_by[" + python_repr(std::string_view(entity)) +
                     "] partitions on " + python_repr(std::string_view(field)) +
                     ", which is not in the key " + python_repr(key) +
                     ". ClickHouse collapses rows of one key only inside one partition, so two "
                     "writes of one key could land in two partitions and stay two rows physically "
                     "for ever - measured: after OPTIMIZE FINAL both remained.");
    }
    const auto kind = field_types.find(field);
    if (kind == field_types.end() || !one_of(kind->second, TEMPORAL_TYPES)) {
      throw MapError(where + ": partition_by[" + python_repr(std::string_view(entity)) +
                     "] partitions on " + python_repr(std::string_view(field)) + ", which is " +
                     (kind == field_types.end() ? std::string("None")
                                                : python_repr(std::string_view(kind->second))) +
                     "; a time partition needs one of " + list_repr(TEMPORAL_TYPES) + ".");
    }
  }
}

namespace {

/// `a, \`b c\`, d` and nothing else: the list itself, without any parentheses around it.
std::optional<std::vector<std::string>> parse_names(std::string_view text) {
  std::vector<std::string> names;
  std::size_t position = 0;
  const auto bare_start = [](char c) {
    return (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || c == '_';
  };
  const auto bare = [&](char c) { return bare_start(c) || (c >= '0' && c <= '9'); };
  while (position < text.size()) {
    if (text[position] == '`') {
      ++position;
      std::string name;
      while (true) {
        if (position >= text.size()) return std::nullopt;  // unterminated
        const char c = text[position];
        if (c == '\\') {
          if (position + 1 >= text.size()) return std::nullopt;  // dangling escape
          name += text[position + 1];
          position += 2;
          continue;
        }
        if (c == '`') {
          ++position;
          break;
        }
        name += c;
        ++position;
      }
      names.push_back(std::move(name));
    } else {
      if (!bare_start(text[position])) return std::nullopt;
      const std::size_t start = position;
      while (position < text.size() && bare(text[position])) ++position;
      names.emplace_back(text.substr(start, position - start));
    }
    if (position < text.size()) {
      // A separator is only ever between two names.
      if (text.substr(position, 2) != ", " || position + 2 >= text.size()) return std::nullopt;
      position += 2;
    }
  }
  return names;
}

/// The text inside one pair of parentheses around the whole of it, or nothing.
std::optional<std::string_view> parenthesised(std::string_view text) {
  if (text.size() < 3 || text.front() != '(' || text.back() != ')') return std::nullopt;
  return text.substr(1, text.size() - 2);
}

}  // namespace

std::optional<std::vector<std::string>> parse_identifier_list(std::string_view text) {
  // From ClickHouse 26.5 the catalogue keeps the parentheses of a single expression written in
  // them: `ORDER BY (id)`, which is how a one-column key is rendered here, reads back `(id)`, where
  // 26.4 and every release before it - 24.8, 25.3, 25.8, 26.3 among them - read `id`; a list of two
  // or more reads `a, b` on all of them (measured). One pair around the whole of a list is the same
  // list, and only a list may be inside it: no name outside backticks holds a parenthesis, so a pair
  // that is not the outermost - `(a) + (b)` - leaves text that is not one.
  return parse_names(parenthesised(text).value_or(text));
}

namespace {

/// `toYYYYMM(\`at\`)` and nothing else: one function of one name, without parentheses around it.
std::optional<PartitionKey> parse_function_key(std::string_view text) {
  // ([A-Za-z][A-Za-z0-9]*)\((.*)\), whole: `.` stops at a newline.
  std::size_t at = 0;
  const auto letter = [](char c) { return (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z'); };
  if (text.empty() || !letter(text[0])) return std::nullopt;
  while (at < text.size() && (letter(text[at]) || (text[at] >= '0' && text[at] <= '9'))) ++at;
  if (at >= text.size() || text[at] != '(' || text.back() != ')' || text.size() < at + 2) {
    return std::nullopt;
  }
  const std::string_view inner = text.substr(at + 1, text.size() - at - 2);
  if (inner.find('\n') != std::string_view::npos) return std::nullopt;
  const auto names = parse_names(inner);
  if (!names || names->size() != 1) return std::nullopt;
  return PartitionKey{std::string(text.substr(0, at)), names->front()};
}

}  // namespace

std::optional<std::optional<PartitionKey>> parse_partition_key(std::string_view text) {
  if (text.empty()) return std::optional<PartitionKey>{};
  // A key written in one pair of parentheses reads back in them from ClickHouse 26.5, as above.
  const auto key = parse_function_key(parenthesised(text).value_or(text));
  if (!key) return std::nullopt;
  return std::optional<PartitionKey>(*key);
}

std::string python_tuple_repr(const std::vector<std::string>& items) {
  std::string out = "(";
  for (std::size_t i = 0; i < items.size(); ++i) {
    if (i != 0) out += ", ";
    out += python_repr(std::string_view(items[i]));
  }
  if (items.size() == 1) out += ",";
  return out + ")";
}

}  // namespace detail

}  // namespace sde
