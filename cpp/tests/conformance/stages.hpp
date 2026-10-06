#pragma once

#include <filesystem>
#include <string>

#include "sde/json.hpp"
#include "sde/version.hpp"

namespace sde::testing_support {

/// The highest tier this build of the library claims (format contract section 9), as the library
/// declares it. An `errors/` case at a stage that needs engines passes as outside the claim, and
/// only at those stages: the runner fails on any stage it does not know.
inline constexpr int kTier = sde::TIER;

void run_map_stage(const std::filesystem::path& directory, const sde::Json& expected,
                   const std::string& error, const std::string& match);

void run_engine_stage(const std::filesystem::path& directory, const sde::Json& expected,
                      const std::string& stage, const std::string& error,
                      const std::string& match);

}  // namespace sde::testing_support
