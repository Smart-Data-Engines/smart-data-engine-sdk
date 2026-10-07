#pragma once

/// ClickHouse's native write barriers, the reference's `ClickHouseFences`: the reserved column and
/// its CHECK constraints on a local MergeTree table of an Atomic database, and a drain that proves
/// no insert admitted before a hold is still running - by detaching the table and attaching it
/// again, after recording the table's exact identity, so a resumed drain never attaches another
/// table because its name fits.

#include <map>
#include <string>
#include <string_view>
#include <vector>

#include "sde/value.hpp"
#include "sde/write_fence.hpp"

namespace sde {
class ClickHouseEngine;
}

namespace sde::detail::clickhouse {

class Fences final : public FenceBackend {
 public:
  explicit Fences(ClickHouseEngine& engine) : engine_(engine) {}

  FenceMetadata metadata(const std::string& table) override;
  void add_column(const std::string& table) override;
  void add_constraint(const std::string& table, const std::string& name,
                      const std::string& expression) override;
  void drop_constraint(const std::string& table, const std::string& name) override;
  void drain(const std::string& table, const std::string& project_id,
             const std::string& hold) override;
  void restore(const std::string& table, const std::string& project_id,
               const std::string& hold) override;

 private:
  /// Each row's cells in the order the statement names them.
  std::vector<std::vector<Value>> query(const std::string& sql);
  void command(const std::string& sql);
  std::vector<std::vector<Value>> table_rows(const std::string& table);
  void drain_log();

  ClickHouseEngine& engine_;
};

/// The barrier constraints of a `formatQuery(create_table_query)`: each `CONSTRAINT __sde_f_...
/// CHECK expression` line, the expression without its trailing comma.
[[nodiscard]] std::map<std::string, std::string> fence_constraints(std::string_view statement);

}  // namespace sde::detail::clickhouse
