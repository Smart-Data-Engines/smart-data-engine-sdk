#include "bignum.hpp"

#include <algorithm>
#include <bit>
#include <cstddef>
#include <stdexcept>

namespace sde::detail {

namespace {

constexpr std::uint64_t kBase = std::uint64_t{1} << 32;
constexpr std::uint32_t kDecimalChunk = 1'000'000'000;  // 10^9: the largest power of ten below 2^32
constexpr int kDecimalChunkDigits = 9;

std::uint32_t low(std::uint64_t value) noexcept { return static_cast<std::uint32_t>(value); }
std::uint32_t high(std::uint64_t value) noexcept { return static_cast<std::uint32_t>(value >> 32); }

}  // namespace

BigUnsigned::BigUnsigned(std::uint64_t value) {
  if (value != 0) limbs_.push_back(low(value));
  if (high(value) != 0) limbs_.push_back(high(value));
}

std::optional<BigUnsigned> BigUnsigned::from_decimal(std::string_view digits) {
  if (digits.empty()) return std::nullopt;
  for (const char c : digits) {
    if (c < '0' || c > '9') return std::nullopt;
  }
  BigUnsigned out;
  // Nine digits at a time: the first chunk takes whatever is left over, so every later one is whole.
  std::size_t position = 0;
  std::size_t first = digits.size() % kDecimalChunkDigits;
  if (first == 0) first = kDecimalChunkDigits;
  while (position < digits.size()) {
    const std::size_t width = position == 0 ? first : kDecimalChunkDigits;
    std::uint32_t chunk = 0;
    std::uint32_t factor = 1;
    for (std::size_t i = 0; i < width; ++i) {
      chunk = chunk * 10U + static_cast<std::uint32_t>(digits[position + i] - '0');
      factor *= 10U;
    }
    out.multiply_add(factor, chunk);
    position += width;
  }
  return out;
}

BigUnsigned BigUnsigned::pow10(unsigned exponent) {
  BigUnsigned out(1);
  while (exponent >= static_cast<unsigned>(kDecimalChunkDigits)) {
    out.multiply_add(kDecimalChunk, 0);
    exponent -= static_cast<unsigned>(kDecimalChunkDigits);
  }
  std::uint32_t rest = 1;
  for (unsigned i = 0; i < exponent; ++i) rest *= 10U;
  out.multiply_add(rest, 0);
  return out;
}

std::string BigUnsigned::to_decimal() const {
  if (is_zero()) return "0";
  BigUnsigned rest = *this;
  std::vector<std::uint32_t> chunks;
  while (!rest.is_zero()) chunks.push_back(rest.divide_small(kDecimalChunk));
  std::string out = std::to_string(chunks.back());
  for (auto chunk = chunks.rbegin() + 1; chunk != chunks.rend(); ++chunk) {
    const std::string text = std::to_string(*chunk);
    out.append(static_cast<std::size_t>(kDecimalChunkDigits) - text.size(), '0');
    out += text;
  }
  return out;
}

std::optional<std::uint64_t> BigUnsigned::to_uint64() const noexcept {
  if (limbs_.size() > 2) return std::nullopt;
  std::uint64_t value = 0;
  for (std::size_t i = limbs_.size(); i-- > 0;) value = (value << 32) | limbs_[i];
  return value;
}

void BigUnsigned::trim() noexcept {
  while (!limbs_.empty() && limbs_.back() == 0) limbs_.pop_back();
}

void BigUnsigned::multiply_add(std::uint32_t factor, std::uint32_t addend) {
  std::uint64_t carry = addend;
  for (std::uint32_t& limb : limbs_) {
    const std::uint64_t product = std::uint64_t{limb} * factor + carry;
    limb = low(product);
    carry = high(product);
  }
  if (carry != 0) limbs_.push_back(low(carry));
  trim();
}

std::uint32_t BigUnsigned::divide_small(std::uint32_t divisor) {
  std::uint64_t remainder = 0;
  for (std::size_t i = limbs_.size(); i-- > 0;) {
    const std::uint64_t current = (remainder << 32) | limbs_[i];
    limbs_[i] = low(current / divisor);
    remainder = current % divisor;
  }
  trim();
  return low(remainder);
}

BigUnsigned& BigUnsigned::operator+=(const BigUnsigned& other) {
  if (limbs_.size() < other.limbs_.size()) limbs_.resize(other.limbs_.size(), 0);
  std::uint64_t carry = 0;
  for (std::size_t i = 0; i < limbs_.size(); ++i) {
    const std::uint64_t sum =
        std::uint64_t{limbs_[i]} + (i < other.limbs_.size() ? other.limbs_[i] : 0U) + carry;
    limbs_[i] = low(sum);
    carry = high(sum);
  }
  if (carry != 0) limbs_.push_back(low(carry));
  return *this;
}

BigUnsigned& BigUnsigned::operator-=(const BigUnsigned& other) {
  if (*this < other) throw std::logic_error("BigUnsigned subtraction would be negative");
  std::uint64_t borrow = 0;
  for (std::size_t i = 0; i < limbs_.size(); ++i) {
    const std::uint64_t subtrahend = (i < other.limbs_.size() ? other.limbs_[i] : 0U) + borrow;
    if (std::uint64_t{limbs_[i]} >= subtrahend) {
      limbs_[i] = low(std::uint64_t{limbs_[i]} - subtrahend);
      borrow = 0;
    } else {
      limbs_[i] = low(kBase + limbs_[i] - subtrahend);
      borrow = 1;
    }
  }
  trim();
  return *this;
}

BigUnsigned operator*(const BigUnsigned& left, const BigUnsigned& right) {
  BigUnsigned out;
  if (left.is_zero() || right.is_zero()) return out;
  out.limbs_.assign(left.limbs_.size() + right.limbs_.size(), 0);
  for (std::size_t i = 0; i < left.limbs_.size(); ++i) {
    std::uint64_t carry = 0;
    for (std::size_t j = 0; j < right.limbs_.size(); ++j) {
      // At most (2^32-1)^2 + 2 * (2^32-1), which is exactly 2^64 - 1: no overflow.
      const std::uint64_t current =
          std::uint64_t{left.limbs_[i]} * right.limbs_[j] + out.limbs_[i + j] + carry;
      out.limbs_[i + j] = low(current);
      carry = high(current);
    }
    out.limbs_[i + right.limbs_.size()] = low(carry);
  }
  out.trim();
  return out;
}

std::pair<BigUnsigned, BigUnsigned> divmod(const BigUnsigned& dividend, const BigUnsigned& divisor) {
  if (divisor.is_zero()) throw std::domain_error("BigUnsigned division by zero");
  if (dividend < divisor) return {BigUnsigned{}, dividend};
  if (divisor.limbs_.size() == 1) {
    BigUnsigned quotient = dividend;
    const std::uint32_t remainder = quotient.divide_small(divisor.limbs_[0]);
    return {quotient, BigUnsigned(remainder)};
  }

  // Knuth, TAOCP vol. 2, 4.3.1, algorithm D, in the form of Hacker's Delight's `divmnu`.
  const std::size_t m = dividend.limbs_.size();
  const std::size_t n = divisor.limbs_.size();
  // D1: normalise, so the divisor's top limb has its high bit set and each quotient-digit estimate
  // is at most two too large.
  const int shift = std::countl_zero(divisor.limbs_.back());
  const auto shifted = [shift](std::uint32_t upper, std::uint32_t lower) {
    return low((std::uint64_t{upper} << shift) | (std::uint64_t{lower} >> (32 - shift)));
  };
  std::vector<std::uint32_t> v(n);
  for (std::size_t i = n - 1; i > 0; --i) v[i] = shifted(divisor.limbs_[i], divisor.limbs_[i - 1]);
  v[0] = low(std::uint64_t{divisor.limbs_[0]} << shift);
  std::vector<std::uint32_t> u(m + 1);
  u[m] = low(std::uint64_t{dividend.limbs_[m - 1]} >> (32 - shift));
  for (std::size_t i = m - 1; i > 0; --i) u[i] = shifted(dividend.limbs_[i], dividend.limbs_[i - 1]);
  u[0] = low(std::uint64_t{dividend.limbs_[0]} << shift);

  BigUnsigned quotient;
  quotient.limbs_.assign(m - n + 1, 0);
  for (std::size_t j = m - n + 1; j-- > 0;) {
    // D3: estimate the quotient digit from the top two limbs, then correct it with the third.
    const std::uint64_t numerator = (std::uint64_t{u[j + n]} << 32) | u[j + n - 1];
    std::uint64_t estimate = numerator / v[n - 1];
    std::uint64_t rest = numerator % v[n - 1];
    while (estimate >= kBase || estimate * v[n - 2] > ((rest << 32) | u[j + n - 2])) {
      --estimate;
      rest += v[n - 1];
      if (rest >= kBase) break;
    }
    // D4: multiply and subtract.
    std::uint64_t borrow = 0;
    std::uint64_t carry = 0;
    for (std::size_t i = 0; i < n; ++i) {
      const std::uint64_t product = estimate * v[i] + carry;
      carry = high(product);
      const std::uint64_t subtrahend = std::uint64_t{low(product)} + borrow;
      if (std::uint64_t{u[i + j]} >= subtrahend) {
        u[i + j] = low(u[i + j] - subtrahend);
        borrow = 0;
      } else {
        u[i + j] = low(kBase + u[i + j] - subtrahend);
        borrow = 1;
      }
    }
    const std::uint64_t top = carry + borrow;
    bool negative = false;
    if (std::uint64_t{u[j + n]} >= top) {
      u[j + n] = low(u[j + n] - top);
    } else {
      u[j + n] = low(kBase + u[j + n] - top);
      negative = true;
    }
    // D5 and D6: the estimate was one too large, which happens rarely; add the divisor back.
    if (negative) {
      --estimate;
      std::uint64_t add_carry = 0;
      for (std::size_t i = 0; i < n; ++i) {
        const std::uint64_t sum = std::uint64_t{u[i + j]} + v[i] + add_carry;
        u[i + j] = low(sum);
        add_carry = high(sum);
      }
      u[j + n] = low(std::uint64_t{u[j + n]} + add_carry);
    }
    quotient.limbs_[j] = low(estimate);
  }
  quotient.trim();

  // D8: the remainder is what is left, shifted back.
  BigUnsigned remainder;
  remainder.limbs_.assign(n, 0);
  for (std::size_t i = 0; i + 1 < n; ++i) {
    remainder.limbs_[i] =
        low((std::uint64_t{u[i]} >> shift) | (std::uint64_t{u[i + 1]} << (32 - shift)));
  }
  remainder.limbs_[n - 1] = low(std::uint64_t{u[n - 1]} >> shift);
  remainder.trim();
  return {quotient, remainder};
}

std::strong_ordering operator<=>(const BigUnsigned& left, const BigUnsigned& right) noexcept {
  if (left.limbs_.size() != right.limbs_.size()) return left.limbs_.size() <=> right.limbs_.size();
  for (std::size_t i = left.limbs_.size(); i-- > 0;) {
    if (left.limbs_[i] != right.limbs_[i]) return left.limbs_[i] <=> right.limbs_[i];
  }
  return std::strong_ordering::equal;
}

}  // namespace sde::detail
