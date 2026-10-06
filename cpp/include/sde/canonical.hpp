#pragma once

/// The canonical encoding (format contract section 1) and the identifiers derived from it
/// (section 2). Every digest in the product - a model version, a shape id, a map's fingerprint and
/// the payload a signature covers - is a function of these bytes, so they are the part of this
/// library that has to agree with every other one exactly.

#include <string>
#include <string_view>

#include "sde/json.hpp"

namespace sde {

/// The canonical bytes of a value: UTF-8, object keys NFC-normalised then sorted by code point and
/// unique after normalisation, strings NFC-normalised with minimal escaping, integers in their
/// shortest form, no floating point and no insignificant whitespace. Throws `CanonicalError` for a
/// value with no canonical form.
[[nodiscard]] std::string canonical_bytes(const Json& value);

/// `lowercase_hex(sha256(bytes))[:16]`, the form of `model_version` and of a shape's id.
[[nodiscard]] std::string digest16(std::string_view bytes);

/// `lowercase_hex(sha256(bytes))`, the form of a map's fingerprint (section 7b).
[[nodiscard]] std::string sha256_hex(std::string_view bytes);

}  // namespace sde
