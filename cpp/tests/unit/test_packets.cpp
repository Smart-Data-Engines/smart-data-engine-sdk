/// Signed packets, where the shared vectors do not reach: what a plan gives an operator beyond the
/// fields a vector pins, the refusals that name the packet they refuse, and the two name builders.
///
/// The packets are the vectors' own, re-signed here with a key made for the test, so a change
/// reaches the rule it is about rather than stopping at a signature that no longer verifies.

#include <functional>
#include <memory>
#include <string>
#include <string_view>
#include <typeinfo>
#include <vector>

#include <gtest/gtest.h>
#include <openssl/evp.h>

#include "crypto.hpp"
#include "encoding.hpp"
#include "sde/canonical.hpp"
#include "sde/errors.hpp"
#include "sde/model.hpp"
#include "sde/packets.hpp"
#include "sde/placement.hpp"
#include "support/vectors.hpp"

namespace {

using sde::testing_support::read_json;
using sde::testing_support::vectors_root;

constexpr std::string_view kProject = "11111111111111111111111111111111";

/// An Ed25519 key for the test, from a fixed seed: the library only verifies, so the signing is
/// OpenSSL's, called directly.
class Signer {
 public:
  Signer() {
    const sde::detail::Digest seed = sde::detail::sha256("the packets' unit tests");
    key_.reset(EVP_PKEY_new_raw_private_key(EVP_PKEY_ED25519, nullptr, seed.data(), seed.size()));
  }

  [[nodiscard]] std::string public_key() const {
    unsigned char raw[32];
    std::size_t length = sizeof raw;
    EXPECT_EQ(EVP_PKEY_get_raw_public_key(key_.get(), raw, &length), 1);
    return {reinterpret_cast<const char*>(raw), length};
  }

  [[nodiscard]] std::string sign(std::string_view message) const {
    const std::unique_ptr<EVP_MD_CTX, decltype(&EVP_MD_CTX_free)> context(EVP_MD_CTX_new(),
                                                                          &EVP_MD_CTX_free);
    unsigned char signature[64];
    std::size_t length = sizeof signature;
    EXPECT_EQ(EVP_DigestSignInit(context.get(), nullptr, nullptr, nullptr, key_.get()), 1);
    EXPECT_EQ(EVP_DigestSign(context.get(), signature, &length,
                             reinterpret_cast<const unsigned char*>(message.data()), message.size()),
              1);
    return {reinterpret_cast<const char*>(signature), length};
  }

  [[nodiscard]] sde::PublicKeys keys() const { return sde::PublicKeys::named({{"k", public_key()}}); }

  /// The document signed as the control plane signs it: over its canonical bytes without the block.
  [[nodiscard]] sde::Json signed_document(sde::Json document) const {
    (void)document.erase("signature");
    sde::Json block = sde::Json::object();
    block.set("alg", "ed25519");
    block.set("key_id", "k");
    block.set("value", sde::detail::base64_encode(sign(sde::canonical_bytes(document))));
    document.set("signature", std::move(block));
    return document;
  }

  /// A packet with each of its maps re-signed, then the packet around them.
  [[nodiscard]] sde::Json resigned(sde::Json packet,
                                   std::initializer_list<std::string_view> maps) const {
    for (std::string_view name : maps) {
      packet.set(std::string(name), signed_document(*packet.find(name)));
    }
    return signed_document(std::move(packet));
  }

 private:
  std::unique_ptr<EVP_PKEY, decltype(&EVP_PKEY_free)> key_{nullptr, &EVP_PKEY_free};
};

struct Case {
  sde::Model model;
  sde::Json packet;
};

Case vector_case(std::string_view name) {
  const auto directory = vectors_root() / "migration" / std::string(name);
  return {sde::load_neutral_model(read_json(directory / "model.json")),
          read_json(directory / "plan.json")};
}

std::string refusal(const std::function<void()>& load) {
  try {
    load();
  } catch (const sde::MigrationRefused& error) {
    EXPECT_EQ(typeid(error), typeid(sde::MigrationRefused)) << error.what();
    return error.what();
  }
  ADD_FAILURE() << "the packet was accepted";
  return "";
}

const sde::Json& at(const sde::Json& document, std::initializer_list<std::string_view> path) {
  const sde::Json* value = &document;
  for (std::string_view step : path) value = value->find(step);
  return *value;
}

sde::Json& at(sde::Json& document, std::initializer_list<std::string_view> path) {
  sde::Json* value = &document;
  for (std::string_view step : path) value = value->find(step);
  return *value;
}

// ── Cutover ─────────────────────────────────────────────────────────────────────────────────────

class CutoverPacket : public ::testing::Test {
 protected:
  Signer signer;
  Case base = vector_case("079-cutover-packet-authorizes-one-group");

  [[nodiscard]] sde::Json resigned(sde::Json packet) const {
    return signer.resigned(std::move(packet), {"before", "success", "abort"});
  }
  [[nodiscard]] sde::CutoverPlan load(const sde::Json& packet) const {
    return sde::load_cutover_plan(packet, base.model, std::string(kProject), signer.keys());
  }
};

TEST_F(CutoverPacket, LoadsWhatTheControlPlaneSigned) {
  const sde::CutoverPlan plan = load(resigned(base.packet));
  EXPECT_EQ(plan.verified_with(), "k");
  EXPECT_EQ(plan.protocol(), sde::CUTOVER_PROTOCOL);
  EXPECT_EQ(plan.group(), "Event");
  EXPECT_EQ(plan.pause_budget_ms(), 5000);
  EXPECT_EQ(plan.verification().group(), "Event");
  EXPECT_EQ(plan.source_epoch(), 1);
  EXPECT_EQ(plan.activation_epoch(), 3);
}

TEST_F(CutoverPacket, AnIntegralNumberIsAnIntegerHoweverItIsWritten) {
  // Section 7d: the signature and the fingerprint are over the integer, so `5000.0` is `5000`.
  const sde::Json packet = resigned(base.packet);
  sde::Json spelled = packet;
  spelled.set("pause_budget_ms", sde::Json::from_lexeme("5000.0"));
  const sde::CutoverPlan plan = load(spelled);
  EXPECT_EQ(plan.fingerprint(), load(packet).fingerprint());
  EXPECT_TRUE(plan.as_record().find("pause_budget_ms")->as_number().integer);
}

TEST_F(CutoverPacket, AnOutcomeIsSuccessOrAbort) {
  const sde::CutoverPlan plan = load(resigned(base.packet));
  EXPECT_EQ(plan.candidate_payload("success"), sde::canonical_bytes(at(plan.as_record(), {"success"})));
  EXPECT_NE(plan.candidate_payload("success"), plan.candidate_payload("abort"));
  EXPECT_EQ(refusal([&] { (void)plan.candidate_payload("rollback"); }),
            "cutover outcome must be success or abort");
}

TEST_F(CutoverPacket, StartsOnlyFromTheSignedMapItNames) {
  const sde::CutoverPlan plan = load(resigned(base.packet));
  plan.check_current(plan.before());
  EXPECT_EQ(refusal([&] { plan.check_current(plan.success()); }),
            "cutover plan does not name the current placement map");
  // The same map, unsigned: the client's own document is not the one this plan starts from.
  sde::LoadOptions options;
  options.model = &base.model;
  const sde::PlacementMap unsigned_map = sde::load_map(
      [&] {
        sde::Json document = at(plan.as_record(), {"before"});
        (void)document.erase("signature");
        return document;
      }(),
      options);
  EXPECT_EQ(refusal([&] { plan.check_current(unsigned_map); }),
            "cutover plan does not name the current placement map");
}

TEST_F(CutoverPacket, AMapOrEncodingRefusalInsideItIsTheCutovers) {
  sde::Json packet = resigned(base.packet);
  at(packet, {"verification"}).set("note", sde::Json::from_lexeme("1.5"));
  EXPECT_EQ(refusal([&] { (void)load(packet); }).rfind("cutover document refused: float at "
                                                       "$.verification.note",
                                                       0),
            0U);
  sde::Json wrong_model = base.packet;
  at(wrong_model, {"success"}).set("model_version", "0000000000000000");
  EXPECT_EQ(refusal([&] { (void)load(resigned(wrong_model)); }).rfind("cutover document refused: ", 0),
            0U);
}

// ── Staging ─────────────────────────────────────────────────────────────────────────────────────

class StagingPacket : public ::testing::Test {
 protected:
  Signer signer;
  Case base = vector_case("099-staging-authorizes-one-fresh-copy");

  [[nodiscard]] sde::Json resigned(sde::Json packet) const {
    return signer.resigned(std::move(packet), {"current", "prepared"});
  }
  [[nodiscard]] sde::StagingPlan load(const sde::Json& packet) const {
    return sde::load_staging_plan(packet, base.model, std::string(kProject), signer.keys());
  }
};

TEST_F(StagingPacket, LoadsWhatTheControlPlaneSigned) {
  const sde::StagingPlan plan = load(resigned(base.packet));
  EXPECT_EQ(plan.verified_with(), "k");
  EXPECT_EQ(plan.protocol(), sde::STAGING_PROTOCOL);
  EXPECT_EQ(plan.prepared_payload(), sde::canonical_bytes(at(plan.as_record(), {"prepared"})));
  plan.check_current(plan.current());
  EXPECT_EQ(refusal([&] { plan.check_current(plan.prepared()); }),
            "staging authorization does not name the signed current map");
}

TEST_F(StagingPacket, ItsRefusalsNameAStaging) {
  // The reference's helpers shared with the cutover packet say "cutover" here; this library names
  // the packet it refuses (the third implementation's findings, implementing.md).
  sde::Json packet = resigned(base.packet);
  packet.set("stage_id", "STAGE");
  EXPECT_EQ(refusal([&] { (void)load(packet); }),
            "staging stage_id must be 32 lowercase hexadecimal digits");
  packet = resigned(base.packet);
  (void)at(packet, {"current"}).erase("signature");
  EXPECT_EQ(refusal([&] { (void)load(signer.signed_document(packet)); }),
            "staging signature must be an object");
  EXPECT_EQ(refusal([&] { (void)load(sde::Json::array()); }),
            "staging authorization must be an object");
}

TEST_F(StagingPacket, ItsCopyTakesTheNamesOfItsStage) {
  const sde::StagingPlan plan = load(resigned(base.packet));
  for (const auto& [entity, table] :
       plan.prepared().placement_of(plan.group()).derived.at(0).layout.tables) {
    EXPECT_EQ(table.rfind("sde_m_" + plan.stage_id() + "_", 0), 0U) << entity;
  }
}

// ── Index build ─────────────────────────────────────────────────────────────────────────────────

class IndexPacket : public ::testing::Test {
 protected:
  Signer signer;

  [[nodiscard]] sde::IndexPlan load(const Case& base) const {
    return sde::load_index_plan(signer.resigned(base.packet, {"current", "prepared"}), base.model,
                                std::string(kProject), signer.keys());
  }
};

TEST_F(IndexPacket, GivesTheOperatorWholeDefinitions) {
  // A replacement: one index in force dropped, one added. The operator builds and drops from the
  // definitions, so they are the loaded maps' - every field, the method included when the map
  // leaves it to its default.
  const sde::IndexPlan plan = load(vector_case("181-index-change-replaces-an-index"));
  EXPECT_EQ(plan.protocol(), sde::INDEX_CHANGE_PROTOCOL);
  ASSERT_EQ(plan.added().size(), 1U);
  EXPECT_EQ(plan.added()[0].name, sde::index_build_name(plan.index_id(), 1));
  EXPECT_EQ(plan.added()[0].entity, "Event");
  EXPECT_EQ(plan.added()[0].columns, std::vector<std::string>{"at"});
  EXPECT_EQ(plan.added()[0].method, "btree");
  ASSERT_EQ(plan.removed().size(), 1U);
  EXPECT_EQ(plan.removed()[0].name, "event_name");
  EXPECT_EQ(plan.removed()[0].columns, std::vector<std::string>{"name"});
  plan.check_current(plan.current());
  EXPECT_EQ(refusal([&] { plan.check_current(plan.prepared()); }),
            "index build authorization does not name the signed current map");
}

TEST_F(IndexPacket, ABuildRemovesNothing) {
  const sde::IndexPlan plan = load(vector_case("143-index-build-adds-a-btree-in-place"));
  EXPECT_EQ(plan.protocol(), sde::INDEX_PROTOCOL);
  EXPECT_EQ(plan.added().size(), 1U);
  EXPECT_TRUE(plan.removed().empty());
}

// ── Names ───────────────────────────────────────────────────────────────────────────────────────

TEST(PacketNames, AStagedTableIsItsStageAndItsPosition) {
  const std::string stage(32, 'a');
  EXPECT_EQ(sde::staging_table_name(stage, 1), "sde_m_" + stage + "_000001");
  EXPECT_EQ(sde::staging_table_name(stage, 999999), "sde_m_" + stage + "_999999");
  for (const std::int64_t position : {std::int64_t{0}, std::int64_t{-1}, std::int64_t{1000000}}) {
    EXPECT_EQ(refusal([&] { (void)sde::staging_table_name(stage, position); }),
              "staging entity position must be an integer from 1 through 999999");
  }
  EXPECT_EQ(refusal([&] { (void)sde::staging_table_name(std::string(32, 'A'), 1); }),
            "staging stage_id must be 32 lowercase hexadecimal digits");
}

TEST(PacketNames, ABuiltIndexIsItsBuildAndItsPosition) {
  const std::string build(32, '6');
  EXPECT_EQ(sde::index_build_name(build, 12), "sde_i_" + build + "_000012");
  for (const std::int64_t position : {std::int64_t{0}, std::int64_t{1000000}}) {
    EXPECT_EQ(refusal([&] { (void)sde::index_build_name(build, position); }),
              "index build position must be an integer from 1 through 999999");
  }
  EXPECT_EQ(refusal([&] { (void)sde::index_build_name(std::string(31, '6'), 1); }),
            "index build index_id must be 32 lowercase hexadecimal digits");
}

}  // namespace
