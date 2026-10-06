#include "conformance/stages.hpp"

#include <gtest/gtest.h>

#include "encoding.hpp"
#include "sde/model.hpp"
#include "sde/placement.hpp"
#include "support/vectors.hpp"

namespace sde::testing_support {

void run_map_stage(const std::filesystem::path& directory, const sde::Json& expected,
                   const std::string& error, const std::string& match) {
  // The model of a map-stage case is valid, and it is built outside the assertion on purpose: a
  // model broken by accident would otherwise fail with the right class at the wrong stage.
  const sde::Model model = sde::load_neutral_model(read_json(directory / "model.json"));
  sde::LoadOptions options;
  options.model = &model;
  // Arguments for the loader, not anything about the refusal (errors/015 needs require_signature,
  // since its map is unsigned and an unsigned map is accepted).
  if (const sde::Json* load = expected.find("load")) {
    if (const sde::Json* key = load->find("public_key"); key != nullptr && key->is_string()) {
      const auto decoded = sde::detail::base64_decode(key->as_string());
      ASSERT_TRUE(decoded.has_value()) << "the case's public key is not base64";
      options.public_keys = sde::PublicKeys::bare(*decoded);
    }
    if (const sde::Json* required = load->find("require_signature"); required != nullptr) {
      options.require_signature = required->is_bool() && required->as_bool();
    }
  }
  const sde::Json document = read_json(directory / "map.json");
  expect_refusal([&] { (void)sde::load_map(document, options); }, error, match);
}

void run_engine_stage(const std::filesystem::path& directory, const sde::Json& /*expected*/,
                      const std::string& stage, const std::string& /*error*/,
                      const std::string& /*match*/) {
  if (kTier < 2) {
    // Tier 2 only (section 8a): this build claims a lower tier, so the case is outside it. Only the
    // session and write stages reach here; the caller fails on a stage it does not know, so no other
    // case can pass this way.
    SUCCEED() << directory.filename() << " is a " << stage << "-stage case, Tier 2";
    return;
  }
  FAIL() << "the " << stage << " stage is not implemented yet: " << directory.filename();
}

}  // namespace sde::testing_support
