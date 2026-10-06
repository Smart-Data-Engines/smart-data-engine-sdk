#include "crypto.hpp"

#include <memory>

#include <openssl/evp.h>
#include <openssl/hmac.h>

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
