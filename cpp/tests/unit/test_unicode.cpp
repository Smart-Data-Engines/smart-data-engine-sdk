#include <gtest/gtest.h>

#include "sde/errors.hpp"
#include "sde/unicode.hpp"

namespace {

TEST(Nfc, ComposesAndLeavesAsciiAlone) {
  EXPECT_EQ(sde::nfc("e\xcc\x81"), "\xc3\xa9");
  EXPECT_EQ(sde::nfc("plain"), "plain");
  EXPECT_EQ(sde::nfc(std::string("a\0b", 3)), std::string("a\0b", 3));
  // Hangul syllables compose algorithmically: U+1100 U+1161 is U+AC00.
  EXPECT_EQ(sde::nfc("\xe1\x84\x80\xe1\x85\xa1"), "\xea\xb0\x80");
  EXPECT_TRUE(sde::is_nfc("\xc3\xa9"));
  EXPECT_FALSE(sde::is_nfc("e\xcc\x81"));
}

TEST(Nfc, RefusesBytesThatAreNotText) {
  EXPECT_THROW((void)sde::nfc("\xed\xa0\x80"), sde::CanonicalError);  // a surrogate
  EXPECT_THROW((void)sde::nfc("\xc3"), sde::CanonicalError);          // truncated
  EXPECT_FALSE(sde::is_nfc("\xff"));
}

TEST(ScalarText, RefusesEveryMalformation) {
  EXPECT_TRUE(sde::is_scalar_text("a\xc3\xa9\xe2\x82\xac\xf0\x9f\x98\x80"));
  for (const char* bad : {"\xc0\x80", "\xe0\x80\x80", "\xed\xa0\x80", "\xf4\x90\x80\x80",
                          "\x80", "\xc3", "\xf8\x88\x80\x80\x80"}) {
    EXPECT_FALSE(sde::is_scalar_text(bad)) << bad;
  }
}

TEST(CodePointOrder, IsNotUtf16Order) {
  // U+1F600 sorts after U+E000 by code point; UTF-16 code units would put it before.
  EXPECT_LT(sde::compare_code_points("\xee\x80\x80", "\xf0\x9f\x98\x80"), 0);
  EXPECT_LT(sde::compare_code_points("a", "\xee\x80\x80"), 0);
  EXPECT_EQ(sde::compare_code_points("x", "x"), 0);
  EXPECT_LT(sde::compare_code_points("", "a"), 0);
}

}  // namespace
