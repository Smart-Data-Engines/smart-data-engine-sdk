#pragma once

#include <functional>
#include <optional>
#include <string>

#include "sde/json.hpp"
#include "sde/placement.hpp"

namespace sde::detail {

/// Verifies an Ed25519 signature block (`{"alg", "value", "key_id"?}`) against the caller's keys,
/// and reports the name of the key that verified (nothing for a bare key). Shared by placement maps
/// and the signed packets that carry them, so there is one rule for a signature.
///
/// The payload is produced only after the block and the keys are checked, which is the reference's
/// order: whatever refusal the producer raises (a payload with no canonical form) comes after the
/// refusals of a malformed block or an unusable key set. `MapError` when no key verifies, naming
/// the keys tried and the one the document claims.
[[nodiscard]] std::optional<std::string> verify_payload(
    const Json& signature, const PublicKeys& public_keys,
    const std::function<std::string()>& payload_of);

}  // namespace sde::detail
