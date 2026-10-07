#include "python_url.hpp"

#include <algorithm>
#include <charconv>
#include <cmath>
#include <cstdio>
#include <limits>
#include <system_error>

#include "python_compat.hpp"
#include "sde/unicode.hpp"
#include "unicode_internal.hpp"

namespace sde::detail::python_url {

bool hex_digit(char c) noexcept {
  return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F');
}

int hex_value(char c) noexcept {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  return c - 'A' + 10;
}

bool ascii(std::string_view text) noexcept {
  for (const char c : text) {
    if (static_cast<unsigned char>(c) > 0x7F) return false;
  }
  return true;
}

std::string lower(std::string_view text) {
  std::string out(text);
  for (char& c : out) {
    if (c >= 'A' && c <= 'Z') c = static_cast<char>(c - 'A' + 'a');
  }
  return out;
}

namespace {

bool in(unsigned char byte, unsigned char low, unsigned char high) noexcept {
  return byte >= low && byte <= high;
}

/// The length of the maximal ill-formed subpart at `pos`: the longest prefix of a well-formed
/// sequence that starts there, at least one byte - what one U+FFFD stands for.
std::size_t ill_formed_subpart(std::string_view bytes, std::size_t pos) noexcept {
  const auto lead = static_cast<unsigned char>(bytes[pos]);
  // The ranges of the second byte and the number of continuation bytes, per Unicode's Table 3-7.
  unsigned char low = 0x80;
  unsigned char high = 0xBF;
  std::size_t needed = 0;
  if (in(lead, 0xC2, 0xDF)) {
    needed = 1;
  } else if (lead == 0xE0) {
    needed = 2;
    low = 0xA0;
  } else if (in(lead, 0xE1, 0xEC) || in(lead, 0xEE, 0xEF)) {
    needed = 2;
  } else if (lead == 0xED) {
    needed = 2;
    high = 0x9F;
  } else if (lead == 0xF0) {
    needed = 3;
    low = 0x90;
  } else if (in(lead, 0xF1, 0xF3)) {
    needed = 3;
  } else if (lead == 0xF4) {
    needed = 3;
    high = 0x8F;
  } else {
    return 1;
  }
  std::size_t length = 1;
  for (std::size_t k = 0; k < needed && pos + length < bytes.size(); ++k) {
    const auto next = static_cast<unsigned char>(bytes[pos + length]);
    if (k == 0 ? !in(next, low, high) : !in(next, 0x80, 0xBF)) break;
    ++length;
  }
  return length;
}

/// CPython's UTF-8 decoder with `errors="replace"`.
std::string decode_replacing(std::string_view bytes) {
  std::string out;
  std::size_t i = 0;
  while (i < bytes.size()) {
    const std::size_t length = utf8_sequence_length(bytes, i);
    if (length != 0) {
      out.append(bytes.substr(i, length));
      i += length;
      continue;
    }
    i += ill_formed_subpart(bytes, i);
    out += "\xEF\xBF\xBD";
  }
  return out;
}

/// `_check_bracketed_host`: an IPvFuture literal, or an IPv6 address - never an IPv4 one.
void check_bracketed_host(std::string_view host) {
  if (!host.empty() && host[0] == 'v') {
    // \Av[a-fA-F0-9]+\..+\Z
    std::size_t i = 1;
    while (i < host.size() && hex_digit(host[i])) ++i;
    if (i == 1 || i >= host.size() || host[i] != '.' || i + 1 >= host.size()) {
      throw PythonValueError{};
    }
    return;
  }
  if (ip_address(host) != Address::v6) throw PythonValueError{};
}

/// `_check_bracketed_netloc`: nothing before the bracket, nothing between it and the port, and the
/// host a bracketed one - mirroring how `hostname` splits the authority.
void check_bracketed_netloc(std::string_view netloc) {
  const std::size_t at = netloc.rfind('@');
  const std::string_view authority = at == std::string_view::npos ? netloc : netloc.substr(at + 1);
  const std::size_t open = authority.find('[');
  std::string_view host;
  if (open != std::string_view::npos) {
    if (open != 0) throw PythonValueError{};
    const std::string_view bracketed = authority.substr(open + 1);
    const std::size_t close = bracketed.find(']');
    host = bracketed.substr(0, close);
    const std::string_view after =
        close == std::string_view::npos ? std::string_view{} : bracketed.substr(close + 1);
    if (!after.empty() && after[0] != ':') throw PythonValueError{};
  } else {
    host = authority.substr(0, authority.find(':'));
  }
  check_bracketed_host(host);
}

/// `_checknetloc`: a host whose NFKC form spells a delimiter (`℀` is `a/c`) is refused.
void check_netloc(std::string_view netloc) {
  if (netloc.empty() || ascii(netloc)) return;
  std::string kept;
  for (const char c : netloc) {
    if (c != '@' && c != ':' && c != '#' && c != '?') kept += c;
  }
  const std::string normalised = nfkc(kept);
  if (normalised == kept) return;
  if (normalised.find_first_of("/?#@:") != std::string::npos) throw PythonValueError{};
}

bool scheme_character(char c) noexcept {
  return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') || c == '+' ||
         c == '-' || c == '.';
}

/// `_hostinfo`: the host part and the port text of an authority.
std::pair<std::string, std::optional<std::string>> hostinfo(std::string_view netloc) {
  const std::size_t at = netloc.rfind('@');
  const std::string_view info = at == std::string_view::npos ? netloc : netloc.substr(at + 1);
  std::string_view name;
  std::string_view port;
  if (const std::size_t open = info.find('['); open != std::string_view::npos) {
    const std::string_view bracketed = info.substr(open + 1);
    const std::size_t close = bracketed.find(']');
    name = bracketed.substr(0, close);
    const std::string_view rest =
        close == std::string_view::npos ? std::string_view{} : bracketed.substr(close + 1);
    const std::size_t colon = rest.find(':');
    port = colon == std::string_view::npos ? std::string_view{} : rest.substr(colon + 1);
  } else {
    const std::size_t colon = info.find(':');
    name = info.substr(0, colon);
    port = colon == std::string_view::npos ? std::string_view{} : info.substr(colon + 1);
  }
  return {std::string(name),
          port.empty() ? std::nullopt : std::optional<std::string>(std::string(port))};
}

/// A decimal digit's value: Unicode's digits come in runs of ten, from zero.
int digit_value(char32_t code_point) noexcept {
  char32_t start = code_point;
  while (start > 0 && is_decimal_digit(start - 1)) --start;
  return static_cast<int>((code_point - start) % 10);
}

}  // namespace

Split urlsplit(std::string_view url) {
  std::size_t first = 0;
  while (first < url.size() && static_cast<unsigned char>(url[first]) <= 0x20) ++first;
  std::string text;
  for (const char c : url.substr(first)) {
    if (c != '\t' && c != '\r' && c != '\n') text += c;
  }
  Split out;
  std::string_view rest = text;
  const std::size_t colon = rest.find(':');
  if (colon != std::string_view::npos && colon > 0 &&
      ((rest[0] >= 'a' && rest[0] <= 'z') || (rest[0] >= 'A' && rest[0] <= 'Z'))) {
    bool valid = true;
    for (std::size_t i = 0; i < colon; ++i) valid = valid && scheme_character(rest[i]);
    if (valid) {
      out.scheme = lower(rest.substr(0, colon));
      rest.remove_prefix(colon + 1);
    }
  }
  if (rest.substr(0, 2) == "//") {
    std::size_t end = rest.size();
    for (const char delimiter : {'/', '?', '#'}) {
      const std::size_t at = rest.find(delimiter, 2);
      if (at != std::string_view::npos) end = std::min(end, at);
    }
    out.netloc = std::string(rest.substr(2, end - 2));
    rest.remove_prefix(end);
    const bool open = out.netloc.find('[') != std::string::npos;
    const bool close = out.netloc.find(']') != std::string::npos;
    if (open != close) throw PythonValueError{};
    if (open && close) check_bracketed_netloc(out.netloc);
  }
  if (const std::size_t hash = rest.find('#'); hash != std::string_view::npos) {
    out.fragment = std::string(rest.substr(hash + 1));
    rest = rest.substr(0, hash);
  }
  if (const std::size_t question = rest.find('?'); question != std::string_view::npos) {
    out.query = std::string(rest.substr(question + 1));
    rest = rest.substr(0, question);
  }
  out.path = std::string(rest);
  check_netloc(out.netloc);
  return out;
}

std::optional<std::string> hostname(std::string_view netloc) {
  const std::string name = hostinfo(netloc).first;
  if (name.empty()) return std::nullopt;
  // Python's full lowercase, not ASCII's: the Kelvin sign is a `k` to it, and so a host.
  const std::size_t percent = name.find('%');
  if (percent == std::string::npos) return to_lower(name);
  return to_lower(name.substr(0, percent)) + name.substr(percent);
}

std::optional<int> port(std::string_view netloc) {
  const std::optional<std::string> text = hostinfo(netloc).second;
  if (!text) return std::nullopt;
  std::size_t first = 0;
  for (const char c : *text) {
    if (c < '0' || c > '9') throw PythonValueError{};
  }
  while (first + 1 < text->size() && (*text)[first] == '0') ++first;
  const std::string_view digits = std::string_view(*text).substr(first);
  if (digits.size() > 5) throw PythonValueError{};
  int value = 0;
  for (const char c : digits) value = value * 10 + (c - '0');
  if (value > 65535) throw PythonValueError{};
  return value;
}

UserInfo userinfo(std::string_view netloc) {
  const std::size_t at = netloc.rfind('@');
  if (at == std::string_view::npos) return {};
  const std::string_view info = netloc.substr(0, at);
  const std::size_t colon = info.find(':');
  if (colon == std::string_view::npos) return {std::string(info), std::nullopt};
  return {std::string(info.substr(0, colon)), std::string(info.substr(colon + 1))};
}

std::string unquote_to_bytes(std::string_view text) {
  std::string out;
  for (std::size_t i = 0; i < text.size(); ++i) {
    if (text[i] == '%' && i + 2 < text.size() && hex_digit(text[i + 1]) && hex_digit(text[i + 2])) {
      out += static_cast<char>(hex_value(text[i + 1]) * 16 + hex_value(text[i + 2]));
      i += 2;
    } else {
      out += text[i];
    }
  }
  return out;
}

std::string unquote(std::string_view text, Errors errors) {
  std::string out;
  std::size_t i = 0;
  while (i < text.size()) {
    std::size_t j = i;
    const bool run_ascii = static_cast<unsigned char>(text[i]) <= 0x7F;
    while (j < text.size() && (static_cast<unsigned char>(text[j]) <= 0x7F) == run_ascii) ++j;
    const std::string_view run = text.substr(i, j - i);
    if (run_ascii) {
      const std::string bytes = unquote_to_bytes(run);
      if (errors == Errors::strict) {
        if (!decode_utf8(bytes)) throw PythonValueError{};
        out += bytes;
      } else {
        out += decode_replacing(bytes);
      }
    } else {
      out += run;
    }
    i = j;
  }
  return out;
}

std::vector<std::pair<std::string, std::string>> parse_qsl(std::string_view query,
                                                           bool keep_blank_values,
                                                           bool strict_parsing, Errors errors) {
  std::vector<std::pair<std::string, std::string>> out;
  if (query.empty()) return out;
  const auto spaced = [&](std::string_view text) {
    std::string plus(text);
    for (char& c : plus) {
      if (c == '+') c = ' ';
    }
    return unquote(plus, errors);
  };
  std::size_t start = 0;
  while (true) {
    const std::size_t amp = query.find('&', start);
    const std::string_view field =
        query.substr(start, amp == std::string_view::npos ? std::string_view::npos : amp - start);
    if (!field.empty() || strict_parsing) {
      const std::size_t equals = field.find('=');
      if (equals == std::string_view::npos) {
        if (strict_parsing) throw PythonValueError{};
        if (keep_blank_values) out.emplace_back(spaced(field), std::string());
      } else if (equals + 1 < field.size() || keep_blank_values) {
        out.emplace_back(spaced(field.substr(0, equals)), spaced(field.substr(equals + 1)));
      }
    }
    if (amp == std::string_view::npos) break;
    start = amp + 1;
  }
  return out;
}

std::optional<std::uint32_t> ipv4(std::string_view text) {
  std::uint32_t value = 0;
  int octets = 0;
  std::size_t start = 0;
  while (true) {
    const std::size_t dot = text.find('.', start);
    const std::string_view octet =
        text.substr(start, dot == std::string_view::npos ? std::string_view::npos : dot - start);
    if (octet.empty() || octet.size() > 3) return std::nullopt;
    for (const char c : octet) {
      if (c < '0' || c > '9') return std::nullopt;
    }
    if (octet.size() > 1 && octet[0] == '0') return std::nullopt;
    int number = 0;
    for (const char c : octet) number = number * 10 + (c - '0');
    if (number > 255) return std::nullopt;
    value = (value << 8U) | static_cast<std::uint32_t>(number);
    ++octets;
    if (dot == std::string_view::npos) break;
    start = dot + 1;
  }
  if (octets != 4) return std::nullopt;
  return value;
}

std::optional<std::array<std::uint16_t, 8>> ipv6(std::string_view text) {
  std::vector<std::string> parts;
  std::size_t start = 0;
  while (true) {
    const std::size_t colon = text.find(':', start);
    parts.emplace_back(
        text.substr(start, colon == std::string_view::npos ? std::string_view::npos : colon - start));
    if (colon == std::string_view::npos) break;
    start = colon + 1;
  }
  if (parts.size() < 3) return std::nullopt;
  if (parts.back().find('.') != std::string::npos) {
    const std::optional<std::uint32_t> tail = ipv4(parts.back());
    if (!tail) return std::nullopt;
    parts.pop_back();
    char high[8];
    char low[8];
    std::snprintf(high, sizeof high, "%x", static_cast<unsigned>((*tail >> 16U) & 0xFFFFU));
    std::snprintf(low, sizeof low, "%x", static_cast<unsigned>(*tail & 0xFFFFU));
    parts.emplace_back(high);
    parts.emplace_back(low);
  }
  if (parts.size() > 9) return std::nullopt;
  std::optional<std::size_t> skip;
  for (std::size_t i = 1; i + 1 < parts.size(); ++i) {
    if (parts[i].empty()) {
      if (skip) return std::nullopt;
      skip = i;
    }
  }
  std::size_t high_count = 0;
  std::size_t low_count = 0;
  std::size_t skipped = 0;
  if (skip) {
    high_count = *skip;
    low_count = parts.size() - *skip - 1;
    if (parts.front().empty()) {
      if (high_count == 0) return std::nullopt;
      --high_count;
      if (high_count != 0) return std::nullopt;
    }
    if (parts.back().empty()) {
      if (low_count == 0) return std::nullopt;
      --low_count;
      if (low_count != 0) return std::nullopt;
    }
    if (high_count + low_count >= 8) return std::nullopt;
    skipped = 8 - (high_count + low_count);
  } else {
    if (parts.size() != 8) return std::nullopt;
    if (parts.front().empty() || parts.back().empty()) return std::nullopt;
    high_count = parts.size();
  }
  const auto hextet = [](const std::string& part) -> std::optional<std::uint16_t> {
    if (part.empty() || part.size() > 4) return std::nullopt;
    unsigned value = 0;
    for (const char c : part) {
      if (!hex_digit(c)) return std::nullopt;
      value = value * 16 + static_cast<unsigned>(hex_value(c));
    }
    return static_cast<std::uint16_t>(value);
  };
  std::array<std::uint16_t, 8> words{};
  std::size_t at = 0;
  for (std::size_t i = 0; i < high_count; ++i) {
    const auto word = hextet(parts[i]);
    if (!word) return std::nullopt;
    words[at++] = *word;
  }
  at += skipped;
  for (std::size_t i = parts.size() - low_count; i < parts.size(); ++i) {
    const auto word = hextet(parts[i]);
    if (!word) return std::nullopt;
    words[at++] = *word;
  }
  return words;
}

std::string ipv6_format(const std::array<std::uint16_t, 8>& words) {
  std::vector<std::string> hextets;
  for (const std::uint16_t word : words) {
    char text[8];
    std::snprintf(text, sizeof text, "%x", static_cast<unsigned>(word));
    hextets.emplace_back(text);
  }
  int best_start = -1;
  int best_length = 0;
  int current_start = -1;
  int current_length = 0;
  for (int index = 0; index < 8; ++index) {
    if (hextets[static_cast<std::size_t>(index)] == "0") {
      ++current_length;
      if (current_start == -1) current_start = index;
      if (current_length > best_length) {
        best_length = current_length;
        best_start = current_start;
      }
    } else {
      current_length = 0;
      current_start = -1;
    }
  }
  if (best_length > 1) {
    const auto begin = hextets.begin() + best_start;
    const bool at_end = best_start + best_length == 8;
    hextets.erase(begin, begin + best_length);
    hextets.insert(hextets.begin() + best_start, "");
    if (at_end) hextets.emplace_back("");
    if (best_start == 0) hextets.insert(hextets.begin(), "");
  }
  std::string out;
  for (std::size_t i = 0; i < hextets.size(); ++i) {
    if (i != 0) out += ':';
    out += hextets[i];
  }
  return out;
}

Address ip_address(std::string_view text) {
  if (ipv4(text)) return Address::v4;
  const std::size_t percent = text.find('%');
  if (percent != std::string_view::npos) {
    const std::string_view scope = text.substr(percent + 1);
    if (scope.empty() || scope.find('%') != std::string_view::npos) return Address::none;
    text = text.substr(0, percent);
  }
  return ipv6(text) ? Address::v6 : Address::none;
}

double float_of(std::string_view text) {
  const std::optional<std::u32string> code_points = decode_utf8(text);
  if (!code_points) throw PythonValueError{};
  // `_PyUnicode_TransformDecimalAndSpaceToASCII`: whitespace a space, a decimal digit its ASCII
  // digit, anything else outside ASCII a character no number has.
  std::string ascii_text;
  for (const char32_t c : *code_points) {
    if (python_space(c)) {
      ascii_text += ' ';
    } else if (c >= 0x80 && is_decimal_digit(c)) {
      ascii_text += static_cast<char>('0' + digit_value(c));
    } else if (c < 0x80) {
      ascii_text += static_cast<char>(c);
    } else {
      throw PythonValueError{};
    }
  }
  // `_Py_string_to_number_with_underscores`: an underscore only between two digits.
  std::string plain;
  char previous = '\0';
  for (const char c : ascii_text) {
    if (c == '_') {
      if (!(previous >= '0' && previous <= '9')) throw PythonValueError{};
    } else {
      if (previous == '_' && !(c >= '0' && c <= '9')) throw PythonValueError{};
      plain += c;
    }
    previous = c;
  }
  if (previous == '_') throw PythonValueError{};
  // `float_from_string_inner`: spaces at either end, then the whole of the rest a number.
  std::string_view body = plain;
  while (!body.empty() && body.front() == ' ') body.remove_prefix(1);
  while (!body.empty() && body.back() == ' ') body.remove_suffix(1);
  if (body.empty()) throw PythonValueError{};
  bool negative = false;
  std::string_view number = body;
  if (number.front() == '+' || number.front() == '-') {
    negative = number.front() == '-';
    number.remove_prefix(1);
  }
  const std::string word = lower(number);
  if (word == "inf" || word == "infinity") {
    return negative ? -std::numeric_limits<double>::infinity()
                    : std::numeric_limits<double>::infinity();
  }
  if (word == "nan") return std::numeric_limits<double>::quiet_NaN();
  // (digits[.digits]|.digits)([eE][+-]?digits)?
  std::size_t i = 0;
  const auto digits = [&] {
    const std::size_t from = i;
    while (i < number.size() && number[i] >= '0' && number[i] <= '9') ++i;
    return i - from;
  };
  const std::size_t whole = digits();
  std::size_t fraction = 0;
  if (i < number.size() && number[i] == '.') {
    ++i;
    fraction = digits();
  }
  if (whole == 0 && fraction == 0) throw PythonValueError{};
  if (i < number.size() && (number[i] == 'e' || number[i] == 'E')) {
    ++i;
    if (i < number.size() && (number[i] == '+' || number[i] == '-')) ++i;
    if (digits() == 0) throw PythonValueError{};
  }
  if (i != number.size()) throw PythonValueError{};
  double value = 0;
  const auto [end, problem] = std::from_chars(number.data(), number.data() + number.size(), value);
  if (problem == std::errc::result_out_of_range) {
    // An exponent past the range: Python's parser gives an infinity, or zero below it.
    const std::size_t exponent = number.find_first_of("eE");
    const bool small = exponent != std::string_view::npos && number[exponent + 1] == '-';
    value = small ? 0.0 : std::numeric_limits<double>::infinity();
  } else if (problem != std::errc() || end != number.data() + number.size()) {
    throw PythonValueError{};
  }
  return negative ? -value : value;
}

}  // namespace sde::detail::python_url
