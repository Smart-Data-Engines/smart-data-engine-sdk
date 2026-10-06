#include "conformance/stages.hpp"

#include <gtest/gtest.h>

namespace sde::testing_support {

void run_map_stage(const std::filesystem::path& directory, const sde::Json& /*expected*/,
                   const std::string& /*error*/, const std::string& /*match*/) {
  FAIL() << "the map stage is not implemented yet: " << directory.filename();
}

void run_engine_stage(const std::filesystem::path& directory, const sde::Json& /*expected*/,
                      const std::string& stage, const std::string& /*error*/,
                      const std::string& /*match*/) {
  if (kTier < 2) {
    // Tier 2 only (section 8a): this build claims a lower tier, so the case is outside it. The count
    // of such cases is asserted separately, so nothing else can hide here.
    SUCCEED() << directory.filename() << " is a " << stage << "-stage case, Tier 2";
    return;
  }
  FAIL() << "the " << stage << " stage is not implemented yet: " << directory.filename();
}

}  // namespace sde::testing_support
