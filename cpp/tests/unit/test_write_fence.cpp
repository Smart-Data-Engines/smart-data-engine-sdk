/// Write fences where the shared vectors do not reach: the constraint text an engine writes back,
/// read exactly as the reference reads it (the table below is its `_predicate` on the same input),
/// epochs from JSON numbers, and the refusals of a reserved namespace.

#include <map>
#include <string>
#include <utility>
#include <vector>

#include <gtest/gtest.h>

#include "sde/errors.hpp"
#include "sde/placement.hpp"
#include "sde/testing/memory.hpp"
#include "sde/write_fence.hpp"
#include "write_fence_internal.hpp"

namespace {

const std::vector<std::pair<std::string, std::string>> kPredicates = {
    {"1", "1"},
    {"true", "1"},
    {" true ", "1"},
    {"false", "0"},
    {"0", "0"},
    {"(1)", "1"},
    {"((true))", "1"},
    {"CHECK (true)", "1"},
    {"CHECK (false) NOT VALID", "0"},
    {"CHECK  ((__sde_write_epoch >= 5)) NOT  VALID", "__sde_write_epoch>=5"},
    {"__sde_write_epoch >= 5", "__sde_write_epoch>=5"},
    {"\"__sde_write_epoch\" >= 5", "__sde_write_epoch>=5"},
    {"`__sde_write_epoch` <= 7", "__sde_write_epoch<=7"},
    {"(__sde_write_epoch <= '7'::bigint)", "__sde_write_epoch<=7"},
    {"((__sde_write_epoch)>=(0005))", "__sde_write_epoch>=5"},
    {"__sde_write_epoch >  5", "<unrecognized>"},
    {"__sde_write_epoch => 5", "<unrecognized>"},
    {"__sde_write_epoch >= -5", "<unrecognized>"},
    {"__sde_write_epoch >= 5.0", "<unrecognized>"},
    {"x >= 5", "<unrecognized>"},
    {"__sde_write_epoch >= 5 AND 1", "<unrecognized>"},
    {"\u00a0__sde_write_epoch\u2003>=\u30005\u2028", "__sde_write_epoch>=5"},
    {"\u001c1\u001d", "1"},
    {"CHECK(1)", "1"},
    {"CHECKtrue", "1"},
    {"NOT VALID", "<unrecognized>"},
    {"1 NOT VALID", "1"},
    {"1NOT VALID", "<unrecognized>"},
    {"1 NOTVALID", "<unrecognized>"},
    {"check (1)", "<unrecognized>"},
    {"( (1) )", "1"},
    {"(1", "<unrecognized>"},
    {"1)", "<unrecognized>"},
    {"( __sde_write_epoch >= 5 ) NOT VALID", "__sde_write_epoch>=5"},
    {"'5'::bigint >= __sde_write_epoch", "<unrecognized>"},
    {"__sde_write_epoch >= '5'", "<unrecognized>"},
    {"__sde_write_epoch >= '5'::BIGINT", "<unrecognized>"},
    {"__sde_write_epoch>=99999999999999999999", "__sde_write_epoch>=99999999999999999999"},
    {"__SDE_WRITE_EPOCH >= 5", "<unrecognized>"},
    {"\"__sde_write_epoch >= 5\"", "<unrecognized>"},
    {"((((__sde_write_epoch >= 5))))", "__sde_write_epoch>=5"},
    {"(__sde_write_epoch >= 5", "__sde_write_epoch>=5"},
    {"__sde_write_epoch >= 5)", "__sde_write_epoch>=5"},
};

TEST(FencePredicate, ReadsConstraintTextAsTheReferenceDoes) {
  for (const auto& [raw, normalised] : kPredicates) {
    EXPECT_EQ(sde::detail::fence_predicate(raw), normalised) << "\"" << raw << "\"";
  }
}

TEST(Epoch, FromJsonNumbersAsTheContractReadsThem) {
  EXPECT_EQ(sde::epoch_from_json(sde::parse_json("1")), 1);
  EXPECT_EQ(sde::epoch_from_json(sde::parse_json("1.0")), 1);  // migration/055
  EXPECT_EQ(sde::epoch_from_json(sde::parse_json("2e0")), 2);
  EXPECT_EQ(sde::epoch_from_json(sde::parse_json("9007199254740991")), 9007199254740991);
  EXPECT_EQ(sde::epoch_from_json(sde::parse_json("9007199254740991.0")), 9007199254740991);
  for (const char* refused : {"0", "-1", "1.5", "9007199254740992", "1e300", "true", "\"1\"", "null"}) {
    EXPECT_THROW((void)sde::epoch_from_json(sde::parse_json(refused)), sde::MigrationRefused)
        << refused;
  }
  EXPECT_THROW((void)sde::check_epoch(0), sde::MigrationRefused);
  EXPECT_EQ(sde::check_epoch(sde::MAX_EPOCH), sde::MAX_EPOCH);
}

sde::FenceMetadata with(std::map<std::string, std::string> constraints) {
  return sde::FenceMetadata{"t", sde::ColumnState::valid, std::move(constraints)};
}

const std::string kOwner = "__sde_f_owner_" + std::string(32, 'a');

std::string refusal(const sde::FenceMetadata& metadata) {
  try {
    (void)sde::fence_state(metadata);
  } catch (const sde::MigrationRefused& refused) {
    return refused.what();
  }
  return "";
}

TEST(FenceState, RefusesWhatTheReservedNamespaceDoesNotHold) {
  EXPECT_EQ(refusal(with({{"__sde_f_mystery", "1"}})),
            "unrecognized constraint in the reserved write-fence namespace");
  EXPECT_EQ(refusal(with({{kOwner, "1"}, {"__sde_f_min_5", "__sde_write_epoch >= 6"}})),
            "write fence constraint __sde_f_min_5 has an unexpected predicate");
  EXPECT_EQ(refusal(with({{kOwner, "1"}, {"__sde_f_owner_" + std::string(32, 'b'), "1"}})),
            "the table carries write fences from more than one project");
  EXPECT_EQ(refusal(with({{"__sde_f_min_5", "__sde_write_epoch >= 5"}})),
            "write fence constraints have no project owner");
  // An epoch past the safe range, written in the name, is refused as an epoch.
  EXPECT_EQ(refusal(with({{kOwner, "1"}, {"__sde_f_min_9007199254740992",
                                           "__sde_write_epoch >= 9007199254740992"}})),
            "write epoch must be a positive safe integer");
  // Names outside the namespace are the client's, whatever they say.
  EXPECT_EQ(refusal(with({{"anything", "garbage"}})), "");
}

TEST(FenceState, ReadsBoundsHoldsAndRetirementsSorted) {
  const sde::FenceState state = sde::fence_state(with({
      {kOwner, "CHECK (true)"},
      {"__sde_f_min_3", "__sde_write_epoch >= 3"},
      {"__sde_f_min_2", "CHECK ((__sde_write_epoch >= 2)) NOT VALID"},
      {"__sde_f_max_3", "(__sde_write_epoch <= '3'::bigint)"},
      {"__sde_f_hold_" + std::string(32, 'c'), "false"},
      {"__sde_f_setup", "0"},
      {"__sde_f_retired_" + std::string(32, 'd'), "1"},
  }));
  EXPECT_EQ(state.project_id, std::string(32, 'a'));
  EXPECT_EQ(state.minimums, (std::vector<std::int64_t>{2, 3}));
  EXPECT_EQ(state.lower_epoch(), 3);
  EXPECT_EQ(state.upper_epoch(), 3);
  EXPECT_EQ(state.holds, (std::vector<std::string>{std::string(32, 'c'), "setup"}));
  EXPECT_TRUE(state.closed());
  EXPECT_TRUE(state.complete());
  EXPECT_EQ(state.epoch(), 3);
}

TEST(WriteFence, RefusesWhatItCannotOwn) {
  sde::testing::MemoryFences backend;
  const std::string project(32, 'a');
  EXPECT_THROW(sde::WriteFence(backend, "t", "ABC"), sde::MigrationRefused);
  EXPECT_THROW(sde::WriteFence(backend, "", project), sde::MigrationRefused);
  EXPECT_THROW(sde::WriteFence(backend, std::string("a\0b", 3), project), sde::MigrationRefused);
  for (const char* table : {"sde_map_state", "sde_backfill_state", "__sde_fence_drains"}) {
    EXPECT_THROW(sde::WriteFence(backend, table, project), sde::MigrationRefused) << table;
  }
  backend.column = sde::ColumnState::conflict;
  sde::WriteFence fence(backend, "t", project);
  EXPECT_THROW((void)fence.state(), sde::MigrationRefused);
}

TEST(WriteFence, AFailedResponseLeavesTheTableToInspect) {
  // The DDL took effect and the response was lost: the next call reads the constraints that are
  // there, so a retry resumes rather than repeating or assuming a rollback.
  sde::testing::MemoryFences backend;
  backend.fail_after = 3;
  sde::WriteFence fence(backend, "t", std::string(32, 'a'));
  EXPECT_THROW((void)fence.prepare(1), sde::EngineError);
  backend.fail_after.reset();
  const sde::FenceState state = fence.resume_prepare(1);
  EXPECT_TRUE(state.complete());
  EXPECT_EQ(state.epoch(), 1);
  EXPECT_TRUE(state.holds.empty());
}

}  // namespace
