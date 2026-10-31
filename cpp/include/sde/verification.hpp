#pragma once

/// Verification requests (format contract section 7b): a comparison bound to one request, one
/// project and one exact placement map, without carrying a row.
///
/// A matching row count is not evidence about a particular migration. The control plane persists a
/// request before the comparison, and the verifier echoes it only after checking it against its own
/// session. The project id is the client's local configuration, never learned from the request, so
/// identical models and maps in two projects are not interchangeable evidence.

#include <cstdint>
#include <optional>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

#include "sde/json.hpp"
#include "sde/placement.hpp"

namespace sde {

inline constexpr int REQUEST_PROTOCOL = 1;

/// An ISO timestamp with its offset, `YYYY-MM-DDTHH:MM:SS[.f{1,6}](Z|+HH:MM)`, every part in range,
/// as microseconds since the epoch in UTC. `MigrationRefused` for anything else.
[[nodiscard]] std::int64_t aware_time(std::string_view value);

class VerificationRequest {
 public:
  /// Checked as the reference checks it, refusing (`MigrationRefused`) in the same order.
  VerificationRequest(std::string request_id, std::string project_id, std::string model_version,
                      std::int64_t map_version, std::string map_fingerprint, std::string group,
                      std::string source_engine, std::string source_id,
                      std::vector<std::pair<std::string, std::string>> targets,
                      std::string requested_at, bool requires_signature);

  /// The request as the control plane wrote it: exactly its eleven fields, protocol 1.
  [[nodiscard]] static VerificationRequest from_record(const Json& record);

  [[nodiscard]] const std::string& request_id() const noexcept { return request_id_; }
  [[nodiscard]] const std::string& project_id() const noexcept { return project_id_; }
  [[nodiscard]] const std::string& model_version() const noexcept { return model_version_; }
  [[nodiscard]] std::int64_t map_version() const noexcept { return map_version_; }
  [[nodiscard]] const std::string& map_fingerprint() const noexcept { return map_fingerprint_; }
  [[nodiscard]] const std::string& group() const noexcept { return group_; }
  [[nodiscard]] const std::string& source_engine() const noexcept { return source_engine_; }
  [[nodiscard]] const std::string& source_id() const noexcept { return source_id_; }
  /// `(engine, id)`, sorted.
  [[nodiscard]] const std::vector<std::pair<std::string, std::string>>& targets() const noexcept {
    return targets_;
  }
  [[nodiscard]] const std::string& requested_at() const noexcept { return requested_at_; }
  [[nodiscard]] bool requires_signature() const noexcept { return requires_signature_; }

  [[nodiscard]] Json as_record() const;

  /// Refuses a request for another project or group, or one that does not describe this session's
  /// model, map, source and targets exactly. Nothing is compared after a refusal.
  void check_session(const PlacementMap& placement, const std::optional<std::string>& project_id,
                     const std::string& group) const;
  /// Refuses a comparison taken before its request was made.
  void check_time(const std::string& at) const;

  friend bool operator==(const VerificationRequest&, const VerificationRequest&) = default;

 private:
  std::string request_id_;
  std::string project_id_;
  std::string model_version_;
  std::int64_t map_version_ = 0;
  std::string map_fingerprint_;
  std::string group_;
  std::string source_engine_;
  std::string source_id_;
  std::vector<std::pair<std::string, std::string>> targets_;
  std::string requested_at_;
  bool requires_signature_ = false;
};

/// The request a session's map would answer for one group: what the control plane persists before
/// a comparison, and what a verifier checks the one it was given against.
[[nodiscard]] VerificationRequest verification_request(const PlacementMap& placement,
                                                       const std::string& group,
                                                       const std::string& project_id,
                                                       const std::string& request_id,
                                                       const std::string& requested_at);

}  // namespace sde
