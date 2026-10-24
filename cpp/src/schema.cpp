#include "sde/schema.hpp"

#include <algorithm>
#include <string>

#include "physical_internal.hpp"
#include "python_compat.hpp"
#include "sde/errors.hpp"
#include "sde/layout.hpp"

namespace sde {

namespace {

using detail::python_repr;
using Keys = std::map<std::string, std::vector<std::string>>;

/// The dialects this library renders DDL for, sorted: the list every refusal names.
const std::vector<std::string>& rendered_dialects() {
  static const std::vector<std::string> dialects(std::begin(DIALECTS), std::end(DIALECTS));
  return dialects;
}

bool known_dialect(std::string_view dialect) {
  return std::find(std::begin(DIALECTS), std::end(DIALECTS), dialect) != std::end(DIALECTS);
}

/// PostgreSQL's spelling of the neutral `string`, the type that takes the reads' collation.
const std::string& postgres_text() {
  static const std::string text = column_type("string", "postgres");
  return text;
}

struct ColumnsAndKey {
  const std::map<std::string, std::string>& columns;
  std::vector<std::string> key;
};

ColumnsAndKey columns_and_key(const PhysicalLayout& layout, const std::string& entity,
                              const Keys& keys) {
  const auto columns = layout.columns.find(entity);
  if (columns == layout.columns.end() || columns->second.empty()) {
    throw EngineError("the layout gives no columns for " + python_repr(entity));
  }
  const auto key = keys.find(entity);
  if (key == keys.end() || key->second.empty()) {
    throw EngineError("no key for " + python_repr(entity) +
                      "; a table without one cannot be addressed");
  }
  return ColumnsAndKey{columns->second, key->second};
}

/// The key in physical order, refused as an engine's error: by the time a layout is rendered it was
/// loaded, so a bad order here is a layout built by hand.
std::vector<std::string> ordered_key(const std::string& where, const std::string& entity,
                                     const std::vector<std::string>& key,
                                     const PhysicalLayout& layout) {
  const auto found = layout.key_order.find(entity);
  if (found == layout.key_order.end()) return key;
  if (auto refusal = detail::key_order_refusal(where, entity, key, found->second)) {
    throw EngineError(*refusal);
  }
  return found->second;
}

/// Indexes in code point order of their name, whatever order the document gave; stable, so two of
/// one name keep the document's order between them.
std::vector<const Index*> sorted_indexes(const PhysicalLayout& layout) {
  std::vector<const Index*> out;
  out.reserve(layout.indexes.size());
  for (const Index& index : layout.indexes) out.push_back(&index);
  std::stable_sort(out.begin(), out.end(),
                   [](const Index* left, const Index* right) { return left->name < right->name; });
  return out;
}

std::string join(const std::vector<std::string>& parts, std::string_view separator) {
  std::string out;
  for (std::size_t i = 0; i < parts.size(); ++i) {
    if (i > 0) out += separator;
    out += parts[i];
  }
  return out;
}

std::vector<std::string> methods(const auto& vocabulary) {
  return std::vector<std::string>(std::begin(vocabulary), std::end(vocabulary));
}

std::vector<std::string> postgres_statements(const PhysicalLayout& layout, const Keys& keys) {
  if (!layout.partition_by.empty()) {
    std::vector<std::string> partitioned;
    for (const auto& [entity, unused] : layout.partition_by) partitioned.push_back(entity);
    throw EngineError(
        "the layout partitions " + python_repr(partitioned) +
        " and PostgreSQL partitioning is not rendered by this library: every partition would have "
        "to exist before a row arrived, which is a lifecycle this product does not manage. An "
        "unpartitioned table under a map that says otherwise would be a silent drop, so this "
        "refuses.");
  }
  std::vector<std::string> statements;
  for (const auto& [entity, table] : layout.tables) {
    const ColumnsAndKey found = columns_and_key(layout, entity, keys);
    const std::vector<std::string> ordered =
        ordered_key("table " + python_repr(table), entity, found.key, layout);
    std::vector<std::string> definitions;
    for (const auto& [column, type] : found.columns) {
      std::string definition = quote_ansi(column) + " " + type;
      if (type == postgres_text()) definition += " " + std::string(POSTGRES_TEXT_COLLATION);
      definitions.push_back(std::move(definition));
    }
    std::vector<std::string> primary;
    for (const std::string& column : ordered) primary.push_back(quote_ansi(column));
    statements.push_back("CREATE TABLE IF NOT EXISTS " + quote_ansi(table) + " (" +
                         join(definitions, ", ") + ", PRIMARY KEY (" + join(primary, ", ") + "))");
  }
  for (const Index* index : sorted_indexes(layout)) {
    const auto table = layout.tables.find(index->entity);
    if (table == layout.tables.end()) continue;
    static const std::map<std::string, std::string> none;
    const auto columns = layout.columns.find(index->entity);
    statements.push_back("CREATE INDEX IF NOT EXISTS " +
                         postgres_index_target(*index, table->second,
                                               columns == layout.columns.end() ? none
                                                                               : columns->second));
  }
  return statements;
}

std::vector<std::string> clickhouse_statements(const PhysicalLayout& layout, const Keys& keys) {
  std::size_t legacy = 0;
  for (const Index& index : layout.indexes) {
    if (!is_clickhouse_method(index.method)) ++legacy;
  }
  if (legacy > 0) {
    throw EngineError("the layout carries " + std::to_string(legacy) +
                      " index definitions and this engine has no B-tree to put them in. A "
                      "ClickHouse index is a data-skipping index with a type and a granularity, so "
                      "this map was built for another dialect.");
  }
  std::vector<std::string> statements;
  for (const auto& [entity, table] : layout.tables) {
    const ColumnsAndKey found = columns_and_key(layout, entity, keys);
    std::vector<std::string> missing;
    for (const std::string& column : found.key) {
      if (found.columns.count(column) == 0) missing.push_back(column);
    }
    if (!missing.empty()) {
      throw EngineError("the key of " + python_repr(entity) +
                        " names columns the layout does not have: " + python_repr(missing) +
                        ". In ClickHouse the key becomes ORDER BY, so this would produce a table "
                        "that cannot be created rather than one with a missing constraint.");
    }
    const std::string where = "table " + python_repr(table);
    // ORDER BY is the key, in declared order unless the layout gives a physical one. The order is
    // positional - it decides which prefixes of the key prune granules - so it is never sorted.
    const std::vector<std::string> ordered = ordered_key(where, entity, found.key, layout);
    const Partition* partition = nullptr;
    if (const auto it = layout.partition_by.find(entity); it != layout.partition_by.end()) {
      partition = &it->second;
      if (auto refusal = detail::partition_key_refusal(where, entity, found.key, *partition)) {
        throw EngineError(*refusal);
      }
    }
    std::vector<std::string> parts;
    for (const auto& [column, type] : found.columns) parts.push_back(quote_backtick(column) + " " + type);
    // Indexes inline: `CREATE TABLE IF NOT EXISTS` never adds one to an existing table, and a
    // separate ALTER would be a mutation over every existing part.
    for (const Index* index : sorted_indexes(layout)) {
      if (index->entity == entity) parts.push_back(clickhouse_index_clause(*index));
    }
    std::string partition_clause;
    if (partition != nullptr) {
      const std::string_view function = detail::partition_function(partition->granularity);
      if (function.empty()) {
        throw EngineError(where + ": partition_by[" + python_repr(entity) + "].granularity is " +
                          python_repr(partition->granularity) + ", outside the vocabulary");
      }
      partition_clause =
          "PARTITION BY " + std::string(function) + "(" + quote_backtick(partition->field) + ") ";
    }
    std::vector<std::string> order;
    for (const std::string& column : ordered) order.push_back(quote_backtick(column));
    statements.push_back("CREATE TABLE IF NOT EXISTS " + quote_backtick(table) + " (" +
                         join(parts, ", ") + ") ENGINE = ReplacingMergeTree " + partition_clause +
                         "ORDER BY (" + join(order, ", ") + ")");
  }
  return statements;
}

/// No DDL: the engine's schema is not ours to create. An empty list rather than a refusal - running
/// nothing is the correct action for a caller preparing such an engine - unless the layout carries a
/// physical design, which this engine could not honour.
std::vector<std::string> fixed_statements(const PhysicalLayout& layout) {
  if (!layout.key_order.empty() || !layout.partition_by.empty() || !layout.indexes.empty()) {
    throw EngineError(
        "this engine's schema is fixed in its own source, so a physical design - key order, "
        "partition or indexes - cannot be applied to it. A map that declared one and got the fixed "
        "table anyway would be a storage decision silently dropped.");
  }
  return {};
}

}  // namespace

std::string quote_ansi(std::string_view identifier) {
  std::string out = "\"";
  for (const char c : identifier) {
    if (c == '"') out += '"';
    out += c;
  }
  out += '"';
  return out;
}

std::string quote_backtick(std::string_view identifier) {
  std::string out = "`";
  for (const char c : identifier) {
    if (c == '\\' || c == '`') out += '\\';
    out += c;
  }
  out += '`';
  return out;
}

std::string quote_identifier(std::string_view dialect, std::string_view identifier) {
  if (dialect == "postgres") return quote_ansi(identifier);
  if (dialect == "clickhouse") return quote_backtick(identifier);
  throw EngineError("no identifier quoting for dialect " + python_repr(dialect) +
                    ": this library sends identifiers only to postgres and clickhouse, and a "
                    "fixed-schema engine takes none from it");
}

bool schema_is_fixed(std::string_view dialect) {
  if (!known_dialect(dialect)) {
    throw EngineError("unknown dialect " + python_repr(dialect) + "; this library renders " +
                      python_repr(rendered_dialects()) +
                      ". Answering False would say 'that engine takes DDL from us' about an engine "
                      "it has never heard of.");
  }
  return dialect == "orderbook";
}

std::vector<std::string> schema_statements(const PhysicalLayout& layout, const Keys& keys,
                                           std::string_view dialect) {
  if (dialect == "postgres") return postgres_statements(layout, keys);
  if (dialect == "clickhouse") return clickhouse_statements(layout, keys);
  if (dialect == "orderbook") return fixed_statements(layout);
  throw EngineError("no DDL for dialect " + python_repr(dialect) + "; this library renders " +
                    python_repr(rendered_dialects()) +
                    ". Refusing rather than falling back to ANSI: a statement that looks right on "
                    "the wrong engine creates a table with the wrong storage semantics.");
}

std::string postgres_index_target(const Index& index, std::string_view table,
                                  const std::map<std::string, std::string>& columns) {
  if (!is_postgres_method(index.method)) {
    throw EngineError("index " + python_repr(index.name) + " is a " + index.method +
                      " index, which is a ClickHouse data-skipping index; PostgreSQL has " +
                      python_repr(methods(POSTGRES_METHODS)) +
                      ". This map was designed for another dialect.");
  }
  std::vector<std::string> indexed;
  for (const std::string& column : index.columns) {
    std::string part = quote_ansi(column);
    const auto type = columns.find(column);
    if (type != columns.end() && type->second == postgres_text()) {
      part += " " + std::string(POSTGRES_TEXT_COLLATION);
    }
    indexed.push_back(std::move(part));
  }
  // A B-tree has no USING clause, as no index did before map contract 5 named a method.
  const std::string with = index.method == "btree" ? "" : "USING " + index.method + " ";
  return quote_ansi(index.name) + " ON " + quote_ansi(table) + " " + with + "(" +
         join(indexed, ", ") + ")";
}

std::string clickhouse_index_clause(const Index& index) {
  if (!is_clickhouse_method(index.method)) {
    throw EngineError("index " + python_repr(index.name) +
                      " is not a ClickHouse data-skipping index; this engine has " +
                      python_repr(methods(CLICKHOUSE_METHODS)));
  }
  if (index.columns.size() != 1) {
    throw EngineError("the data-skipping index " + python_repr(index.name) +
                      " must summarise exactly one column");
  }
  // The loader requires both numbers for these methods; a layout built by hand may not have them.
  if (!index.granularity || (index.method == "set" && !index.max_rows)) {
    throw EngineError("the data-skipping index " + python_repr(index.name) + " has no " +
                      (index.granularity ? "max_rows" : "granularity"));
  }
  const std::string type =
      index.method == "set" ? "set(" + std::to_string(*index.max_rows) + ")" : index.method;
  return "INDEX " + quote_backtick(index.name) + " " + quote_backtick(index.columns.front()) +
         " TYPE " + type + " GRANULARITY " + std::to_string(*index.granularity);
}

CompatibilityViews compatibility_views(const PhysicalLayout& layout,
                                       const std::map<std::string, std::string>& was,
                                       std::string_view dialect) {
  if (!known_dialect(dialect)) {
    throw EngineError("no compatibility view for dialect " + python_repr(dialect) +
                      "; this library renders " + python_repr(rendered_dialects()) + ".");
  }
  CompatibilityViews out;
  if (schema_is_fixed(dialect)) {
    for (const auto& [entity, table] : layout.tables) {
      const auto old = was.find(entity);
      out.not_possible.emplace_back(
          entity, std::string(dialect) +
                      " imposes its own schema and accepts no DDL from this library, so there is "
                      "nowhere to put a view. Its table is " +
                      python_repr(table) + " and the old name was " +
                      (old == was.end() ? std::string("None") : python_repr(old->second)) +
                      ": a query naming the old one has to be edited.");
    }
    return out;
  }
  const bool clickhouse = dialect == "clickhouse";
  // PostgreSQL has no `CREATE VIEW IF NOT EXISTS`; `CREATE OR REPLACE VIEW` is idempotent there.
  const std::string opening = clickhouse ? "CREATE VIEW IF NOT EXISTS" : "CREATE OR REPLACE VIEW";
  const std::string final = clickhouse ? " FINAL" : "";
  for (const auto& [entity, table] : layout.tables) {
    const auto old = was.find(entity);
    if (old == was.end()) {
      out.not_possible.emplace_back(entity, "the source layout gives no table for " +
                                                python_repr(entity) +
                                                ", so there is no old name to stand in for.");
      continue;
    }
    if (old->second == table) {
      std::string why = "both engines call this table " + python_repr(table) +
                        ", so the name is not what moved - the dialect is.";
      if (clickhouse) {
        why += " A query moved here verbatim must read `FROM " + quote_backtick(table) +
               " FINAL`: the table is a ReplacingMergeTree, so without it a row written twice "
               "under one key is counted twice until a background merge collapses it - measured, "
               "two rows against one.";
      }
      out.not_possible.emplace_back(entity, std::move(why));
      continue;
    }
    const auto columns = layout.columns.find(entity);
    if (columns == layout.columns.end() || columns->second.empty()) {
      throw EngineError("the layout gives no columns for " + python_repr(entity));
    }
    // Sorted by name, which is the order `CREATE TABLE` declares them in (`schema/009`).
    std::vector<std::string> selected;
    for (const auto& [column, unused] : columns->second) {
      selected.push_back(quote_identifier(dialect, column));
    }
    out.create.push_back(opening + " " + quote_identifier(dialect, old->second) + " AS SELECT " +
                         join(selected, ", ") + " FROM " + quote_identifier(dialect, table) + final);
    out.drop.push_back("DROP VIEW IF EXISTS " + quote_identifier(dialect, old->second));
  }
  return out;
}

}  // namespace sde
