#include "conformance/stages.hpp"

#include <map>
#include <memory>
#include <optional>
#include <vector>

#include <gtest/gtest.h>

#include "encoding.hpp"
#include "sde/model.hpp"
#include "sde/placement.hpp"
#include "sde/session.hpp"
#include "sde/testing/memory.hpp"
#include "support/vectors.hpp"

namespace sde::testing_support {

namespace {

/// The loader's arguments a case may carry in `expected.json` - not anything about the refusal
/// (`errors/015` needs `require_signature`, since its map is unsigned and an unsigned map is
/// accepted).
sde::LoadOptions load_options(const sde::Model& model, const sde::Json& expected) {
  sde::LoadOptions options;
  options.model = &model;
  if (const sde::Json* load = expected.find("load")) {
    if (const sde::Json* key = load->find("public_key"); key != nullptr && key->is_string()) {
      const auto decoded = sde::detail::base64_decode(key->as_string());
      EXPECT_TRUE(decoded.has_value()) << "the case's public key is not base64";
      if (decoded) options.public_keys = sde::PublicKeys::bare(*decoded);
    }
    if (const sde::Json* required = load->find("require_signature"); required != nullptr) {
      options.require_signature = required->is_bool() && required->as_bool();
    }
  }
  return options;
}

void write(sde::Session& session, const sde::Json& step) {
  const std::string& entity = step.find("entity")->as_string();
  if (step.find("operation")->as_string() == "save") {
    session.save(entity, sde::testing::row_from_json(*step.find("values")));
    return;
  }
  std::vector<sde::Row> rows;
  for (const sde::Json& row : step.find("rows")->as_array()) {
    rows.push_back(sde::testing::row_from_json(row));
  }
  session.save_many(entity, rows);
}

}  // namespace

void run_map_stage(const std::filesystem::path& directory, const sde::Json& expected,
                   const std::string& error, const std::string& match) {
  // The model of a map-stage case is valid, and it is built outside the assertion on purpose: a
  // model broken by accident would otherwise fail with the right class at the wrong stage.
  const sde::Model model = sde::load_neutral_model(read_json(directory / "model.json"));
  const sde::LoadOptions options = load_options(model, expected);
  const sde::Json document = read_json(directory / "map.json");
  expect_refusal([&] { (void)sde::load_map(document, options); }, error, match);
}

void run_engine_stage(const std::filesystem::path& directory, const sde::Json& expected,
                      const std::string& stage, const std::string& error,
                      const std::string& match) {
  // The model and the map are valid, and both are loaded outside the assertion, for the reason the
  // map stage builds its model outside: the refusal must come from the door being tested.
  const sde::Model model = sde::load_neutral_model(read_json(directory / "model.json"));
  const sde::PlacementMap map =
      sde::load_map(read_json(directory / "map.json"), load_options(model, expected));
  const auto engines = sde::testing::engines_from(read_json(directory / "engines.json"));
  std::map<std::string, sde::Engine*> pointers;
  for (const auto& [name, engine] : engines) pointers.emplace(name, engine.get());
  ASSERT_FALSE(engines.empty());
  const sde::testing::Recorded& journal = engines.begin()->second->recorded();

  if (stage == "session") {
    // A rule no document can answer: a map names engines and carries no dialect, so the first door
    // holding the adapters is the one that can. It must also have cost nothing.
    expect_refusal([&] { const sde::Session session(model, map, pointers); }, error, match);
    EXPECT_EQ(journal.as_json(), read_json(directory / "calls.json"))
        << "the refusal is right and it was not free. Nothing may be created, read or written on "
           "the strength of a map this library is about to reject.";
    return;
  }

  // The write stage: a row the model does not allow, refused before any engine is called. One
  // accepted write comes first and is the control - the engine records what it receives, so the
  // empty rest of `calls.json` says the refused row reached nothing.
  sde::Session session(model, map, pointers);
  const sde::Json& steps = *expected.find("write");
  write(session, *steps.find("accepted"));
  try {
    write(session, *steps.find("refused"));
    ADD_FAILURE() << directory.filename() << ": the row was accepted";
  } catch (const std::exception& refused) {
    // The class exactly: BulkWriteRefused is a ModelPlanningError, and a vector naming the parent
    // must not be satisfied by the child, or the reverse.
    EXPECT_EQ(exact_class(refused), error) << refused.what();
    EXPECT_NE(std::string_view(refused.what()).find(match), std::string_view::npos)
        << "the message must contain \"" << match << "\": " << refused.what();
  }
  EXPECT_EQ(journal.as_json(), read_json(directory / "calls.json"))
      << "the refusal is right and it came too late. A row the model does not allow must not "
         "reach an engine, which would store it or answer in its own words.";
}

}  // namespace sde::testing_support
