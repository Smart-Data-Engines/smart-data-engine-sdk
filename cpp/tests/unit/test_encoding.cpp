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

TEST(Hex, RoundTripsAndRefusesOddInput) {
  EXPECT_EQ(hex_encode(std::string("\x00\xff\x10", 3)), "00ff10");
  EXPECT_EQ(*hex_decode("00FF10"), std::string("\x00\xff\x10", 3));
  EXPECT_FALSE(hex_decode("abc"));
  EXPECT_FALSE(hex_decode("zz"));
}

}  // namespace
