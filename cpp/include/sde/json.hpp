#pragma once

/// JSON as the format contract needs it, which is not what a general-purpose JSON library offers.
///
/// Four properties a library parser would lose, each pinned by a conformance vector:
/// - **a number keeps its lexical form.** `canonical/005` carries 2^53 - 1; a parser that decodes to
///   `double` re-emits other bytes. Integers are read exactly or refused, never rounded.
/// - **keys are byte strings, compared as written.** `canonical/008` holds two keys that differ only
///   in Unicode composition; they are two members here, and the canonical encoder refuses the pair
///   after normalising. A parser that normalised would hide the defect the vector exists for.
/// - **an unpaired surrogate escape survives the parse** (as its three-byte sequence, which is not
///   UTF-8), so that the placement-map loader refuses it with the contract's `MapError` rather than
///   the parser refusing it with something else (`errors/050`, `errors/051`).
/// - **a byte-identical duplicate key keeps the last value at the first position**, which is what
///   Python's `dict` and JavaScript objects do. Both reference libraries read documents that way.

#include <cstddef>
#include <cstdint>
#include <optional>
#include <string>
#include <string_view>
#include <type_traits>
#include <utility>
#include <variant>
#include <vector>

#include "sde/errors.hpp"

namespace sde {

/// A document that is not JSON. Callers that read a document of the contract re-raise it as the
/// contract's own class for that document (`MapError` for a map, `DeclarationError` for a model).
class JsonError : public SdeError {
 public:
  using SdeError::SdeError;
  ~JsonError() override;
};

class Json {
 public:
  enum class Kind { null, boolean, number, string, array, object };

  /// A number as written: its lexeme, and whether that lexeme is an integer (no fraction, no
  /// exponent). `2.0` is not an integer lexeme; whether a reader accepts it as one is that reader's
  /// rule (map contract 4 does, the canonical encoder does not).
  struct Number {
    std::string lexeme;
    bool integer = true;
  };
  using Array = std::vector<Json>;
  using Member = std::pair<std::string, Json>;
  /// Members in document order; keys are distinct byte strings.
  using Object = std::vector<Member>;

  Json() noexcept = default;
  Json(std::nullptr_t) noexcept {}  // NOLINT(google-explicit-constructor)
  Json(bool value) noexcept : value_(value) {}  // NOLINT(google-explicit-constructor)
  template <class T>
    requires(std::is_integral_v<T> && !std::is_same_v<T, bool> && !std::is_same_v<T, char> &&
             !std::is_same_v<T, char8_t> && !std::is_same_v<T, char16_t> &&
             !std::is_same_v<T, char32_t> && !std::is_same_v<T, wchar_t>)
  Json(T value) : value_(Number{std::to_string(value), true}) {}  // NOLINT(google-explicit-constructor)
  /// A floating point value, written in its shortest round-trip form and never an integer lexeme:
  /// the canonical encoder refuses it whatever its value, as the reference does.
  Json(double value);  // NOLINT(google-explicit-constructor)
  Json(const char* value) : value_(std::string(value)) {}  // NOLINT(google-explicit-constructor)
  Json(std::string value) noexcept : value_(std::move(value)) {}  // NOLINT(google-explicit-constructor)
  Json(std::string_view value) : value_(std::string(value)) {}  // NOLINT(google-explicit-constructor)
  Json(Array value) noexcept : value_(std::move(value)) {}  // NOLINT(google-explicit-constructor)
  Json(Object value) noexcept : value_(std::move(value)) {}  // NOLINT(google-explicit-constructor)

  /// A number from a lexeme that must be a JSON number; refused otherwise.
  [[nodiscard]] static Json from_lexeme(std::string lexeme);
  [[nodiscard]] static Json array() { return Json(Array{}); }
  [[nodiscard]] static Json object() { return Json(Object{}); }

  [[nodiscard]] Kind kind() const noexcept { return static_cast<Kind>(value_.index()); }
  [[nodiscard]] bool is_null() const noexcept { return kind() == Kind::null; }
  [[nodiscard]] bool is_bool() const noexcept { return kind() == Kind::boolean; }
  [[nodiscard]] bool is_number() const noexcept { return kind() == Kind::number; }
  [[nodiscard]] bool is_string() const noexcept { return kind() == Kind::string; }
  [[nodiscard]] bool is_array() const noexcept { return kind() == Kind::array; }
  [[nodiscard]] bool is_object() const noexcept { return kind() == Kind::object; }

  [[nodiscard]] bool as_bool() const;
  [[nodiscard]] const Number& as_number() const;
  [[nodiscard]] const std::string& as_string() const;
  [[nodiscard]] const Array& as_array() const;
  [[nodiscard]] Array& as_array();
  [[nodiscard]] const Object& as_object() const;
  [[nodiscard]] Object& as_object();

  /// The member with this exact key, or null when there is none (or this is not an object).
  [[nodiscard]] const Json* find(std::string_view key) const noexcept;
  [[nodiscard]] Json* find(std::string_view key) noexcept;
  [[nodiscard]] bool contains(std::string_view key) const noexcept { return find(key) != nullptr; }
  /// Replaces an existing member's value in place, or appends a new member.
  void set(std::string key, Json value);
  /// Removes the member with this key, if there is one; reports whether there was.
  bool erase(std::string_view key);

  /// The exact value of an integer lexeme within the 64-bit range; empty for anything else.
  [[nodiscard]] std::optional<std::int64_t> to_int64() const noexcept;
  /// The number as the nearest double (correctly rounded); empty when this is not a number.
  [[nodiscard]] std::optional<double> to_double() const noexcept;

  /// Semantic equality: numbers by value (integers exactly, others as doubles), objects as sets of
  /// members, arrays in order.
  friend bool operator==(const Json& left, const Json& right);

 private:
  std::variant<std::nullptr_t, bool, Number, std::string, Array, Object> value_{nullptr};
};

/// Parses one JSON text (RFC 8259) strictly: UTF-8 without a byte order mark, no comments, no
/// trailing commas, no `NaN`, and nothing but whitespace after the value.
[[nodiscard]] Json parse_json(std::string_view text);

/// Writes a document compactly: no insignificant whitespace, members in stored order, minimal
/// escaping. This is a document writer, not the canonical encoder - it neither sorts nor normalises.
[[nodiscard]] std::string dump_json(const Json& value);

}  // namespace sde
