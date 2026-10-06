/// The placement map loader, where the shared vectors do not reach: the log it writes, the text
/// overload, number spellings, and the refusals this library makes that the two reference libraries
/// do not yet (the third implementation's findings, to become shared vectors).

#include <string>
#include <utility>
#include <vector>

#include <gtest/gtest.h>

#include "sde/errors.hpp"
#include "sde/model.hpp"
#include "sde/placement.hpp"
#include "sde/routing.hpp"

namespace {

sde::Model events() {
  return sde::load_neutral_model(R"({"entities": [
    {"name": "Event", "fields": [{"name": "id", "type": "int64"},
                                 {"name": "at", "type": "timestamptz"}], "key": ["id", "at"]}]})");
}

/// A contract-3 map placing the one group, with `source` and the rest of the group's body given.
std::string map_with(const sde::Model& model, const std::string& group_body,
                     const std::string& extra = "") {
  return R"({"contract": 3, "model_version": ")" + model.version() +
         R"(", "map_version": 1, "groups": {"Event": {)" + group_body + "}}" + extra + "}";
}

const std::string kSource = R"("source": {"id": "Event@pg", "engine": "pg", "layout": {"auto": true}})";

sde::LoadOptions with(const sde::Model& model) {
  sde::LoadOptions options;
  options.model = &model;
  return options;
}

std::string refusal(const std::string& text, const sde::LoadOptions& options) {
  try {
    (void)sde::load_map(std::string_view(text), options);
  } catch (const sde::MapError& error) {
    return error.what();
  }
  ADD_FAILURE() << "the map was accepted: " << text;
  return "";
}

#define EXPECT_REFUSED(text, options, fragment)                                    \
  do {                                                                             \
    const std::string message_ = refusal((text), (options));                       \
    EXPECT_NE(message_.find(fragment), std::string::npos) << "message: " << message_; \
  } while (false)

TEST(LoadMap, AcceptsAHandWrittenMapAndDerivesItsLayout) {
  const sde::Model model = events();
  const sde::PlacementMap map = sde::load_map(std::string_view(map_with(model, kSource)), with(model));
  EXPECT_EQ(map.contract(), 3);
  EXPECT_EQ(map.map_version(), 1);
  EXPECT_FALSE(map.is_signed());
  EXPECT_FALSE(map.verified_with().has_value());
  EXPECT_FALSE(map.project_id().has_value());
  ASSERT_TRUE(map.fingerprint().has_value());
  EXPECT_EQ(map.fingerprint()->size(), 64U);
  const sde::GroupPlacement& group = map.placement_of("Event");
  EXPECT_EQ(group.source.layout.tables.at("Event"), "event");
  EXPECT_EQ(group.source.layout.columns.at("Event").at("at"), "timestamptz");
  EXPECT_TRUE(group.source.is_source());
  EXPECT_EQ(group.all().size(), 1U);
}

TEST(LoadMap, TextThatIsNotJsonIsAMapError) {
  const sde::Model model = events();
  EXPECT_REFUSED("{\"contract\": 3,", with(model), "a placement map is a JSON document");
  EXPECT_REFUSED("\xef\xbb\xbf{}", with(model), "a placement map is a JSON document");
  EXPECT_REFUSED("[]", with(model), "a placement map is an object");
}

TEST(LoadMap, LogsBothOutcomes) {
  const sde::Model model = events();
  std::vector<std::pair<std::string, sde::Json>> events_seen;
  sde::LoadOptions options = with(model);
  options.log = [&](std::string_view event, const sde::Json& fields) {
    events_seen.emplace_back(std::string(event), fields);
  };
  (void)sde::load_map(std::string_view(map_with(model, kSource)), options);
  ASSERT_EQ(events_seen.size(), 1U);
  EXPECT_EQ(events_seen[0].first, "sde.map.loaded");
  EXPECT_EQ(sde::dump_json(events_seen[0].second),
            R"({"model_version":")" + model.version() +
                R"(","map_version":1,"signed":false,"forward_only":false,"key":null})");

  // A refusal is logged too, its reason cut at 200 characters, as the reference cuts it.
  const std::string too_new = R"({"contract": 7, "model_version": "x", "map_version": 1})";
  EXPECT_THROW((void)sde::load_map(std::string_view(too_new), options), sde::MapError);
  ASSERT_EQ(events_seen.size(), 2U);
  EXPECT_EQ(events_seen[1].first, "sde.map.rejected");
  EXPECT_EQ(events_seen[1].second.find("error")->as_string(), "MapError");
  const std::string& reason = events_seen[1].second.find("reason")->as_string();
  EXPECT_EQ(reason.size(), 200U);
  EXPECT_EQ(reason.rfind("this map declares format contract 7 and this library implements 6", 0),
            0U);

  EXPECT_THROW((void)sde::load_map(std::string_view("not json"), options), sde::MapError);
  ASSERT_EQ(events_seen.size(), 3U);
  EXPECT_EQ(events_seen[2].first, "sde.map.rejected");
}

TEST(LoadMap, TheContractIsReadAsTheReferenceReadsIt) {
  const sde::Model model = events();
  // 4.0 is contract 4 (section 7d normalises integral numbers before anything else) ...
  const std::string four = R"({"contract": 4.0, "model_version": ")" + model.version() +
                           R"(", "map_version": 2.0, "project_id": ")" + std::string(32, 'a') +
                           R"(", "groups": {"Event": {)" + kSource + R"(, "write_epoch": 3e0}}})";
  const sde::PlacementMap map = sde::load_map(std::string_view(four), with(model));
  EXPECT_EQ(map.contract(), 4);
  EXPECT_EQ(map.map_version(), 2);
  EXPECT_EQ(map.placement_of("Event").write_epoch, 3);
  EXPECT_EQ(map.project_id(), std::string(32, 'a'));
  // ... and 3.0 is not a version: below 4 nothing is normalised.
  EXPECT_REFUSED(R"({"contract": 3.0})", with(model),
                 "declares format contract 3.0, which is not a version number");
  EXPECT_REFUSED(R"({"contract": "6"})", with(model),
                 "declares format contract '6', which is not a version number");
  EXPECT_REFUSED(R"({"model_version": "x"})", with(model), "declares format contract None");
  // An integer past 64 bits is a version from the future, or from before the floor.
  EXPECT_REFUSED(R"({"contract": 99999999999999999999})", with(model),
                 "declares format contract 99999999999999999999 and this library implements 6");
  EXPECT_REFUSED(R"({"contract": -99999999999999999999})", with(model),
                 "the oldest this library still reads is 1");
  EXPECT_REFUSED(R"({"contract": 0})", with(model), "the oldest this library still reads is 1");
}

TEST(LoadMap, AMaterialisationIdAndEngineAreStrings) {
  const sde::Model model = events();
  EXPECT_REFUSED(
      map_with(model, R"("source": {"id": true, "engine": "pg", "layout": {"auto": true}})"),
      with(model), "group 'Event'.source: a materialisation id is a string, not True");
  EXPECT_REFUSED(
      map_with(model, R"("source": {"id": "Event@pg", "engine": 5, "layout": {"auto": true}})"),
      with(model), "group 'Event'.source: an engine is named by a string, not 5");
  EXPECT_REFUSED(map_with(model, R"("source": "Event@pg")"), with(model),
                 "group 'Event'.source: expected an object");
}

TEST(LoadMap, ALagBudgetIsANonNegativeIntegralNumber) {
  const sde::Model model = events();
  const auto derived = [&](const std::string& lag) {
    return map_with(model, kSource + R"(, "derived": [{"id": "Event@ch", "engine": "ch",
        "layout": {"tables": {"Event": "event_wide"}}, "lag_budget_ms": )" +
                               lag + "}]");
  };
  const sde::PlacementMap map = sde::load_map(std::string_view(derived("30000.0")), with(model));
  EXPECT_EQ(map.placement_of("Event").derived.at(0).lag_budget_ms, 30000);
  EXPECT_FALSE(map.placement_of("Event").derived.at(0).is_source());
  EXPECT_REFUSED(derived("-1"), with(model), "lag_budget_ms must be a non-negative integer");
  EXPECT_REFUSED(derived("1.5"), with(model), "not 1.5");
  EXPECT_REFUSED(derived(R"("30000")"), with(model), "not '30000'");
  EXPECT_REFUSED(derived("true"), with(model), "not True");
}

TEST(LoadMap, TablesAndColumnsAreNamesAndTypes) {
  const sde::Model model = events();
  const auto layout = [&](const std::string& body) {
    return map_with(model, R"("source": {"id": "Event@pg", "engine": "pg", "layout": )" + body + "}");
  };
  EXPECT_REFUSED(layout(R"({"tables": {"Event": 5}})"), with(model),
                 "tables['Event'] must be a table name, not 5");
  EXPECT_REFUSED(layout(R"({"tables": {"Event": ""}})"), with(model),
                 "tables['Event'] must be a table name, not ''");
  EXPECT_REFUSED(layout(R"({"tables": {"Event": "event"}, "columns": []})"), with(model),
                 "columns maps an entity to an object of column name -> type, not []");
  EXPECT_REFUSED(layout(R"({"tables": {"Event": "event"}, "columns": {"Event": "id"}})"),
                 with(model), "columns['Event'] must be an object of column name -> type");
  EXPECT_REFUSED(layout(R"({"tables": {"Event": "event"}, "columns": {"Event": {"id": 5}}})"),
                 with(model), "columns['Event']['id'] must be a type name, not 5");
  const sde::PlacementMap map = sde::load_map(
      std::string_view(layout(R"({"tables": {"Event": "event"}, "columns": null})")), with(model));
  EXPECT_TRUE(map.placement_of("Event").source.layout.columns.empty());
}

TEST(LoadMap, DerivedIsAList) {
  const sde::Model model = events();
  EXPECT_REFUSED(map_with(model, kSource + R"(, "derived": {})"), with(model),
                 "group 'Event': 'derived' is a list of materialisations, not {}");
  const sde::PlacementMap map =
      sde::load_map(std::string_view(map_with(model, kSource + R"(, "derived": null)")), with(model));
  EXPECT_TRUE(map.placement_of("Event").derived.empty());
}

TEST(LoadMap, AnAutoLayoutNeedsTheModel) {
  const sde::Model model = events();
  EXPECT_REFUSED(map_with(model, kSource), sde::LoadOptions{},
                 "a layout asked to be derived with {\"auto\": true}, but no model was supplied");
  // Explicit layouts load without one.
  const std::string explicit_map = map_with(
      model, R"("source": {"id": "Event@pg", "engine": "pg", "layout": {"tables": {"Event": "e"}}})");
  EXPECT_EQ(sde::load_map(std::string_view(explicit_map)).placement_of("Event").source.layout.tables.at("Event"),
            "e");
}

TEST(LoadMap, ACopyInTheSourcesEngineMayNotBeTheSource) {
  const sde::Model model = events();
  const std::string shadow = map_with(
      model, kSource + R"(, "derived": [{"id": "Event@pg2", "engine": "pg", "layout": {"auto": true},
                                         "lag_budget_ms": 0}])");
  EXPECT_REFUSED(shadow, with(model),
                 "materialisation 'Event@pg2' is in the same engine as the source and reuses its "
                 "tables ['event']");
  // The same layout in another engine is a copy.
  const std::string copy = map_with(
      model, kSource + R"(, "derived": [{"id": "Event@pg2", "engine": "pg-2", "layout": {"auto": true},
                                         "lag_budget_ms": 0}])");
  const sde::PlacementMap map = sde::load_map(std::string_view(copy), with(model));
  EXPECT_EQ(map.placement_of("Event").by_id("Event@pg2").layout.tables.at("Event"), "event");
}

TEST(LoadMap, ALegacyMapWithoutACanonicalFormLoadsWithoutAFingerprint) {
  const sde::Model model = events();
  const sde::PlacementMap legacy =
      sde::load_map(std::string_view(map_with(model, kSource, R"(, "note": 1.5)")), with(model));
  EXPECT_FALSE(legacy.fingerprint().has_value());
  const std::string four = R"({"contract": 4, "model_version": ")" + model.version() +
                           R"(", "map_version": 1, "project_id": ")" + std::string(32, '0') +
                           R"(", "groups": {"Event": {)" + kSource +
                           R"(, "write_epoch": 1}}, "note": 1.5})";
  EXPECT_REFUSED(four, with(model), "contract 4 requires a canonically encodable placement map");
}

TEST(LoadMap, ASignedMapWithoutACanonicalFormIsAMapError) {
  const sde::Model model = events();
  const std::string signed_map = map_with(
      model, kSource, R"(, "note": 1.5, "signature": {"alg": "ed25519", "value": ")" +
                          std::string(86, 'A') + R"(=="})");
  sde::LoadOptions options = with(model);
  options.public_keys = sde::PublicKeys::bare(std::string(32, '\x01'));
  EXPECT_REFUSED(signed_map, options, "its payload has no canonical form");
}

TEST(LoadMap, SignatureValuesAreDecodedAsTheReferenceDecodesThem) {
  const sde::Model model = events();
  sde::LoadOptions options = with(model);
  options.public_keys = sde::PublicKeys::bare(std::string(32, '\x01'));
  const auto with_value = [&](const std::string& value) {
    return map_with(model, kSource,
                    R"(, "signature": {"alg": "ed25519", "value": )" + value + "}");
  };
  EXPECT_REFUSED(with_value("5"), options, "the signature is not valid base64");
  EXPECT_REFUSED(with_value(R"("AAAA AAAA")"), options, "the signature is not valid base64");
  EXPECT_REFUSED(with_value(R"("AAA")"), options, "the signature is not valid base64");
  // Excess padding after a whole quantum decodes, as in Python 3.12; the bytes then do not verify.
  EXPECT_REFUSED(with_value(R"("AAAA==")"), options, "does not verify against any of the 1 key");
  EXPECT_REFUSED(map_with(model, kSource, R"(, "signature": "abc")"), options,
                 "only ed25519 signatures are understood");
  EXPECT_REFUSED(map_with(model, kSource, R"(, "signature": {"alg": "rsa", "value": "AAAA"})"),
                 options, "only ed25519 signatures are understood");
  // A null signature is no signature.
  EXPECT_FALSE(sde::load_map(std::string_view(map_with(model, kSource, R"(, "signature": null)")),
                             options)
                   .is_signed());
}

TEST(LoadMap, ABadKeyIsTheCallersConfiguration) {
  const sde::Model model = events();
  sde::LoadOptions options = with(model);
  const std::string signed_map =
      map_with(model, kSource, R"(, "signature": {"alg": "ed25519", "key_id": "k9", "value": ")" +
                                   std::string(86, 'A') + R"(=="})");
  options.public_keys = sde::PublicKeys::bare(std::string(31, '\x01'));
  EXPECT_REFUSED(signed_map, options, "the public key '(unnamed)' is 31 bytes");
  options.public_keys = sde::PublicKeys::named({{"k1", std::string(32, '\x01')},
                                                {"k2", std::string(33, '\x01')}});
  EXPECT_REFUSED(signed_map, options, "the public key 'k2' is 33 bytes");
  options.public_keys = sde::PublicKeys::named({});
  EXPECT_REFUSED(signed_map, options, "an empty set of public keys");
  options.public_keys = sde::PublicKeys::named({{"k1", std::string(32, '\x01')},
                                                {"k2", std::string(32, '\x02')}});
  EXPECT_REFUSED(signed_map, options,
                 "does not verify against any of the 2 key(s) supplied (['k1', 'k2']); the map "
                 "says it was signed with 'k9'");
}

TEST(PlacementMap, AskingForWhatTheMapDoesNotHaveIsAMapError) {
  const sde::Model model = events();
  const sde::PlacementMap map = sde::load_map(std::string_view(map_with(model, kSource)), with(model));
  EXPECT_THROW((void)map.placement_of("Order"), sde::MapError);
  EXPECT_THROW((void)map.placement_of("Event").by_id("Event@ch"), sde::MapError);
  EXPECT_THROW((void)map.placement_of("Event").source.layout.table_for("Order"), sde::MapError);
  EXPECT_EQ(map.placement_of("Event").source.layout.table_for("Event"), "event");
}

TEST(Resolve, WritesGoToTheSourceWhateverTheRoutingSays) {
  const sde::Model model = events();
  std::string routing = R"(, "routing": {)";
  bool first = true;
  for (const sde::OperationShape& shape : model.shapes()) {
    routing += std::string(first ? "" : ", ") + "\"" + shape.id + "\": \"Event@ch\"";
    first = false;
  }
  routing += "}";
  const std::string text = map_with(
      model, kSource + R"(, "derived": [{"id": "Event@ch", "engine": "ch",
                                         "layout": {"tables": {"Event": "event_wide"}},
                                         "lag_budget_ms": 1000}])",
      routing);
  const sde::PlacementMap map = sde::load_map(std::string_view(text), with(model));
  for (const sde::OperationShape& shape : model.shapes()) {
    const std::string& got = sde::resolve(map, shape).id;
    EXPECT_EQ(got, sde::is_write_kind(shape.kind) ? "Event@pg" : "Event@ch") << shape.kind;
    EXPECT_EQ(sde::resolve(map, shape, {true, false}).id, "Event@pg");
    EXPECT_EQ(sde::resolve(map, shape, {false, true}).id, "Event@pg");
  }
}

}  // namespace
