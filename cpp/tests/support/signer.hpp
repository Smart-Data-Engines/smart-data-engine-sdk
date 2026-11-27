#pragma once

/// Signing for the tests: a map or a packet signed as the control plane signs it, with a key made
/// from a fixed seed. Shared by the unit and live tests, so every signed document in them is
/// produced by one piece of code.

#include <initializer_list>
#include <memory>
#include <string>
#include <string_view>

#include <gtest/gtest.h>
#include <openssl/bn.h>
#include <openssl/core_names.h>
#include <openssl/ec.h>
#include <openssl/ecdsa.h>
#include <openssl/evp.h>

#include "crypto.hpp"
#include "encoding.hpp"
#include "sde/canonical.hpp"
#include "sde/json.hpp"
#include "sde/placement.hpp"

namespace sde::testing_support {

/// An Ed25519 key for a test, from a fixed seed, or a P-256 key for ES256 (`es256`), drawn fresh:
/// the library only verifies, so the signing is OpenSSL's, called directly.
class Signer {
 public:
  explicit Signer(std::string_view seed_text, bool es256 = false) : es256_(es256) {
    if (es256_) {
      key_.reset(EVP_PKEY_Q_keygen(nullptr, nullptr, "EC", "P-256"));
      return;
    }
    const sde::detail::Digest seed = sde::detail::sha256(seed_text);
    key_.reset(EVP_PKEY_new_raw_private_key(EVP_PKEY_ED25519, nullptr, seed.data(), seed.size()));
  }

  /// 32 bytes of Ed25519, or the 65 of an uncompressed P-256 point.
  [[nodiscard]] std::string public_key() const {
    unsigned char raw[65];
    std::size_t length = sizeof raw;
    if (es256_) {
      EXPECT_EQ(EVP_PKEY_get_octet_string_param(key_.get(), OSSL_PKEY_PARAM_ENCODED_PUBLIC_KEY, raw,
                                                sizeof raw, &length),
                1);
    } else {
      EXPECT_EQ(EVP_PKEY_get_raw_public_key(key_.get(), raw, &length), 1);
    }
    return {reinterpret_cast<const char*>(raw), length};
  }

  /// An Ed25519 signature, or ES256's r and s of 32 bytes each with s in the lower half.
  [[nodiscard]] std::string sign(std::string_view message) const {
    const std::unique_ptr<EVP_MD_CTX, decltype(&EVP_MD_CTX_free)> context(EVP_MD_CTX_new(),
                                                                          &EVP_MD_CTX_free);
    unsigned char signature[80];
    std::size_t length = sizeof signature;
    EXPECT_EQ(EVP_DigestSignInit(context.get(), nullptr, es256_ ? EVP_sha256() : nullptr, nullptr,
                                 key_.get()),
              1);
    const auto* bytes = reinterpret_cast<const unsigned char*>(message.data());
    EXPECT_EQ(EVP_DigestSign(context.get(), signature, &length, bytes, message.size()), 1);
    if (!es256_) return {reinterpret_cast<const char*>(signature), length};
    const unsigned char* der = signature;
    const std::unique_ptr<ECDSA_SIG, decltype(&ECDSA_SIG_free)> parsed(
        d2i_ECDSA_SIG(nullptr, &der, static_cast<long>(length)), &ECDSA_SIG_free);
    const std::unique_ptr<BIGNUM, decltype(&BN_free)> order(BN_new(), &BN_free);
    const std::unique_ptr<BN_CTX, decltype(&BN_CTX_free)> numbers(BN_CTX_new(), &BN_CTX_free);
    const std::unique_ptr<EC_GROUP, decltype(&EC_GROUP_free)> group(
        EC_GROUP_new_by_curve_name(NID_X9_62_prime256v1), &EC_GROUP_free);
    EXPECT_EQ(EC_GROUP_get_order(group.get(), order.get(), numbers.get()), 1);
    const std::unique_ptr<BIGNUM, decltype(&BN_free)> s(BN_dup(ECDSA_SIG_get0_s(parsed.get())),
                                                        &BN_free);
    const std::unique_ptr<BIGNUM, decltype(&BN_free)> half(BN_new(), &BN_free);
    EXPECT_EQ(BN_rshift1(half.get(), order.get()), 1);
    if (BN_cmp(s.get(), half.get()) > 0) {
      EXPECT_EQ(BN_sub(s.get(), order.get(), s.get()), 1);
    }
    unsigned char p1363[64];
    EXPECT_EQ(BN_bn2binpad(ECDSA_SIG_get0_r(parsed.get()), p1363, 32), 32);
    EXPECT_EQ(BN_bn2binpad(s.get(), p1363 + 32, 32), 32);
    return {reinterpret_cast<const char*>(p1363), sizeof p1363};
  }

  [[nodiscard]] sde::PublicKeys keys() const {
    return sde::PublicKeys::named({{"k", public_key()}});
  }

  /// The document signed as the control plane signs it: over its canonical bytes without the block.
  [[nodiscard]] sde::Json signed_document(sde::Json document) const {
    (void)document.erase("signature");
    sde::Json block = sde::Json::object();
    block.set("alg", es256_ ? "es256" : "ed25519");
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
  bool es256_;
  std::unique_ptr<EVP_PKEY, decltype(&EVP_PKEY_free)> key_{nullptr, &EVP_PKEY_free};
};

}  // namespace sde::testing_support
