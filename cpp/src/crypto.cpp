#include "crypto.hpp"

#include <memory>

#include <openssl/bn.h>
#include <openssl/crypto.h>
#include <openssl/ecdsa.h>
#include <openssl/evp.h>
#include <openssl/hmac.h>
#include <openssl/x509.h>

#include "sde/errors.hpp"

namespace sde::detail {

namespace {

const unsigned char* bytes_of(std::string_view text) {
  return reinterpret_cast<const unsigned char*>(text.data());
}

}  // namespace

Digest sha256(std::string_view data) {
  Digest out{};
  unsigned int length = 0;
  if (EVP_Digest(data.data(), data.size(), out.data(), &length, EVP_sha256(), nullptr) != 1 ||
      length != out.size()) {
    throw SdeError("SHA-256 failed in OpenSSL");
  }
  return out;
}

Digest hmac_sha256(std::string_view key, std::string_view message) {
  Digest out{};
  unsigned int length = 0;
  // HMAC() accepts a null key pointer only with length 0; an empty salt is a key of no bytes.
  static const unsigned char kEmpty = 0;
  const unsigned char* key_bytes = key.empty() ? &kEmpty : bytes_of(key);
  if (HMAC(EVP_sha256(), key_bytes, static_cast<int>(key.size()), bytes_of(message), message.size(),
           out.data(), &length) == nullptr ||
      length != out.size()) {
    throw SdeError("HMAC-SHA256 failed in OpenSSL");
  }
  return out;
}

bool ed25519_verify(std::string_view public_key, std::string_view message,
                    std::string_view signature) {
  if (public_key.size() != 32 || signature.size() != 64) return false;
  std::unique_ptr<EVP_PKEY, decltype(&EVP_PKEY_free)> key(
      EVP_PKEY_new_raw_public_key(EVP_PKEY_ED25519, nullptr, bytes_of(public_key), public_key.size()),
      &EVP_PKEY_free);
  if (!key) return false;
  std::unique_ptr<EVP_MD_CTX, decltype(&EVP_MD_CTX_free)> context(EVP_MD_CTX_new(),
                                                                  &EVP_MD_CTX_free);
  if (!context) throw SdeError("OpenSSL could not allocate a verification context");
  if (EVP_DigestVerifyInit(context.get(), nullptr, nullptr, nullptr, key.get()) != 1) return false;
  return EVP_DigestVerify(context.get(), bytes_of(signature), signature.size(), bytes_of(message),
                          message.size()) == 1;
}

namespace {

/// The SubjectPublicKeyInfo of an uncompressed P-256 point, the point's 65 bytes after it.
constexpr unsigned char kP256Spki[] = {0x30, 0x59, 0x30, 0x13, 0x06, 0x07, 0x2a, 0x86, 0x48,
                                       0xce, 0x3d, 0x02, 0x01, 0x06, 0x08, 0x2a, 0x86, 0x48,
                                       0xce, 0x3d, 0x03, 0x01, 0x07, 0x03, 0x42, 0x00};

std::unique_ptr<EVP_PKEY, decltype(&EVP_PKEY_free)> p256_key(std::string_view point) {
  std::unique_ptr<EVP_PKEY, decltype(&EVP_PKEY_free)> none(nullptr, &EVP_PKEY_free);
  if (point.size() != 65 || static_cast<unsigned char>(point[0]) != 0x04) return none;
  std::string der(reinterpret_cast<const char*>(kP256Spki), sizeof(kP256Spki));
  der.append(point);
  const auto* cursor = reinterpret_cast<const unsigned char*>(der.data());
  // d2i_PUBKEY decodes the point and refuses one that is not on the curve.
  return std::unique_ptr<EVP_PKEY, decltype(&EVP_PKEY_free)>(
      d2i_PUBKEY(nullptr, &cursor, static_cast<long>(der.size())), &EVP_PKEY_free);
}

}  // namespace

bool p256_point(std::string_view point) { return p256_key(point) != nullptr; }

bool es256_verify(std::string_view public_key, std::string_view message,
                  std::string_view signature) {
  if (signature.size() != 64) return false;
  const auto key = p256_key(public_key);
  if (!key) return false;
  std::unique_ptr<ECDSA_SIG, decltype(&ECDSA_SIG_free)> sig(ECDSA_SIG_new(), &ECDSA_SIG_free);
  if (!sig) throw SdeError("OpenSSL could not allocate a signature");
  BIGNUM* r = BN_bin2bn(bytes_of(signature.substr(0, 32)), 32, nullptr);
  BIGNUM* s = BN_bin2bn(bytes_of(signature.substr(32, 32)), 32, nullptr);
  if (r == nullptr || s == nullptr || ECDSA_SIG_set0(sig.get(), r, s) != 1) {
    BN_free(r);
    BN_free(s);
    return false;
  }
  unsigned char* der = nullptr;
  const int size = i2d_ECDSA_SIG(sig.get(), &der);
  if (size <= 0) return false;
  std::unique_ptr<unsigned char, void (*)(unsigned char*)> owned(
      der, [](unsigned char* bytes) { OPENSSL_free(bytes); });
  std::unique_ptr<EVP_MD_CTX, decltype(&EVP_MD_CTX_free)> context(EVP_MD_CTX_new(),
                                                                  &EVP_MD_CTX_free);
  if (!context) throw SdeError("OpenSSL could not allocate a verification context");
  if (EVP_DigestVerifyInit(context.get(), nullptr, EVP_sha256(), nullptr, key.get()) != 1) {
    return false;
  }
  return EVP_DigestVerify(context.get(), der, static_cast<std::size_t>(size), bytes_of(message),
                          message.size()) == 1;
}

std::string to_hex(std::span<const std::uint8_t> bytes) {
  static constexpr char kHex[] = "0123456789abcdef";
  std::string out;
  out.reserve(bytes.size() * 2);
  for (const std::uint8_t byte : bytes) {
    out.push_back(kHex[byte >> 4U]);
    out.push_back(kHex[byte & 0x0FU]);
  }
  return out;
}

}  // namespace sde::detail
