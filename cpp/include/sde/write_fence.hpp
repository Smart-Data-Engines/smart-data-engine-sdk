#pragma once

/// Engine-enforced write generations and named barriers (format contract section 7c), entirely on
/// the client's side.
///
/// A generation travels with each write, and native CHECK constraints make the test part of the
/// INSERT: checking it before an insert would leave a paused process able to commit against a
/// stale placement. These are primitives for a cutover executor, not permission to switch a map.

#include <cstdint>
#include <map>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

#include "sde/json.hpp"

namespace sde {

/// The reserved namespace of the constraints a fence consists of.
inline constexpr std::string_view FENCE_PREFIX = "__sde_f_";
/// The hold a table carries while its fence is being provisioned.
inline constexpr std::string_view FENCE_SETUP = "__sde_f_setup";

/// The reserved write-epoch column as the engine reports it.
enum class ColumnState { absent, valid, conflict };

/// `absent`, `valid`, `conflict`.
[[nodiscard]] std::string_view column_state_name(ColumnState state) noexcept;

/// One table's fence as its engine's catalogue describes it: the table's identity, the reserved
/// column, and every constraint by name with its predicate as the engine writes it back.
struct FenceMetadata {
  std::string identity;
  ColumnState column = ColumnState::absent;
  std::map<std::string, std::string> constraints;
};

/// The DDL capability behind a fence, held by provisioning and execution roles and never required
/// of a runtime role.
class FenceBackend {
 public:
  virtual ~FenceBackend() = default;
  virtual FenceMetadata metadata(const std::string& table) = 0;
  virtual void add_column(const std::string& table) = 0;
  virtual void add_constraint(const std::string& table, const std::string& name,
                              const std::string& expression) = 0;
  virtual void drop_constraint(const std::string& table, const std::string& name) = 0;
  /// Waits until no insert admitted before the hold took effect is still running.
  virtual void drain(const std::string& table, const std::string& project_id,
                     const std::string& hold) = 0;
  virtual void restore(const std::string& table, const std::string& project_id,
                       const std::string& hold) = 0;
};

/// What a table's constraints say about its fence.
struct FenceState {
  std::string identity;
  std::optional<std::string> project_id;
  ColumnState column = ColumnState::absent;
  std::vector<std::int64_t> minimums;  ///< sorted
  std::vector<std::int64_t> maximums;  ///< sorted
  std::vector<std::string> holds;      ///< sorted; `setup` while being provisioned
  std::vector<std::string> retired;    ///< sorted

  [[nodiscard]] std::optional<std::int64_t> lower_epoch() const;
  [[nodiscard]] std::optional<std::int64_t> upper_epoch() const;
  /// An owner, the column, and both bounds.
  [[nodiscard]] bool complete() const;
  /// A hold is in place, or the bounds admit nothing.
  [[nodiscard]] bool closed() const;
  /// The one epoch a complete fence admits, when its bounds agree.
  [[nodiscard]] std::optional<std::int64_t> epoch() const;
  [[nodiscard]] Json as_record() const;
};

/// Reads the reserved constraints of one table. Refuses (`MigrationRefused`) a name in the reserved
/// namespace it does not know, a predicate other than the one its name implies, fences of two
/// projects, and bounds or holds without an owner.
[[nodiscard]] FenceState fence_state(const FenceMetadata& metadata);

/// A write epoch: a positive safe integer (1 to 2^53 - 1). `MigrationRefused` otherwise.
std::int64_t check_epoch(std::int64_t epoch);

/// A write epoch from a JSON number as the contract reads one: an integral number however it is
/// written - `1.0` is `1` - within the safe range, and nothing else (`MigrationRefused`).
[[nodiscard]] std::int64_t epoch_from_json(const Json& value);

/// A table's provisioning capability. Serialise executor commands per project state directory.
///
/// Writes carry a number from their own immutable placement, so replaying a stale administrative
/// command cannot lower the native minimum. A partial command can leave the table closed, and an
/// I/O error calls for inspection and resumption, not an assumption of rollback.
class WriteFence {
 public:
  /// `backend` must outlive the fence. Refuses (`MigrationRefused`) a project id that is not 32
  /// lowercase hexadecimal digits, an empty table or one with a NUL, and the library's own tables.
  WriteFence(FenceBackend& backend, std::string table, std::string project_id);

  [[nodiscard]] const std::string& table() const noexcept { return table_; }
  [[nodiscard]] const std::string& project_id() const noexcept { return project_id_; }

  /// The fence as it stands; refuses one that belongs to another project or a column whose
  /// definition conflicts with the reserved one.
  FenceState state();
  /// Installs the fence at `epoch`, or confirms an existing one at that epoch.
  FenceState prepare(std::int64_t epoch);
  /// Closes the table under a named barrier and waits for admitted writes to finish.
  FenceState freeze(const std::string& request_id);
  FenceState resume(const std::string& request_id);
  FenceState resume_prepare(std::int64_t epoch);
  /// Moves the epoch forward, under a barrier.
  FenceState advance(std::int64_t epoch);
  /// Retires a barrier and reopens admission, once the epoch change is complete.
  FenceState release(const std::string& request_id);

 private:
  FenceState ready();
  void bounds(std::int64_t epoch);

  FenceBackend* backend_;
  std::string table_;
  std::string project_id_;
};

}  // namespace sde
