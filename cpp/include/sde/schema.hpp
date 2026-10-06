#pragma once

/// DDL as a value (format contract section 7a, Tier 2): a layout and a set of keys in, statements
/// out, no connection anywhere. The engine adapters apply these statements; the control plane shows
/// them to a client as the schema chosen for them. Statements are bytes a server receives, so the
/// `schema/` vectors compare them exactly.

#include <map>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

#include "sde/physical.hpp"

namespace sde {

/// An identifier in double quotes, a double quote doubled: PostgreSQL's rule, under which a
/// backslash is literal.
[[nodiscard]] std::string quote_ansi(std::string_view identifier);

/// An identifier in backticks, a backtick **and a backslash** escaped with a backslash, in one
/// pass: ClickHouse's lexer reads `\n` inside backticks as an escape, so a name containing a
/// backslash would otherwise reach the server as a different name (`schema/011`).
[[nodiscard]] std::string quote_backtick(std::string_view identifier);

/// The dialect's quoting: `postgres` and `clickhouse`. A fixed-schema engine never receives an
/// identifier of ours, and an unknown dialect has no rule; both are refused (`EngineError`) rather
/// than quoted with a rule that escapes nothing.
[[nodiscard]] std::string quote_identifier(std::string_view dialect, std::string_view identifier);

/// The collation of every PostgreSQL text column and of every text column in an index this library
/// creates. Its reads compare and order text by code point and say so on every text column they
/// touch; PostgreSQL uses an index only for an expression of the index's own collation.
inline constexpr std::string_view POSTGRES_TEXT_COLLATION = "COLLATE \"C\"";

/// Whether the engine imposes its own schema, so that an empty statement list means "nothing to
/// run" rather than "no tables". An unknown dialect is refused (`EngineError`): answering `false`
/// would say that engine takes DDL from us.
[[nodiscard]] bool schema_is_fixed(std::string_view dialect);

/// The statements that create this layout, in the order they must run, each `IF NOT EXISTS`.
/// `keys` is each entity's key in declared order. Tables come in code point order of the entity,
/// columns in code point order of their name, and the key in physical order (`key_order`, else
/// declared). Refuses (`EngineError`) an unknown dialect, an entity without columns or a key, a key
/// order that is not a permutation of the key, a partition outside the key, a design the dialect
/// cannot render, and - for a fixed-schema engine - any physical design at all.
[[nodiscard]] std::vector<std::string> schema_statements(
    const PhysicalLayout& layout, const std::map<std::string, std::vector<std::string>>& keys,
    std::string_view dialect);

/// `<name> ON <table> [USING <method>] (<columns>)`, shared by every PostgreSQL index this library
/// creates: a new table's, an in-place build's and a staged copy's. `columns` is the entity's layout
/// columns, so a text column is indexed with the reads' collation.
[[nodiscard]] std::string postgres_index_target(const Index& index, std::string_view table,
                                                const std::map<std::string, std::string>& columns);

/// `INDEX <name> <column> TYPE <type> GRANULARITY <n>`, inside `CREATE TABLE` or after `ADD`.
[[nodiscard]] std::string clickhouse_index_clause(const Index& index);

/// What can stand under a table's old name on the target after a group has moved, and what cannot
/// (with the reason, per entity, in code point order). A view cannot cross engines, so it serves a
/// query that was re-pointed at the new engine and still names the old table.
struct CompatibilityViews {
  std::vector<std::string> create;  ///< run on the target when reads switch
  std::vector<std::string> drop;    ///< run when the source is dropped
  std::vector<std::pair<std::string, std::string>> not_possible;  ///< (entity, why)

  /// Whether every table got a view. `false` is ordinary: two dialects usually name a table alike.
  [[nodiscard]] bool complete() const noexcept { return not_possible.empty(); }
};

/// Views on the target under the names the group's tables had in the engine it left. `layout` is
/// the group's layout on the target and `was` each entity's table name on the source. Columns are
/// listed by name, in the order `CREATE TABLE` declares them; ClickHouse reads `FINAL`.
[[nodiscard]] CompatibilityViews compatibility_views(const PhysicalLayout& layout,
                                                     const std::map<std::string, std::string>& was,
                                                     std::string_view dialect);

}  // namespace sde
