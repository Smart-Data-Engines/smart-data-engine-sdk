/// Known answers for the three primitives, from their own standards rather than from this library:
/// FIPS 180-2 for SHA-256, RFC 4231 for HMAC-SHA256 and RFC 8032 section 7.1 for Ed25519.

#include <gtest/gtest.h>

#include "crypto.hpp"
#include "encoding.hpp"
#include "sde/canonical.hpp"

namespace {

using sde::detail::ed25519_verify;
using sde::detail::hex_decode;
using sde::detail::hmac_sha256;
using sde::detail::sha256;
using sde::detail::to_hex;

std::string unhex(std::string_view text) { return *hex_decode(text); }

TEST(Sha256, KnownAnswers) {
  EXPECT_EQ(to_hex(sha256("abc")),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
  EXPECT_EQ(to_hex(sha256("")),
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855");
  EXPECT_EQ(sde::digest16("abc"), "ba7816bf8f01cfea");
}

TEST(HmacSha256, Rfc4231Cases) {
  // Test case 1: a 20-byte key of 0x0b.
  EXPECT_EQ(to_hex(hmac_sha256(std::string(20, '\x0b'), "Hi There")),
            "b0344c61d8db38535ca8afceaf0bf12b881dc200c9833da726e9376c2e32cff7");
  // Test case 2: a key shorter than the block.
  EXPECT_EQ(to_hex(hmac_sha256("Jefe", "what do ya want for nothing?")),
            "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843");
  // Test case 6: a key longer than the block is hashed first.
  EXPECT_EQ(to_hex(hmac_sha256(std::string(131, '\xaa'),
                               "Test Using Larger Than Block-Size Key - Hash Key First")),
            "60e431591ee0b67f0d8a26aacbf5b77f8e0bc6213728c5140546040f0ee37f54");
}

TEST(HmacSha256, AnEmptyKeyIsAKeyOfNoBytes) {
  // RFC 2104 pads the key to the block size, so an empty key and a block of zero bytes agree.
  EXPECT_EQ(hmac_sha256("", "message"), hmac_sha256(std::string(64, '\0'), "message"));
}

TEST(Ed25519, Rfc8032TestOneVerifiesAndATamperedOneDoesNot) {
  const std::string key =
      unhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a");
  const std::string signature = unhex(
      "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b4"
      "6bd25bf5f0595bbe24655141438e7a100b");
  EXPECT_TRUE(ed25519_verify(key, "", signature));
  EXPECT_FALSE(ed25519_verify(key, "x", signature));
  std::string flipped = signature;
  flipped[0] = static_cast<char>(flipped[0] ^ 1);
  EXPECT_FALSE(ed25519_verify(key, "", flipped));
}

TEST(Ed25519, WrongLengthsAreAnAnswerNotAnError) {
  const std::string key(32, '\x01');
  EXPECT_FALSE(ed25519_verify(key.substr(0, 31), "m", std::string(64, '\0')));
  EXPECT_FALSE(ed25519_verify(key, "m", std::string(63, '\0')));
}

}  // namespace
