#include "encoding.hpp"

#include <array>
#include <cstdint>

namespace sde::detail {

namespace {

constexpr std::string_view kAlphabet =
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

int sextet(char c) {
  if (c >= 'A' && c <= 'Z') return c - 'A';
  if (c >= 'a' && c <= 'z') return c - 'a' + 26;
  if (c >= '0' && c <= '9') return c - '0' + 52;
  if (c == '+') return 62;
  if (c == '/') return 63;
  return -1;
}

int nibble(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  return -1;
}

}  // namespace

std::optional<std::string> base64_decode(std::string_view text) {
  if (text.size() % 4 != 0) return std::nullopt;
  std::string out;
  out.reserve(text.size() / 4 * 3);
  for (std::size_t i = 0; i < text.size(); i += 4) {
    const bool last = i + 4 == text.size();
    std::array<int, 4> values{};
    int padding = 0;
    for (std::size_t j = 0; j < 4; ++j) {
      const char c = text[i + j];
      if (c == '=') {
        // Padding only at the end of the last quantum, and never in its first two places.
        if (!last || j < 2) return std::nullopt;
        ++padding;
        values[j] = 0;
        continue;
      }
      if (padding > 0) return std::nullopt;  // data after padding
      values[j] = sextet(c);
      if (values[j] < 0) return std::nullopt;
    }
    const auto group = (static_cast<std::uint32_t>(values[0]) << 18U) |
                       (static_cast<std::uint32_t>(values[1]) << 12U) |
                       (static_cast<std::uint32_t>(values[2]) << 6U) |
                       static_cast<std::uint32_t>(values[3]);
    out.push_back(static_cast<char>((group >> 16U) & 0xFFU));
    if (padding < 2) out.push_back(static_cast<char>((group >> 8U) & 0xFFU));
    if (padding < 1) out.push_back(static_cast<char>(group & 0xFFU));
    // Bits the padding drops must be zero, or two texts would decode to one value.
    if (padding == 2 && (group & 0xFFFFU) != 0) return std::nullopt;
    if (padding == 1 && (group & 0xFFU) != 0) return std::nullopt;
  }
  return out;
}

std::string base64_encode(std::string_view bytes) {
  std::string out;
  out.reserve((bytes.size() + 2) / 3 * 4);
  std::size_t i = 0;
  for (; i + 3 <= bytes.size(); i += 3) {
    const auto group = (static_cast<std::uint32_t>(static_cast<unsigned char>(bytes[i])) << 16U) |
                       (static_cast<std::uint32_t>(static_cast<unsigned char>(bytes[i + 1])) << 8U) |
                       static_cast<std::uint32_t>(static_cast<unsigned char>(bytes[i + 2]));
    out.push_back(kAlphabet[(group >> 18U) & 0x3FU]);
    out.push_back(kAlphabet[(group >> 12U) & 0x3FU]);
    out.push_back(kAlphabet[(group >> 6U) & 0x3FU]);
    out.push_back(kAlphabet[group & 0x3FU]);
  }
  const std::size_t rest = bytes.size() - i;
  if (rest > 0) {
    std::uint32_t group = static_cast<std::uint32_t>(static_cast<unsigned char>(bytes[i])) << 16U;
    if (rest == 2) group |= static_cast<std::uint32_t>(static_cast<unsigned char>(bytes[i + 1])) << 8U;
    out.push_back(kAlphabet[(group >> 18U) & 0x3FU]);
    out.push_back(kAlphabet[(group >> 12U) & 0x3FU]);
    out.push_back(rest == 2 ? kAlphabet[(group >> 6U) & 0x3FU] : '=');
    out.push_back('=');
  }
  return out;
}

std::string hex_encode(std::string_view bytes) {
  static constexpr char kHex[] = "0123456789abcdef";
  std::string out;
  out.reserve(bytes.size() * 2);
  for (const char c : bytes) {
    const auto byte = static_cast<unsigned char>(c);
    out.push_back(kHex[byte >> 4U]);
    out.push_back(kHex[byte & 0x0FU]);
  }
  return out;
}

std::optional<std::string> hex_decode(std::string_view text) {
  if (text.size() % 2 != 0) return std::nullopt;
  std::string out;
  out.reserve(text.size() / 2);
  for (std::size_t i = 0; i < text.size(); i += 2) {
    const int high = nibble(text[i]);
    const int low = nibble(text[i + 1]);
    if (high < 0 || low < 0) return std::nullopt;
    out.push_back(static_cast<char>((high << 4) | low));
  }
  return out;
}

}  // namespace sde::detail
