#pragma once

#include <filesystem>
#include <string>

#include "sde/json.hpp"

namespace sde::testing_support {

/// The highest tier this build of the library claims (format contract section 9). Vectors of a
/// higher tier are counted as outside it - by an assertion on exactly which they are - never
/// skipped silently.
inline constexpr int kTier = 0;

void run_map_stage(const std::filesystem::path& directory, const sde::Json& expected,
                   const std::string& error, const std::string& match);

void run_engine_stage(const std::filesystem::path& directory, const sde::Json& expected,
                      const std::string& stage, const std::string& error,
                      const std::string& match);

}  // namespace sde::testing_support
