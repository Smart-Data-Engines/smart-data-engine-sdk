#pragma once

/// PostgreSQL's native write barriers, the reference's `PostgresFences`: CHECK constraints added
/// `NOT VALID`, a bigint generation column, and a drain that takes the table's lock, so returning
/// from it proves every writer holding a conflicting lock has finished.

#include <string>

#include "sde/write_fence.hpp"

namespace sde {
class PostgresEngine;
}  // namespace sde

namespace sde::detail::postgres {

class Fences final : public FenceBackend {
 public:
  explicit Fences(PostgresEngine& engine) noexcept : engine_(engine) {}

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
  /// DDL runs outside an application's transaction, on a connection kept for it.
  void idle() const;

  PostgresEngine& engine_;
};

}  // namespace sde::detail::postgres
