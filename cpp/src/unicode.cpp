#include "sde/unicode.hpp"

#include <cstdint>
#include <cstdlib>
#include <memory>
#include <vector>

#include <utf8proc.h>

#include "sde/errors.hpp"
#include "unicode_internal.hpp"

namespace sde {

namespace detail {

std::size_t utf8_sequence_length(std::string_view text, std::size_t pos) noexcept {
  const std::size_t n = text.size();
  if (pos >= n) return 0;
  const auto b0 = static_cast<unsigned char>(text[pos]);
  if (b0 < 0x80) return 1;
  auto continuation = [&](std::size_t i) {
    return i < n && (static_cast<unsigned char>(text[i]) & 0xC0U) == 0x80U;
  };
  if (b0 >= 0xC2 && b0 <= 0xDF) return continuation(pos + 1) ? 2 : 0;
  if (b0 >= 0xE0 && b0 <= 0xEF) {
    if (!continuation(pos + 1) || !continuation(pos + 2)) return 0;
    const auto b1 = static_cast<unsigned char>(text[pos + 1]);
    if (b0 == 0xE0 && b1 < 0xA0) return 0;   // overlong
    if (b0 == 0xED && b1 >= 0xA0) return 0;  // a surrogate
    return 3;
  }
  if (b0 >= 0xF0 && b0 <= 0xF4) {
    if (!continuation(pos + 1) || !continuation(pos + 2) || !continuation(pos + 3)) return 0;
    const auto b1 = static_cast<unsigned char>(text[pos + 1]);
    if (b0 == 0xF0 && b1 < 0x90) return 0;   // overlong
    if (b0 == 0xF4 && b1 >= 0x90) return 0;  // above U+10FFFF
    return 4;
  }
  return 0;
}

char32_t decode_sequence(std::string_view text, std::size_t pos, std::size_t length) noexcept {
  const auto byte = [&](std::size_t i) { return static_cast<char32_t>(static_cast<unsigned char>(text[pos + i])); };
  switch (length) {
    case 1:
      return byte(0);
    case 2:
      return ((byte(0) & 0x1FU) << 6U) | (byte(1) & 0x3FU);
    case 3:
      return ((byte(0) & 0x0FU) << 12U) | ((byte(1) & 0x3FU) << 6U) | (byte(2) & 0x3FU);
    default:
      return ((byte(0) & 0x07U) << 18U) | ((byte(1) & 0x3FU) << 12U) | ((byte(2) & 0x3FU) << 6U) |
             (byte(3) & 0x3FU);
  }
}

std::optional<std::u32string> decode_utf8(std::string_view text) {
  std::u32string out;
  out.reserve(text.size());
  for (std::size_t pos = 0; pos < text.size();) {
    const std::size_t length = utf8_sequence_length(text, pos);
    if (length == 0) return std::nullopt;
    out.push_back(decode_sequence(text, pos, length));
    pos += length;
  }
  return out;
}

bool is_decimal_digit(char32_t code_point) noexcept {
  if (code_point < 0x80) return code_point >= U'0' && code_point <= U'9';
  return utf8proc_category(static_cast<utf8proc_int32_t>(code_point)) == UTF8PROC_CATEGORY_ND;
}

void append_code_point_unchecked(std::string& out, std::uint32_t cp) {
  if (cp < 0x80) {
    out.push_back(static_cast<char>(cp));
  } else if (cp < 0x800) {
    out.push_back(static_cast<char>(0xC0U | (cp >> 6U)));
    out.push_back(static_cast<char>(0x80U | (cp & 0x3FU)));
  } else if (cp < 0x10000) {
    out.push_back(static_cast<char>(0xE0U | (cp >> 12U)));
    out.push_back(static_cast<char>(0x80U | ((cp >> 6U) & 0x3FU)));
    out.push_back(static_cast<char>(0x80U | (cp & 0x3FU)));
  } else {
    out.push_back(static_cast<char>(0xF0U | (cp >> 18U)));
    out.push_back(static_cast<char>(0x80U | ((cp >> 12U) & 0x3FU)));
    out.push_back(static_cast<char>(0x80U | ((cp >> 6U) & 0x3FU)));
    out.push_back(static_cast<char>(0x80U | (cp & 0x3FU)));
  }
}

}  // namespace detail

bool is_scalar_text(std::string_view text) noexcept {
  std::size_t pos = 0;
  while (pos < text.size()) {
    const std::size_t length = detail::utf8_sequence_length(text, pos);
    if (length == 0) return false;
    pos += length;
  }
  return true;
}

namespace {

bool is_ascii(std::string_view text) noexcept {
  for (const char c : text) {
    if (static_cast<unsigned char>(c) >= 0x80) return false;
  }
  return true;
}

struct FreeDeleter {
  void operator()(utf8proc_uint8_t* pointer) const noexcept { std::free(pointer); }  // NOLINT
};

}  // namespace

std::string nfc(std::string_view text) {
  // ASCII is its own NFC, and nearly every identifier is ASCII: the common case costs a scan.
  if (is_ascii(text)) return std::string(text);
  if (!is_scalar_text(text)) {
    throw CanonicalError(
        "text that is not well-formed UTF-8 of Unicode scalar values has no NFC form; "
        "normalising bytes that are not text would invent characters");
  }
  utf8proc_uint8_t* raw = nullptr;
  const utf8proc_ssize_t length =
      utf8proc_map(reinterpret_cast<const utf8proc_uint8_t*>(text.data()),
                   static_cast<utf8proc_ssize_t>(text.size()), &raw,
                   static_cast<utf8proc_option_t>(UTF8PROC_STABLE | UTF8PROC_COMPOSE));
  std::unique_ptr<utf8proc_uint8_t, FreeDeleter> owned(raw);
  if (length < 0) {
    throw CanonicalError(std::string("NFC normalisation failed: ") +
                         utf8proc_errmsg(length));
  }
  return std::string(reinterpret_cast<const char*>(owned.get()), static_cast<std::size_t>(length));
}

bool is_nfc(std::string_view text) {
  if (!is_scalar_text(text)) return false;
  return nfc(text) == text;
}

namespace {

// Case_Ignorable is the general categories Mn, Me, Cf, Lm and Sk plus the Word_Break classes
// MidLetter, MidNumLet and Single_Quote (DerivedCoreProperties.txt). These are the latter. Probed
// from Python 3.12's `str.lower()`, character by character, rather than copied from a table: it is
// that function the table names have to agree with.
constexpr std::uint32_t kWordBreakIgnorable[] = {0x0027, 0x002E, 0x003A, 0x00B7, 0x0387, 0x055F,
                                                 0x05F4, 0x2018, 0x2019, 0x2024, 0x2027, 0xFE13,
                                                 0xFE52, 0xFE55, 0xFF07, 0xFF0E, 0xFF1A};

// Cased is Ll, Lu and Lt plus Other_Lowercase and Other_Uppercase. Final_Sigma only asks whether a
// character that is not case-ignorable is cased, so these are the members of the two Other_ lists
// that are not modifier letters (Lm, which is case-ignorable). Probed the same way.
struct Range {
  std::uint32_t first;
  std::uint32_t last;
};
constexpr Range kOtherCased[] = {{0x00AA, 0x00AA},   {0x00BA, 0x00BA},   {0x2160, 0x217F},
                                 {0x24B6, 0x24E9},   {0x1F130, 0x1F149}, {0x1F150, 0x1F169},
                                 {0x1F170, 0x1F189}};

bool case_ignorable(std::uint32_t code_point) {
  switch (utf8proc_category(static_cast<utf8proc_int32_t>(code_point))) {
    case UTF8PROC_CATEGORY_MN:
    case UTF8PROC_CATEGORY_ME:
    case UTF8PROC_CATEGORY_CF:
    case UTF8PROC_CATEGORY_LM:
    case UTF8PROC_CATEGORY_SK:
      return true;
    default:
      break;
  }
  for (const std::uint32_t c : kWordBreakIgnorable) {
    if (c == code_point) return true;
  }
  return false;
}

bool cased(std::uint32_t code_point) {
  switch (utf8proc_category(static_cast<utf8proc_int32_t>(code_point))) {
    case UTF8PROC_CATEGORY_LL:
    case UTF8PROC_CATEGORY_LU:
    case UTF8PROC_CATEGORY_LT:
      return true;
    default:
      break;
  }
  for (const Range& range : kOtherCased) {
    if (code_point >= range.first && code_point <= range.last) return true;
  }
  return false;
}

std::vector<std::uint32_t> scalar_code_points(std::string_view text) {
  if (!is_scalar_text(text)) {
    throw CanonicalError(
        "text that is not well-formed UTF-8 of Unicode scalar values has no lowercase; "
        "case-mapping bytes that are not text would invent characters");
  }
  std::vector<std::uint32_t> out;
  std::size_t pos = 0;
  while (pos < text.size()) {
    const std::size_t length = detail::utf8_sequence_length(text, pos);
    utf8proc_int32_t code_point = 0;
    utf8proc_iterate(reinterpret_cast<const utf8proc_uint8_t*>(text.data() + pos),
                     static_cast<utf8proc_ssize_t>(length), &code_point);
    out.push_back(static_cast<std::uint32_t>(code_point));
    pos += length;
  }
  return out;
}

/// Unicode's Final_Sigma condition (chapter 3, table 3-17): a cased letter before the sigma and
/// none after it, case-ignorable characters skipped on both sides.
bool final_sigma(const std::vector<std::uint32_t>& code_points, std::size_t at) {
  bool after_cased = false;
  for (std::size_t j = at; j-- > 0;) {
    if (!case_ignorable(code_points[j])) {
      after_cased = cased(code_points[j]);
      break;
    }
  }
  if (!after_cased) return false;
  for (std::size_t k = at + 1; k < code_points.size(); ++k) {
    if (!case_ignorable(code_points[k])) return !cased(code_points[k]);
  }
  return true;
}

}  // namespace

std::string to_lower(std::string_view text) {
  if (is_ascii(text)) {
    std::string out(text);
    for (char& c : out) {
      if (c >= 'A' && c <= 'Z') c = static_cast<char>(c - 'A' + 'a');
    }
    return out;
  }
  const std::vector<std::uint32_t> code_points = scalar_code_points(text);
  std::string out;
  out.reserve(text.size());
  for (std::size_t i = 0; i < code_points.size(); ++i) {
    const std::uint32_t c = code_points[i];
    if (c == 0x0130) {
      // The one unconditional lowercase mapping SpecialCasing.txt adds to the simple ones: the dot
      // stays, as a combining character.
      detail::append_code_point_unchecked(out, 0x0069);
      detail::append_code_point_unchecked(out, 0x0307);
    } else if (c == 0x03A3) {
      detail::append_code_point_unchecked(out, final_sigma(code_points, i) ? 0x03C2 : 0x03C3);
    } else {
      detail::append_code_point_unchecked(
          out, static_cast<std::uint32_t>(utf8proc_tolower(static_cast<utf8proc_int32_t>(c))));
    }
  }
  return out;
}

int compare_code_points(std::string_view left, std::string_view right) noexcept {
  const int order = left.compare(right);
  return order < 0 ? -1 : (order > 0 ? 1 : 0);
}

}  // namespace sde
