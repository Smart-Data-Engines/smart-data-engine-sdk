#pragma once

/// The placement map (format contract section 7): which engine holds each group, what its tables
/// look like there, and which copy each operation shape is routed to.
///
/// The map is the only instruction this library takes from outside, and because it decides where
/// data is written it is the one input that refuses rather than degrades. A map with no signature
/// is valid - that is the no-account mode, documented and supported. What is refused is a map that
/// carries a signature, claiming to come from the control plane, when no key was given to check
/// the claim.

#include <cstdint>
#include <map>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

#include "sde/json.hpp"
#include "sde/log.hpp"
#include "sde/model.hpp"
#include "sde/physical.hpp"

namespace sde {

/// The newest map format this library reads, and the oldest. Backwards compatible, forwards strict:
/// every older document has a complete meaning, and what came after this library cannot be known.
inline constexpr int MAP_CONTRACT = 6;
inline constexpr int MAP_CONTRACT_FLOOR = 1;
/// The contract that introduced `also_write`.
inline constexpr int ALSO_WRITE_SINCE = 2;
/// The contract that introduced `project_id` and a write generation in every group (section 7d).
inline constexpr int GENERATIONS_SINCE = 4;
/// The contract from which a group may carry no write generation (section 7k).
inline constexpr int UNFENCED_SINCE = 6;
/// The largest write generation: 2^53 - 1, the largest integer every language reads exactly.
inline constexpr std::int64_t MAX_EPOCH = 9007199254740991;

/// Tables and a column this library owns in the client's engines. A layout naming them is refused.
inline constexpr std::string_view WATERMARK_TABLE = "sde_map_state";
inline constexpr std::string_view BACKFILL_TABLE = "sde_backfill_state";
inline constexpr std::string_view DRAIN_TABLE = "__sde_fence_drains";
inline constexpr std::string_view EPOCH_COLUMN = "__sde_write_epoch";

/// One physical copy of a group in one engine.
struct Materialization {
  std::string id;
  std::string engine;
  PhysicalLayout layout;
  /// How far behind the source this copy may be. Absent exactly for the source, which is where
  /// writes land and so is not behind anything.
  std::optional<std::int64_t> lag_budget_ms;

  [[nodiscard]] bool is_source() const noexcept { return !lag_budget_ms.has_value(); }
};

/// Where one group lives.
struct GroupPlacement {
  std::string group;
  Materialization source;
  std::vector<Materialization> derived;  ///< in document order
  /// Ids of the derived copies writes are **also** sent to, in document order: how a migration
  /// reaches a library, with no phase name anywhere. Empty for a map that is not mid-migration.
  std::vector<std::string> also_write;
  /// The write generation (section 7d). Absent below contract 4, and from contract 6 for a group
  /// whose engine cannot fence writes.
  std::optional<std::int64_t> write_epoch;

  /// The source, then the derived copies in document order.
  [[nodiscard]] std::vector<const Materialization*> all() const;
  /// The copy with this id; `MapError` when the group has none.
  [[nodiscard]] const Materialization& by_id(std::string_view id) const;
  /// The copies `also_write` names, in its order.
  [[nodiscard]] std::vector<const Materialization*> also_write_targets() const;
};

/// The keys a signed map may be verified with: one bare key, which is reported back without a
/// name, or a set under names of the caller's choosing - which is what lets the signing key rotate
/// without breaking anybody, and why the name that verified is reported.
class PublicKeys {
 public:
  /// One 32-byte Ed25519 public key.
  [[nodiscard]] static PublicKeys bare(std::string key);
  /// Named keys. An empty set is refused when a map is loaded: it can verify nothing, and it is not
  /// the no-account mode (an unsigned map is).
  [[nodiscard]] static PublicKeys named(std::map<std::string, std::string> keys);

  [[nodiscard]] bool is_bare() const noexcept { return bare_; }
  /// Name to key; a bare key is under the empty name.
  [[nodiscard]] const std::map<std::string, std::string>& keys() const noexcept { return keys_; }

 private:
  bool bare_ = false;
  std::map<std::string, std::string> keys_;
};

struct LoadOptions {
  /// The model the map is for. Optional only so the format can be exercised on its own; in an
  /// application it is always given, because a map for another model version must be refused
  /// rather than half-applied, and `{"auto": true}` needs it.
  const Model* model = nullptr;
  /// Absent: a signed map is refused, an unsigned one accepted (the no-account mode).
  std::optional<PublicKeys> public_keys;
  /// Refuse an unsigned map too.
  bool require_signature = false;
  /// Receives `sde.map.loaded` or `sde.map.rejected`.
  LogSink log;
};

/// A loaded, validated placement map. Immutable: only `load_map` makes one, so holding one means
/// every rule of section 7 was checked against exactly this content.
class PlacementMap {
 public:
  [[nodiscard]] int contract() const noexcept { return contract_; }
  [[nodiscard]] const std::string& model_version() const noexcept { return model_version_; }
  [[nodiscard]] std::int64_t map_version() const noexcept { return map_version_; }
  /// Every group, by name.
  [[nodiscard]] const std::map<std::string, GroupPlacement>& groups() const noexcept {
    return groups_;
  }
  /// The group's placement; `MapError` when the map has none - a group with nowhere to live is not
  /// a slow path, it is an unanswerable operation.
  [[nodiscard]] const GroupPlacement& placement_of(std::string_view group) const;
  /// Shape id to materialisation id.
  [[nodiscard]] const std::map<std::string, std::string>& routing() const noexcept {
    return routing_;
  }
  /// Whether the document carried a signature (which, loaded, verified).
  [[nodiscard]] bool is_signed() const noexcept { return signed_; }
  /// The caller's name for the key that verified the map; empty for an unsigned map and for one
  /// verified with a bare key, which `is_signed` tells apart.
  [[nodiscard]] const std::optional<std::string>& verified_with() const noexcept {
    return verified_with_;
  }
  /// From contract 4.
  [[nodiscard]] const std::optional<std::string>& project_id() const noexcept {
    return project_id_;
  }
  /// SHA-256 of the canonical map without its signature, lowercase hex (section 7b). Empty only for
  /// a legacy map (contract 3 or below) that is not canonically encodable.
  [[nodiscard]] const std::optional<std::string>& fingerprint() const noexcept {
    return fingerprint_;
  }

 private:
  friend PlacementMap load_map(const Json& document, const LoadOptions& options);
  PlacementMap() = default;

  int contract_ = 0;
  std::string model_version_;
  std::int64_t map_version_ = 0;
  std::map<std::string, GroupPlacement> groups_;
  std::map<std::string, std::string> routing_;
  bool signed_ = false;
  std::optional<std::string> verified_with_;
  std::optional<std::string> project_id_;
  std::optional<std::string> fingerprint_;
};

/// Parses and validates a placement map, in the order of section 8a, and puts the outcome on the
/// record through `options.log` either way. Every refusal is a `MapError` naming the defect.
[[nodiscard]] PlacementMap load_map(const Json& document, const LoadOptions& options = {});
/// The same, from JSON text. Text that is not JSON is a `MapError` too.
[[nodiscard]] PlacementMap load_map(std::string_view json_text, const LoadOptions& options = {});
// Text, as above: a JSON string value is never a map, so a literal or a `std::string` is read as
// the document's text rather than being ambiguous between the two.
[[nodiscard]] inline PlacementMap load_map(const char* json_text, const LoadOptions& options = {}) {
  return load_map(std::string_view(json_text), options);
}
[[nodiscard]] inline PlacementMap load_map(const std::string& json_text,
                                           const LoadOptions& options = {}) {
  return load_map(std::string_view(json_text), options);
}

}  // namespace sde
