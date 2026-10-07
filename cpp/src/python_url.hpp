#pragma once

/// Python's URL parsing, as the reference's connection-string parsers lean on it: `urlsplit` and the
/// parts of its result, `unquote`, `parse_qsl`, `ipaddress.ip_address`, and `float()` of text. Each
/// refusal Python makes with its own `ValueError` is a `PythonValueError` here, for the caller to
/// report in its own words. Input is UTF-8, which the caller checks: a Python `str` can hold
/// nothing else.

#include <array>
#include <cstdint>
#include <optional>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

namespace sde::detail::python_url {

/// A `ValueError` of Python's parsers.
struct PythonValueError {};

bool hex_digit(char c) noexcept;
int hex_value(char c) noexcept;
bool ascii(std::string_view text) noexcept;
/// ASCII's lowercase, which is what `urlsplit` gives a scheme.
std::string lower(std::string_view text);

/// `urlsplit(url)` of Python 3.12 and later: leading C0 controls and spaces stripped, tabs and line
/// breaks removed wherever they are, then the scheme, the authority - with its bracket and NFKC
/// checks - the fragment and the query.
struct Split {
  std::string scheme;
  std::string netloc;
  std::string path;
  std::string query;
  std::string fragment;
};
[[nodiscard]] Split urlsplit(std::string_view url);

/// `SplitResult.hostname`: lowercase in Python's full sense, the zone of a scoped address kept as
/// written; nothing when the authority names no host.
[[nodiscard]] std::optional<std::string> hostname(std::string_view netloc);
/// `SplitResult.port`: nothing when absent; `PythonValueError` for anything but ASCII digits, or a
/// number past 65535.
[[nodiscard]] std::optional<int> port(std::string_view netloc);
/// `SplitResult.username` and `.password`, still percent-encoded.
struct UserInfo {
  std::optional<std::string> username;
  std::optional<std::string> password;
};
[[nodiscard]] UserInfo userinfo(std::string_view netloc);

/// `unquote_to_bytes`: each `%XX` its byte, everything else as it is.
[[nodiscard]] std::string unquote_to_bytes(std::string_view text);
/// `unquote(text, errors=...)`, which decodes each run of ASCII characters - escapes included - on
/// its own: `strict` refuses (`PythonValueError`) a run whose bytes are not UTF-8, `replace` puts
/// one U+FFFD for each maximal ill-formed part, as CPython's decoder does.
enum class Errors { strict, replace };
[[nodiscard]] std::string unquote(std::string_view text, Errors errors);

/// `parse_qsl(query, keep_blank_values, strict_parsing, errors)`, `&` the separator and `+` a space.
[[nodiscard]] std::vector<std::pair<std::string, std::string>> parse_qsl(std::string_view query,
                                                                       bool keep_blank_values,
                                                                       bool strict_parsing,
                                                                       Errors errors);

/// `IPv4Address`: four decimal octets, each 0 to 255, without a leading zero.
[[nodiscard]] std::optional<std::uint32_t> ipv4(std::string_view text);
/// `IPv6Address` without a scope: Python's own algorithm, which is what decides an edge.
[[nodiscard]] std::optional<std::array<std::uint16_t, 8>> ipv6(std::string_view text);
/// An IPv6 address as `ipaddress` writes it: lowercase hextets without leading zeros, the first
/// longest run of two or more zero hextets as `::`.
[[nodiscard]] std::string ipv6_format(const std::array<std::uint16_t, 8>& words);
/// `ipaddress.ip_address`, with a scope as `IPv6Address` takes one.
enum class Address { none, v4, v6 };
[[nodiscard]] Address ip_address(std::string_view text);

/// `float(text)` of a `str`: Python's whitespace around it, an underscore only between digits, any
/// Unicode decimal digit, `inf`, `infinity` and `nan` in any case; an overflow is an infinity and
/// an underflow zero, as there. `PythonValueError` otherwise.
[[nodiscard]] double float_of(std::string_view text);

}  // namespace sde::detail::python_url
