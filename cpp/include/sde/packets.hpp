#pragma once

/// Signed packets a local operator executes (format contract sections 7g, 7h and 7j): a cutover, a
/// staging and an in-place index build. This library decodes and authorises them - every rule of a
/// packet checked against the model, the locally configured project and the keys - and executes
/// none of them. Loading touches no engine, adopts no map and advances no watermark.
///
/// A plan exists only as loaded: its constructors are private, so every plan in hand has passed
/// every rule, and its record and fingerprint are those of the document that was checked.

#include <cstdint>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

#include "sde/json.hpp"
#include "sde/model.hpp"
#include "sde/physical.hpp"
#include "sde/placement.hpp"
#include "sde/verification.hpp"

namespace sde {

/// A move: the maintained copy that takes over is in another engine binding than the source.
inline constexpr int CUTOVER_PROTOCOL = 1;
/// A relayout: the copy that takes over is in the source's own engine binding, under fresh names.
inline constexpr int CUTOVER_RELAYOUT_PROTOCOL = 2;
/// A move: the fresh copy is prepared in another engine binding than the source.
inline constexpr int STAGING_PROTOCOL = 1;
/// A relayout: the fresh copy is prepared in the source's own engine binding, under fresh names.
inline constexpr int STAGING_RELAYOUT_PROTOCOL = 2;
/// Indexes added to one group's source, built on the tables in force; no copy, no cutover.
inline constexpr int INDEX_PROTOCOL = 1;
/// Indexes added to and removed from one group's source, in place; at least one removed.
inline constexpr int INDEX_CHANGE_PROTOCOL = 2;
/// A day. The budget bounds a build on a server that stopped answering; nothing is paused while it
/// runs, so it is not a pause budget and it is deliberately far longer than a cutover's.
inline constexpr std::int64_t MAX_BUILD_BUDGET_MS = 86'400'000;

/// A signed cutover authorization: the map in force, and the two maps that end it.
class CutoverPlan {
 public:
  [[nodiscard]] const std::string& plan_id() const noexcept { return plan_id_; }
  [[nodiscard]] const std::string& project_id() const noexcept { return project_id_; }
  [[nodiscard]] const std::string& group() const noexcept { return group_; }
  [[nodiscard]] int protocol() const noexcept { return protocol_; }
  [[nodiscard]] const PlacementMap& before() const noexcept { return before_; }
  [[nodiscard]] const PlacementMap& success() const noexcept { return success_; }
  [[nodiscard]] const PlacementMap& abort() const noexcept { return abort_; }
  [[nodiscard]] const VerificationRequest& verification() const noexcept { return verification_; }
  [[nodiscard]] std::int64_t pause_budget_ms() const noexcept { return pause_budget_ms_; }
  [[nodiscard]] const std::string& query_impact_digest() const noexcept {
    return query_impact_digest_;
  }
  /// The name of the configured key that verified the packet; nothing for a bare key.
  [[nodiscard]] const std::optional<std::string>& verified_with() const noexcept {
    return verified_with_;
  }
  /// SHA-256 of the canonical packet without its signature, lowercase hex.
  [[nodiscard]] const std::string& fingerprint() const noexcept { return fingerprint_; }

  /// The group's generation before the cutover, during its maintenance, and once it succeeds.
  [[nodiscard]] std::int64_t source_epoch() const noexcept { return source_epoch_; }
  [[nodiscard]] std::int64_t maintenance_epoch() const noexcept { return source_epoch_ + 1; }
  [[nodiscard]] std::int64_t activation_epoch() const noexcept { return source_epoch_ + 2; }

  /// The packet as it was checked, with every integral number an integer.
  [[nodiscard]] const Json& as_record() const noexcept { return document_; }
  /// The canonical bytes of the map one outcome publishes: `success` or `abort`.
  [[nodiscard]] std::string candidate_payload(std::string_view outcome) const;
  /// Refuses (`MigrationRefused`) a map in force other than the signed one the packet starts from.
  void check_current(const PlacementMap& current) const;

 private:
  friend CutoverPlan load_cutover_plan(const Json& raw, const Model& model,
                                       const std::string& project_id,
                                       const PublicKeys& public_keys);
  CutoverPlan(PlacementMap before, PlacementMap success, PlacementMap abort,
              VerificationRequest verification);

  std::string plan_id_;
  std::string project_id_;
  std::string group_;
  int protocol_ = 0;
  PlacementMap before_;
  PlacementMap success_;
  PlacementMap abort_;
  VerificationRequest verification_;
  std::int64_t pause_budget_ms_ = 0;
  std::string query_impact_digest_;
  std::optional<std::string> verified_with_;
  std::int64_t source_epoch_ = 0;
  std::string fingerprint_;
  Json document_;
};

/// Verifies a cutover authorization and its three candidate maps exactly (`MigrationRefused`): no
/// I/O, no reservation, no activation, no watermark. A map or encoding refusal inside it is reported
/// as `cutover document refused: ...`.
[[nodiscard]] CutoverPlan load_cutover_plan(const Json& raw, const Model& model,
                                            const std::string& project_id,
                                            const PublicKeys& public_keys);

/// The portable physical table of the `position`-th entity (one-based, in code point order) of a
/// staged copy: `sde_m_<stage id>_<position, six digits>`.
[[nodiscard]] std::string staging_table_name(std::string_view stage_id, std::int64_t position);

/// A signed staging authorization: prepare one fresh copy while retaining the source.
class StagingPlan {
 public:
  [[nodiscard]] const std::string& stage_id() const noexcept { return stage_id_; }
  [[nodiscard]] const std::string& project_id() const noexcept { return project_id_; }
  [[nodiscard]] const std::string& group() const noexcept { return group_; }
  [[nodiscard]] int protocol() const noexcept { return protocol_; }
  [[nodiscard]] const PlacementMap& current() const noexcept { return current_; }
  [[nodiscard]] const PlacementMap& prepared() const noexcept { return prepared_; }
  [[nodiscard]] const std::optional<std::string>& verified_with() const noexcept {
    return verified_with_;
  }
  [[nodiscard]] const std::string& fingerprint() const noexcept { return fingerprint_; }
  [[nodiscard]] const Json& as_record() const noexcept { return document_; }
  /// The canonical bytes of the prepared map, which the operator publishes.
  [[nodiscard]] std::string prepared_payload() const;
  void check_current(const PlacementMap& current) const;

 private:
  friend StagingPlan load_staging_plan(const Json& raw, const Model& model,
                                       const std::string& project_id,
                                       const PublicKeys& public_keys);
  StagingPlan(PlacementMap current, PlacementMap prepared);

  std::string stage_id_;
  std::string project_id_;
  std::string group_;
  int protocol_ = 0;
  PlacementMap current_;
  PlacementMap prepared_;
  std::optional<std::string> verified_with_;
  std::string fingerprint_;
  Json document_;
};

/// Verifies a staging authorization and its two maps exactly (`MigrationRefused`). A map or
/// encoding refusal inside it is reported as `staging authorization refused: ...`.
[[nodiscard]] StagingPlan load_staging_plan(const Json& raw, const Model& model,
                                            const std::string& project_id,
                                            const PublicKeys& public_keys);

/// The physical name of the `position`-th index an in-place build adds (one-based):
/// `sde_i_<index build id>_<position, six digits>`.
[[nodiscard]] std::string index_build_name(std::string_view index_id, std::int64_t position);

/// A signed authorization to build, and with protocol 2 also to remove, indexes in place.
class IndexPlan {
 public:
  [[nodiscard]] const std::string& index_id() const noexcept { return index_id_; }
  [[nodiscard]] const std::string& project_id() const noexcept { return project_id_; }
  [[nodiscard]] const std::string& group() const noexcept { return group_; }
  [[nodiscard]] int protocol() const noexcept { return protocol_; }
  [[nodiscard]] const PlacementMap& current() const noexcept { return current_; }
  [[nodiscard]] const PlacementMap& prepared() const noexcept { return prepared_; }
  [[nodiscard]] std::int64_t build_budget_ms() const noexcept { return build_budget_ms_; }
  /// The new definitions in position order, as the prepared map carries them.
  [[nodiscard]] const std::vector<Index>& added() const noexcept { return added_; }
  /// Protocol 2: the definitions in force the next map drops, in their order in force.
  [[nodiscard]] const std::vector<Index>& removed() const noexcept { return removed_; }
  [[nodiscard]] const std::optional<std::string>& verified_with() const noexcept {
    return verified_with_;
  }
  [[nodiscard]] const std::string& fingerprint() const noexcept { return fingerprint_; }
  [[nodiscard]] const Json& as_record() const noexcept { return document_; }
  [[nodiscard]] std::string prepared_payload() const;
  void check_current(const PlacementMap& current) const;

 private:
  friend IndexPlan load_index_plan(const Json& raw, const Model& model,
                                   const std::string& project_id, const PublicKeys& public_keys);
  IndexPlan(PlacementMap current, PlacementMap prepared);

  std::string index_id_;
  std::string project_id_;
  std::string group_;
  int protocol_ = 0;
  PlacementMap current_;
  PlacementMap prepared_;
  std::int64_t build_budget_ms_ = 0;
  std::vector<Index> added_;
  std::vector<Index> removed_;
  std::optional<std::string> verified_with_;
  std::string fingerprint_;
  Json document_;
};

/// Verifies an index build authorization and its two maps exactly (`MigrationRefused`). A map or
/// encoding refusal inside it is reported as `index build authorization refused: ...`.
[[nodiscard]] IndexPlan load_index_plan(const Json& raw, const Model& model,
                                        const std::string& project_id,
                                        const PublicKeys& public_keys);

}  // namespace sde
