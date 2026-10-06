#pragma once

/// The three primitives the contract needs, all from OpenSSL: SHA-256 (sections 2 and 7b),
/// HMAC-SHA256 (section 2a) and Ed25519 verification (section 7, Signing). Writing any of them by
/// hand in a public library would be cryptographic code to maintain for no gain.

#include <array>
#include <cstdint>
#include <span>
#include <string>
#include <string_view>

namespace sde::detail {

using Digest = std::array<std::uint8_t, 32>;

[[nodiscard]] Digest sha256(std::string_view data);
[[nodiscard]] Digest hmac_sha256(std::string_view key, std::string_view message);

/// Whether `signature` is a valid Ed25519 signature of `message` under the 32-byte public key.
/// False for anything that is not, a signature of the wrong length included; never throws for a
/// bad signature, because "does not verify" is an answer, not an error.
[[nodiscard]] bool ed25519_verify(std::string_view public_key, std::string_view message,
                                  std::string_view signature);

[[nodiscard]] std::string to_hex(std::span<const std::uint8_t> bytes);

}  // namespace sde::detail
