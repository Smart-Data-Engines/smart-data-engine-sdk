#include "sde/unicode.hpp"

#include <cstdlib>
#include <memory>

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

int compare_code_points(std::string_view left, std::string_view right) noexcept {
  const int order = left.compare(right);
  return order < 0 ? -1 : (order > 0 ? 1 : 0);
}

}  // namespace sde
