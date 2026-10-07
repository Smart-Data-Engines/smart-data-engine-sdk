#pragma once

/// Signing for the tests: a map or a packet signed as the control plane signs it, with a key made
/// from a fixed seed. Shared by the unit and live tests, so every signed document in them is
/// produced by one piece of code.

#include <initializer_list>
#include <memory>
#include <string>
#include <string_view>

#include <gtest/gtest.h>
#include <openssl/evp.h>

#include "crypto.hpp"
#include "encoding.hpp"
#include "sde/canonical.hpp"
#include "sde/json.hpp"
#include "sde/placement.hpp"

namespace sde::testing_support {

/// An Ed25519 key for a test, from a fixed seed: the library only verifies, so the signing is
/// OpenSSL's, called directly.
class Signer {
 public:
  explicit Signer(std::string_view seed_text) {
    const sde::detail::Digest seed = sde::detail::sha256(seed_text);
    key_.reset(EVP_PKEY_new_raw_private_key(EVP_PKEY_ED25519, nullptr, seed.data(), seed.size()));
  }

  [[nodiscard]] std::string public_key() const {
    unsigned char raw[32];
    std::size_t length = sizeof raw;
    EXPECT_EQ(EVP_PKEY_get_raw_public_key(key_.get(), raw, &length), 1);
    return {reinterpret_cast<const char*>(raw), length};
  }

  [[nodiscard]] std::string sign(std::string_view message) const {
    const std::unique_ptr<EVP_MD_CTX, decltype(&EVP_MD_CTX_free)> context(EVP_MD_CTX_new(),
                                                                          &EVP_MD_CTX_free);
    unsigned char signature[64];
    std::size_t length = sizeof signature;
    EXPECT_EQ(EVP_DigestSignInit(context.get(), nullptr, nullptr, nullptr, key_.get()), 1);
    const auto* bytes = reinterpret_cast<const unsigned char*>(message.data());
    EXPECT_EQ(EVP_DigestSign(context.get(), signature, &length, bytes, message.size()), 1);
    return {reinterpret_cast<const char*>(signature), length};
  }

  [[nodiscard]] sde::PublicKeys keys() const {
    return sde::PublicKeys::named({{"k", public_key()}});
  }

  /// The document signed as the control plane signs it: over its canonical bytes without the block.
  [[nodiscard]] sde::Json signed_document(sde::Json document) const {
    (void)document.erase("signature");
    sde::Json block = sde::Json::object();
    block.set("alg", "ed25519");
    block.set("key_id", "k");
    block.set("value", sde::detail::base64_encode(sign(sde::canonical_bytes(document))));
    document.set("signature", std::move(block));
    return document;
  }

  /// A packet with each of its maps re-signed, then the packet around them.
  [[nodiscard]] sde::Json resigned(sde::Json packet,
                                   std::initializer_list<std::string_view> maps) const {
    for (std::string_view name : maps) {
      packet.set(std::string(name), signed_document(*packet.find(name)));
    }
    return signed_document(std::move(packet));
  }

 private:
  std::unique_ptr<EVP_PKEY, decltype(&EVP_PKEY_free)> key_{nullptr, &EVP_PKEY_free};
};

}  // namespace sde::testing_support
