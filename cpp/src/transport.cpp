#include "transport.hpp"

#include <arpa/inet.h>

#include <algorithm>
#include <array>
#include <cctype>
#include <cstdint>

namespace sde::detail {

bool loopback(const std::string& host) {
  std::string name = host;
  if (name.size() >= 2 && name.front() == '[' && name.back() == ']') {
    name = name.substr(1, name.size() - 2);
  }
  std::string lower = name;
  std::transform(lower.begin(), lower.end(), lower.begin(),
                 [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
  if (lower == "localhost") return true;
  std::array<std::uint8_t, 4> v4{};
  if (inet_pton(AF_INET, name.c_str(), v4.data()) == 1) return v4[0] == 127;
  std::array<std::uint8_t, 16> v6{};
  if (inet_pton(AF_INET6, name.c_str(), v6.data()) != 1) return false;
  const bool leading_zero = std::all_of(v6.begin(), v6.begin() + 10, [](std::uint8_t b) { return b == 0; });
  if (leading_zero && v6[10] == 0 && v6[11] == 0 && v6[12] == 0 && v6[13] == 0 && v6[14] == 0 &&
      v6[15] == 1) {
    return true;  // ::1
  }
  return leading_zero && v6[10] == 0xff && v6[11] == 0xff && v6[12] == 127;  // ::ffff:127.x.y.z
}

}  // namespace sde::detail
