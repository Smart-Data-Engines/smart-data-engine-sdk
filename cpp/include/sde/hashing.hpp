#pragma once

/// Hashed identifiers (format contract section 2a): every entity, field and relation name replaced
/// by a keyed digest, so that we never see that a client has an entity called `patient_diagnosis`.
/// The salt stays on the client's infrastructure and is never serialised into anything.
///
/// Hashing is a model change, not a setting: group names, shape ids and the model version all
/// change, so switching it on needs a new map.

#include <filesystem>
#include <map>
#include <string>
#include <string_view>
#include <utility>

#include "sde/model.hpp"

namespace sde {

/// Twelve hex characters, 48 bits - half a model version, because these appear in table names.
inline constexpr std::size_t DIGEST_CHARS = 12;

/// The translation between the names a client wrote and the digests the model carries. Held only
/// in the client's process. `std::map` reserves no key, which is the property `hashing/003` pins:
/// a container that treats some identifier specially loses it.
struct NameMap {
  std::map<std::string, std::string> entities;
  std::map<std::string, std::map<std::string, std::string>> fields;
  std::map<std::string, std::map<std::string, std::string>> relations;

  /// The hashed name of an entity; `DeclarationError` when the model has no such entity.
  [[nodiscard]] const std::string& entity(std::string_view name) const;
  [[nodiscard]] const std::string& field(std::string_view entity, std::string_view name) const;
  [[nodiscard]] const std::string& relation(std::string_view entity, std::string_view name) const;
};

/// `prefix + lowercase_hex(hmac_sha256(salt, join(nfc(parts), U+0000)))[:12]`.
[[nodiscard]] std::string hashed_name(std::string_view salt, std::string_view prefix,
                                      std::initializer_list<std::string_view> parts);

/// An equivalent model with every identifier replaced by its digest, and the map between them.
/// Refuses a salt shorter than 16 bytes and any two names that hash to one digest - refused rather
/// than merged, because a model with one entity where there were two writes both into one table.
[[nodiscard]] std::pair<Model, NameMap> hash_identifiers(const Model& model, std::string_view salt);

/// Reads a salt file verbatim: no trimming and no text decoding. A Python service and a C++ one
/// sharing one salt file must derive the same digests, and stripping "whitespace" would shorten a
/// random salt about 5% of the time (section 2a).
[[nodiscard]] std::string read_salt_file(const std::filesystem::path& path);

}  // namespace sde
