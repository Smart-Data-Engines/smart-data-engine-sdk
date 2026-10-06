#include <gtest/gtest.h>

#include "encoding.hpp"

namespace {

using sde::detail::base64_decode;
using sde::detail::base64_encode;
using sde::detail::hex_decode;
using sde::detail::hex_encode;

TEST(Base64, RoundTripsRfc4648Examples) {
  const std::pair<std::string, std::string> examples[] = {
      {"", ""},         {"f", "Zg=="},         {"fo", "Zm8="},         {"foo", "Zm9v"},
      {"foob", "Zm9vYg=="}, {"fooba", "Zm9vYmE="}, {"foobar", "Zm9vYmFy"}};
  for (const auto& [plain, encoded] : examples) {
    EXPECT_EQ(base64_encode(plain), encoded);
    ASSERT_TRUE(base64_decode(encoded).has_value()) << encoded;
    EXPECT_EQ(*base64_decode(encoded), plain);
  }
}

TEST(Base64, StrictDecodingRefusesWhatIsNotBase64) {
  EXPECT_FALSE(base64_decode("Zg="));       // length not a multiple of four
  EXPECT_FALSE(base64_decode("Z==="));      // padding in the second place
  EXPECT_FALSE(base64_decode("Zg==Zm9v"));  // padding before the end
  EXPECT_FALSE(base64_decode("Zm9v\n"));    // whitespace
  EXPECT_FALSE(base64_decode("Zm-v"));      // the URL-safe alphabet
  EXPECT_FALSE(base64_decode("Zh=="));      // nonzero bits under the padding
}

// How the reference decodes a signature (Python 3.12, `b64decode(value, validate=True)`); compared
// with it on every string of up to six characters over a hostile alphabet and on a hundred thousand
// long ones. These are the rules worth naming.
TEST(Base64AsPython, AcceptsWhatTheReferenceAccepts) {
  using sde::detail::base64_decode_as_python;
  EXPECT_EQ(*base64_decode_as_python(""), "");
  EXPECT_EQ(*base64_decode_as_python("Zm9v"), "foo");
  EXPECT_EQ(*base64_decode_as_python("Zg=="), "f");
  EXPECT_EQ(*base64_decode_as_python("Zm8="), "fo");
  EXPECT_EQ(*base64_decode_as_python("Zm9v=="), "foo");  // padding after a whole quantum
  EXPECT_EQ(*base64_decode_as_python("Zm9v="), "foo");
  EXPECT_EQ(*base64_decode_as_python("Zh=="), "f");      // nonzero bits under the padding
}

TEST(Base64AsPython, RefusesWhatTheReferenceRefuses) {
  using sde::detail::base64_decode_as_python;
  EXPECT_FALSE(base64_decode_as_python("Zg"));        // the padding is missing
  EXPECT_FALSE(base64_decode_as_python("Zg="));       // half of it is
  EXPECT_FALSE(base64_decode_as_python("Zm9vZ"));     // one sextet too many
  EXPECT_FALSE(base64_decode_as_python("=Zm9"));      // leading padding
  EXPECT_FALSE(base64_decode_as_python("Zg=v"));      // data after a padding character
  // ... even when it completes the quantum, which is the case nothing else refuses.
  EXPECT_FALSE(base64_decode_as_python("Zm=9v"));
  EXPECT_FALSE(base64_decode_as_python("Z=m9v"));
  EXPECT_FALSE(base64_decode_as_python("Zg==Zg=="));  // data after a complete pad sequence
  EXPECT_FALSE(base64_decode_as_python("Zm8=="));     // a pad sequence then more padding
  EXPECT_FALSE(base64_decode_as_python("Zm9v\n"));    // whitespace
  EXPECT_FALSE(base64_decode_as_python("Zm-v"));      // the URL-safe alphabet
  EXPECT_FALSE(base64_decode_as_python("Zm9\xc3\xa9"));  // not ASCII
}

TEST(Hex, RoundTripsAndRefusesOddInput) {
  EXPECT_EQ(hex_encode(std::string("\x00\xff\x10", 3)), "00ff10");
  EXPECT_EQ(*hex_decode("00FF10"), std::string("\x00\xff\x10", 3));
  EXPECT_FALSE(hex_decode("abc"));
  EXPECT_FALSE(hex_decode("zz"));
}

}  // namespace
