#pragma once

/// Copying a group into its second engine, and proving the copy is complete (Tier 2).
///
/// This module produces numbers and the control plane decides: copying a row means reading a
/// client's row, and comparing two copies means holding both engines open, neither of which the
/// control plane may ever do.

#include <map>
#include <optional>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

#include "sde/engine.hpp"
#include "sde/json.hpp"
#include "sde/model.hpp"
#include "sde/placement.hpp"
#include "sde/value.hpp"
#include "sde/verification.hpp"

namespace sde {

class Session;

/// The ordering columns of a keyset scan, refusing an empty order (`EngineError`): with none, every
/// page is the first page and a backfill would copy the same chunk until it was stopped.
[[nodiscard]] std::vector<std::string> key_columns(const std::vector<std::string>& order,
                                                   const std::string& table);

/// A bound has one value per ordering column (`EngineError` otherwise): a row-value comparison of
/// another width is a different comparison, not a narrower one.
void same_width(const std::vector<Value>& bound, const std::vector<std::string>& columns,
                std::string_view name);

/// Sub-second digits a dialect keeps, for the neutral types where dialects differ. A type in
/// neither this table nor `PRECISION_INDEPENDENT` refuses a copy, so adding one to the vocabulary
/// forces a decision rather than inheriting one nobody made.
[[nodiscard]] std::optional<int> dialect_precision(std::string_view neutral,
                                                   std::string_view dialect) noexcept;

/// Neutral types a copy between dialects does not silently change (`decimal(p,s)` is one too).
inline constexpr std::string_view PRECISION_INDEPENDENT[] = {
    "bool", "int32", "int64", "float32", "float64", "string", "bytes", "uuid", "date", "json"};

/// Why copying these columns from one dialect to another would change values, or nothing. Asked
/// by a backfill before it copies and by a session before its first fan-out write: a truncation in
/// the fan-out happens on every write, with no error anywhere.
[[nodiscard]] std::optional<std::string> precision_refusal(
    std::string_view group, std::string_view entity,
    const std::map<std::string, std::string>& columns, std::string_view source_dialect,
    std::string_view target_dialect);

/// Rows per chunk, by default: a chunk is held in memory twice during `verify`, and a crash
/// discards one chunk's work, which is the number that matters.
inline constexpr std::int64_t CHUNK_ROWS = 1000;

/// What a migration reads: an application's session, or an operator's inspection context.
struct MigrationView {
  const Model& model;
  const PlacementMap& placement;
  const std::map<std::string, Engine*>& engines;
  std::optional<std::string> project_id;
  LogSink log;
};

/// How far one entity's copy into one target has got.
struct EntityProgress {
  std::string entity;
  std::string engine;
  std::string table;
  std::int64_t rows_copied = 0;  ///< the marker: rows copied into this target, across every run
  std::int64_t rows_this_run = 0;
  std::int64_t chunks = 0;
  bool complete = false;  ///< the last chunk came back short: the tail is the fan-out's now

  [[nodiscard]] Json as_record() const;
};

/// What one call to `backfill` did, per entity and per target.
struct BackfillProgress {
  std::string group;
  std::vector<EntityProgress> entities;

  [[nodiscard]] bool complete() const noexcept;
  [[nodiscard]] std::int64_t rows_this_run() const noexcept;
  [[nodiscard]] Json as_record() const;
  [[nodiscard]] std::string for_a_human() const;
};

/// One source row the target does not have, or has differently. **This holds the client's own
/// data**, which is why the report's record does not: it stays on their machine.
struct Difference {
  std::string entity;
  std::string table;
  Row key;
  std::vector<std::string> columns;  ///< empty when the row is absent from the target

  [[nodiscard]] bool absent() const noexcept { return columns.empty(); }
  [[nodiscard]] std::string for_a_human() const;
};

/// What the comparison found, in the shape the gate needs and nothing wider.
struct VerifyReport {
  std::string at;
  std::string group;
  std::int64_t chunks_compared = 0;
  std::int64_t chunks_mismatched = 0;
  std::int64_t tail_rows_read = 0;
  std::int64_t tail_rows_missing_in_target = 0;
  std::int64_t rows_source = 0;
  std::int64_t rows_target = 0;
  std::vector<Difference> differences;  ///< the first twenty
  std::int64_t differences_suppressed = 0;
  std::optional<VerificationRequest> request;

  /// The target holds everything the source holds: zero tolerance, both terms.
  [[nodiscard]] bool matched() const noexcept;
  /// The seven counts the gate reads, and the request it answers. Numbers, never rows.
  [[nodiscard]] Json as_record() const;
  [[nodiscard]] std::string for_a_human() const;
};

struct BackfillOptions {
  std::int64_t chunk_rows = CHUNK_ROWS;
  /// Chunks per entity before stopping; empty runs each entity to the end of its table.
  std::optional<std::int64_t> stop_after;
};

struct VerifyOptions {
  std::int64_t chunk_rows = CHUNK_ROWS;
  /// The control plane's request this comparison answers, checked against the session first.
  std::optional<VerificationRequest> request;
  /// When the comparison was taken; the current UTC time when empty.
  std::optional<std::string> at;
};

/// Copies a group's existing rows into every fan-out target its map names, in chunks, recording
/// the marker - a row count - after each. Resumable, and a no-op once complete. Every refusal comes
/// before a single row moves (`MigrationRefused`).
BackfillProgress backfill(Session& session, const std::string& group,
                          const BackfillOptions& options = {});

/// Compares both copies of a group, chunk by chunk below the marker and row by row above it, and
/// reports counts. Reads the source first and the copy second, always.
VerifyReport verify(const MigrationView& view, const std::string& group,
                    const VerifyOptions& options = {});
VerifyReport verify(Session& session, const std::string& group, const VerifyOptions& options = {});

/// An operator's view of a migration's data, separate from an application's session: the map in
/// force before a maintenance identifies the copies while their native epochs differ. It adopts no
/// map and advances no watermark.
class InspectionContext {
 public:
  /// Refuses a map below contract 4, a project other than the map's, a model other than the map's,
  /// and a missing engine of any group with a generation.
  InspectionContext(const Model& model, const PlacementMap& placement,
                    std::map<std::string, Engine*> engines, std::string project_id);

  [[nodiscard]] MigrationView view() const;
  [[nodiscard]] const Model& model() const noexcept { return model_; }
  [[nodiscard]] const PlacementMap& placement() const noexcept { return placement_; }
  [[nodiscard]] const std::map<std::string, Engine*>& engines() const noexcept { return engines_; }
  [[nodiscard]] const std::string& project_id() const noexcept { return project_id_; }

 private:
  const Model& model_;
  const PlacementMap& placement_;
  std::map<std::string, Engine*> engines_;
  std::string project_id_;
};

/// One table frozen under a named barrier for an exact comparison.
struct FrozenTable {
  std::string engine;
  std::string materialization;
  std::string table;
  std::string identity;
  std::string project_id;
  std::int64_t epoch = 0;
  std::string hold_id;

  [[nodiscard]] Json as_record() const;
};

struct FrozenVerifyReport {
  VerifyReport comparison;
  std::vector<FrozenTable> barriers;
  std::int64_t elapsed_ms = 0;

  /// Matched, and the two copies hold the same number of rows: nothing moves under the barriers.
  [[nodiscard]] bool matched() const noexcept;
  [[nodiscard]] Json as_record() const;
};

struct FrozenOptions {
  std::int64_t chunk_rows = CHUNK_ROWS;
  std::optional<std::string> at;
};

/// Closes every table of a group's source and copies under the named barrier `hold_id`, drains
/// what was admitted, compares their exact content and leaves the barriers in place (format
/// contract section 7e). `epochs` names every materialisation id of the comparison - the source and
/// each copy - because they can differ during a maintenance.
FrozenVerifyReport verify_frozen(const InspectionContext& context, const std::string& group,
                                 const VerificationRequest& request, const std::string& hold_id,
                                 const std::map<std::string, std::int64_t>& epochs,
                                 const FrozenOptions& options = {});

}  // namespace sde

