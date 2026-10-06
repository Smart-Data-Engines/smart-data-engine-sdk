#pragma once

/// An unsigned integer of any size, for the exact decimal arithmetic of numeric summaries (a sum of
/// 76-digit values over a count, rounded half to even at up to 38 more digits). The reference does
/// this with Python's unbounded `int` and TypeScript with `bigint`; a 128-bit type would cover most
/// summaries and silently wrap on the rest.
///
/// Small and slow on purpose: base 2^32 limbs, schoolbook multiplication and Knuth's algorithm D
/// for division. Every value it sees has a few hundred bits, and it is never on a hot path.

#include <compare>
#include <cstdint>
#include <optional>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

namespace sde::detail {

class BigUnsigned {
 public:
  BigUnsigned() = default;
  explicit BigUnsigned(std::uint64_t value);

  /// ASCII decimal digits, at least one; empty for anything else. Leading zeros are allowed.
  [[nodiscard]] static std::optional<BigUnsigned> from_decimal(std::string_view digits);
  /// `10^exponent`.
  [[nodiscard]] static BigUnsigned pow10(unsigned exponent);

  /// Decimal digits without leading zeros; `0` for zero.
  [[nodiscard]] std::string to_decimal() const;
  /// The value when it fits in 64 bits.
  [[nodiscard]] std::optional<std::uint64_t> to_uint64() const noexcept;

  [[nodiscard]] bool is_zero() const noexcept { return limbs_.empty(); }
  [[nodiscard]] bool is_odd() const noexcept { return !limbs_.empty() && (limbs_[0] & 1U) != 0; }

  BigUnsigned& operator+=(const BigUnsigned& other);
  /// Requires `*this >= other`; the caller has compared first.
  BigUnsigned& operator-=(const BigUnsigned& other);
  friend BigUnsigned operator+(BigUnsigned left, const BigUnsigned& right) {
    left += right;
    return left;
  }
  friend BigUnsigned operator-(BigUnsigned left, const BigUnsigned& right) {
    left -= right;
    return left;
  }
  friend BigUnsigned operator*(const BigUnsigned& left, const BigUnsigned& right);

  /// Quotient and remainder. The divisor must not be zero.
  friend std::pair<BigUnsigned, BigUnsigned> divmod(const BigUnsigned& dividend,
                                                    const BigUnsigned& divisor);

  friend std::strong_ordering operator<=>(const BigUnsigned& left, const BigUnsigned& right) noexcept;
  friend bool operator==(const BigUnsigned& left, const BigUnsigned& right) noexcept = default;

 private:
  void trim() noexcept;
  /// `*this = *this * factor + addend`, for factors and addends below 2^32.
  void multiply_add(std::uint32_t factor, std::uint32_t addend);
  /// Divides in place by a divisor below 2^32 and returns the remainder.
  std::uint32_t divide_small(std::uint32_t divisor);

  std::vector<std::uint32_t> limbs_;  ///< least significant first, no trailing zero limbs
};

}  // namespace sde::detail
