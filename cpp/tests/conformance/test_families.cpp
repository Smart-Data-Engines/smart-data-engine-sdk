/// Every vector family in the tree is either run by this suite or above this library's tier, by
/// name. A family added later fails here until a runner - or a tier - says what happens to it: a
/// family nobody runs is a rule nobody checks, and it looks exactly like a family that passes.

#include <algorithm>
#include <filesystem>
#include <map>
#include <set>
#include <string>
#include <string_view>

#include <gtest/gtest.h>

#include "sde/version.hpp"
#include "support/vectors.hpp"

namespace {

using namespace sde::testing_support;

/// The tier each family belongs to (format contract section 9). `hashing/` is a mode, not a tier,
/// and is required of a library that offers hashing. Three Tier 2 families go with a capability as
/// well as the tier: see `capable`.
const std::map<std::string, int>& tier_of_family() {
  static const std::map<std::string, int> tiers = {
      {"canonical", 0}, {"model", 0},     {"routing", 0},   {"errors", 0},
      {"signature", 0}, {"hashing", 0},   {"telemetry", 1}, {"schema", 2},
      {"migration", 2}, {"query", 2},     {"books", 2},     {"search", 2},
      {"documents", 2}};
  return tiers;
}

/// Whether this library has the capability a family goes with (section 9): `books/` book streams,
/// `documents/` the Elasticsearch engine, and `search/` `Session.search`. Every other family goes
/// with its tier alone.
bool capable(const std::string& family) {
  const auto adapter = [](std::string_view dialect) {
    return std::find(sde::ADAPTERS.begin(), sde::ADAPTERS.end(), dialect) != sde::ADAPTERS.end();
  };
  if (family == "books") return sde::BOOK_STREAMS;
  if (family == "documents") return adapter("elasticsearch");
  if (family == "search") return sde::SEARCH;
  return true;
}

/// The families this suite has a runner for, each in its own file.
const std::set<std::string>& run_here() {
  static const std::set<std::string> families = {"canonical", "model",   "routing",   "errors",
                                                 "signature", "hashing", "telemetry", "schema",
                                                 "query",     "migration"};
  return families;
}

TEST(Families, EachIsRunHereOrAboveTheTier) {
  std::set<std::string> present;
  for (const auto& entry : std::filesystem::directory_iterator(vectors_root())) {
    if (entry.is_directory()) present.insert(entry.path().filename().string());
  }
  ASSERT_FALSE(present.empty()) << "no vector families under " << vectors_root();
  for (const std::string& family : present) {
    const auto tier = tier_of_family().find(family);
    ASSERT_NE(tier, tier_of_family().end())
        << "`" << family << "/` is a vector family this suite has never heard of: give it a "
        << "runner, or a tier above this library's, before it can pass by being ignored";
    const bool required =
        family == "hashing" ? sde::HASHING : tier->second <= sde::TIER && capable(family);
    if (required) {
      EXPECT_EQ(run_here().count(family), 1U)
          << "`" << family << "/` is Tier " << tier->second << " and this library claims Tier "
          << sde::TIER << ", so it has to be run here";
    }
  }
  // And the other way: a runner for a family that no longer exists is coverage that is not there.
  for (const std::string& family : run_here()) {
    EXPECT_EQ(present.count(family), 1U) << "the suite runs `" << family << "/`, which is gone";
  }
}

}  // namespace
