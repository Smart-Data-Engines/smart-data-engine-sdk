#pragma once

/// Layouts derived from a model: what `{"auto": true}` in a placement map means, and the questions
/// about an engine's types that come before a group can be placed on it.
///
/// The interesting choices - indexes worth their cost, partitions, a second copy - are the control
/// plane's, and are not here. This is the boring total function from a model to a schema that
/// stores it, and it has to be the same function in every library: two libraries deriving different
/// table names from one model read and write different tables.

#include <map>
#include <optional>
#include <set>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

#include "sde/model.hpp"
#include "sde/physical.hpp"

namespace sde {

/// The dialects this library knows. A layout carries no dialect; an engine adapter has one.
inline constexpr std::string_view DIALECTS[] = {"clickhouse", "orderbook", "postgres"};

/// The orderbook engine's own name for its storage: one table, and we did not name it.
inline constexpr std::string_view ORDERBOOK_TABLE = "orderbook";
/// The nine fields an entity must declare, by name and neutral type, to live in that engine.
/// `price` and `quantity` are integers in the engine's sub-unit, not decimals.
inline constexpr std::pair<std::string_view, std::string_view> ORDERBOOK_SHAPE[] = {
    {"symbol", "string"}, {"exchange", "string"},    {"timestamp_ns", "int64"},
    {"side", "string"},   {"level", "int32"},        {"price", "int64"},
    {"quantity", "int64"}, {"order_count", "int32"}, {"sequence_number", "int64"}};
/// The key, in the order the engine addresses by.
inline constexpr std::string_view ORDERBOOK_KEY[] = {"symbol", "exchange", "timestamp_ns", "side",
                                                     "level"};
/// The one field of the shape declared nullable: the server numbers every update itself, so a row
/// is written without it and read back with the server's number.
inline constexpr std::string_view ORDERBOOK_NULLABLE[] = {"sequence_number"};

/// `OrderLine` -> `order_line`, NFC first: an underscore where a lowercase letter or digit meets an
/// uppercase one, or an uppercase run meets a capitalised word (both ASCII only), then the full
/// Unicode lowercase. Exactly Python's `snake_case` and TypeScript's, for every name.
[[nodiscard]] std::string snake_case(std::string_view name);

/// The engine's type for a neutral type. `DeclarationError` when the dialect has none - a gap
/// found here, loudly, rather than in a client's database where the column already exists.
[[nodiscard]] std::string column_type(std::string_view neutral, std::string_view dialect);

/// Whether the dialect has a column type for the neutral type. An unknown dialect and a malformed
/// `decimal(...)` are refused rather than answered: "cannot store" is a claim about an engine.
[[nodiscard]] bool can_store(std::string_view neutral, std::string_view dialect);

/// Whether the engine imposes its schema instead of accepting one (the orderbook engine): its
/// layout renders no DDL, and "no statements" there means nothing to run.
[[nodiscard]] bool schema_is_fixed(std::string_view dialect) noexcept;

/// One entity's columns in the neutral vocabulary, in the reference's order: declared fields in
/// name order, then `<relation>_<target key field>` for every relation from it, by relation name.
using NeutralColumns = std::vector<std::pair<std::string, std::string>>;

/// Every column a group's tables need, per entity, before any dialect.
[[nodiscard]] std::map<std::string, NeutralColumns> group_columns(const Model& model,
                                                                  const Group& group);
/// The fields each entity of the group declares nullable.
[[nodiscard]] std::map<std::string, std::set<std::string>> group_nullable(const Model& model,
                                                                          const Group& group);
/// The neutral types a group's tables need, sorted and distinct.
[[nodiscard]] std::vector<std::string> stored_types(const Model& model, const Group& group);

/// Why a group cannot live in a fixed-schema engine, or nothing if it can (and for any dialect that
/// is not fixed-schema). Names, types and nullability all have to match the engine's one shape.
[[nodiscard]] std::optional<std::string> fixed_schema_mismatch(
    const std::map<std::string, NeutralColumns>& columns, std::string_view dialect,
    const std::map<std::string, std::set<std::string>>& nullable);

/// The obvious schema for a group in one engine: a `snake_case` table per entity, every column
/// typed for the dialect, and in PostgreSQL one index per relation (`<table>_<relation>_idx`).
/// ClickHouse wraps a nullable field outside the key in `Nullable()` and gets no index rows: its
/// primary index is the sort key. The orderbook engine's layout is its own fixed shape.
[[nodiscard]] PhysicalLayout default_layout(const Model& model, const Group& group,
                                            std::string_view dialect = "postgres");

}  // namespace sde
