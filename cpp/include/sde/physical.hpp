#pragma once

/// The physical design vocabulary a placement map can carry (format contract section 7i, map
/// contract 5): the order of an entity's key, a time partition, and indexes of a named method. Every
/// element is a closed vocabulary this library renders; nothing in a map is pasted into DDL.

#include <cstdint>
#include <map>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

namespace sde {

inline constexpr int PHYSICAL_DESIGN_SINCE = 5;

/// Time partition sizes. No week: ClickHouse's week depends on a mode, and a closed vocabulary has
/// no modes.
inline constexpr std::string_view GRANULARITIES[] = {"day", "month", "year"};
/// Neutral types a partition may be derived from. `timestamp` is absent: its ClickHouse column has
/// no zone, so the partition a value falls into would follow the server's configuration.
inline constexpr std::string_view TEMPORAL_TYPES[] = {"date", "timestamptz"};
inline constexpr std::string_view POSTGRES_METHODS[] = {"brin", "btree"};
/// ClickHouse data-skipping index types. It has no B-tree; its primary index is `ORDER BY`.
inline constexpr std::string_view CLICKHOUSE_METHODS[] = {"bloom_filter", "minmax", "set"};
inline constexpr std::string_view INDEX_METHODS[] = {"bloom_filter", "brin", "btree", "minmax",
                                                     "set"};
inline constexpr std::int64_t GRANULARITY_MIN = 1;
inline constexpr std::int64_t GRANULARITY_MAX = 1024;
inline constexpr std::int64_t SET_ROWS_MIN = 1;
inline constexpr std::int64_t SET_ROWS_MAX = 65536;

/// One index of a layout. `method` is `btree` when the document says nothing, which is what every
/// map before contract 5 meant.
struct Index {
  std::string entity;
  std::string name;
  std::vector<std::string> columns;
  std::string method = "btree";
  bool method_written = false;  ///< whether the document named the method, for faithful re-encoding
  std::optional<std::int64_t> granularity;
  std::optional<std::int64_t> max_rows;

  friend bool operator==(const Index&, const Index&) = default;
};

/// `{"field", "granularity"}` - exactly those two keys.
struct Partition {
  std::string field;
  std::string granularity;

  friend bool operator==(const Partition&, const Partition&) = default;
};

/// What a group looks like inside one engine (section 7): a table per entity, each table's columns in
/// the engine's own type spelling, and the physical design of section 7i. Ours to choose and ours to
/// change, which is why the client's code never names a table.
struct PhysicalLayout {
  std::map<std::string, std::string> tables;                          ///< entity -> table
  std::map<std::string, std::map<std::string, std::string>> columns;  ///< entity -> column -> type
  std::vector<Index> indexes;                                         ///< in document order
  std::map<std::string, Partition> partition_by;                      ///< entity -> its partition
  std::map<std::string, std::vector<std::string>> key_order;          ///< entity -> key, physically

  /// The table of this entity; `MapError` when the layout has none, which is a defect in the map
  /// rather than something a library can work around.
  [[nodiscard]] const std::string& table_for(std::string_view entity) const;

  friend bool operator==(const PhysicalLayout&, const PhysicalLayout&) = default;
};

[[nodiscard]] bool is_clickhouse_method(std::string_view method) noexcept;
[[nodiscard]] bool is_postgres_method(std::string_view method) noexcept;

/// The key in physical order: the layout's `key_order` for the entity when present, the declared
/// order otherwise. Refuses (`error` is the class to raise) a `key_order` that is not a permutation
/// of the key, so a renderer without the model still cannot drop a key column.
[[nodiscard]] std::vector<std::string> effective_key(
    std::string_view where, std::string_view entity, const std::vector<std::string>& key,
    const std::map<std::string, std::vector<std::string>>& key_order);

}  // namespace sde
