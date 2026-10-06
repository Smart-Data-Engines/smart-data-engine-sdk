/// `migration/` (Tier 2, second half): taking part in a migration.
///
/// Two things are pinned, and the second is the unusual one. **The record** is what a gate on our
/// side reads. **The calls** are how it was obtained: a library that reached the same counts by
/// scanning a whole table and filtering in memory would satisfy every number and be unusable on a
/// real one. The fixture is the library's own in-memory engine, never one written here.
///
/// A case names its driver by the document it carries. Until this library claims Tier 2, a case
/// whose driver is not written yet is skipped by name; from Tier 2 on, the same case fails.

#include <algorithm>
#include <functional>
#include <map>
#include <memory>
#include <optional>
#include <string>
#include <vector>

#include <gtest/gtest.h>

#include "encoding.hpp"
#include "sde/canonical.hpp"
#include "sde/migration.hpp"
#include "sde/model.hpp"
#include "sde/placement.hpp"
#include "sde/session.hpp"
#include "sde/telemetry.hpp"
#include "sde/verification.hpp"
#include "sde/testing/memory.hpp"
#include "sde/version.hpp"
#include "sde/watermark.hpp"
#include "sde/write_fence.hpp"
#include "support/vectors.hpp"

namespace {

using namespace sde::testing_support;
using sde::testing::MemoryEngine;

using Engines = std::map<std::string, std::unique_ptr<MemoryEngine>>;

/// The drivers this runner has, by the document that selects them, and the ones still to come.
const std::vector<std::string>& written_drivers() {
  static const std::vector<std::string> drivers = {
      "fencing", "watermark", "operations", "bulk", "generation", "backfill", "verify",
      "verification", "frozen"};
  return drivers;
}
const std::vector<std::string>& pending_drivers() {
  static const std::vector<std::string> drivers = {"staging", "index", "cutover"};
  return drivers;
}

std::map<std::string, sde::Engine*> pointers(const Engines& engines) {
  std::map<std::string, sde::Engine*> out;
  for (const auto& [name, engine] : engines) out.emplace(name, engine.get());
  return out;
}

/// A row's text with its keys in order, so two rows compare the same way however a table holds
/// them: the vectors' row order is the reference's sort, and only the content is the claim.
std::string row_key(const sde::Json& row) { return sde::canonical_bytes(row); }

sde::Json sorted_rows(sde::Json rows) {
  auto& items = rows.as_array();
  std::sort(items.begin(), items.end(),
            [](const sde::Json& a, const sde::Json& b) { return row_key(a) < row_key(b); });
  return rows;
}

sde::Json table_json(const std::vector<sde::Row>& rows) {
  sde::Json out = sde::Json::array();
  for (const sde::Row& row : rows) out.as_array().push_back(sde::testing::row_to_json(row));
  return out;
}

/// Every engine's tables as one document, in insertion order.
sde::Json tables_json(const Engines& engines) {
  sde::Json out = sde::Json::object();
  for (const auto& [name, engine] : engines) {
    sde::Json tables = sde::Json::object();
    for (const auto& [table, rows] : engine->tables) tables.set(table, table_json(rows));
    out.set(name, std::move(tables));
  }
  return out;
}

sde::PlacementMap load_case_map(const std::filesystem::path& directory, const sde::Model& model) {
  sde::LoadOptions options;
  options.model = &model;
  if (has_file(directory / "load.json")) {
    const sde::Json load = read_json(directory / "load.json");
    if (const sde::Json* named = load.find("public_key"); named != nullptr && named->is_string()) {
      const sde::Json keys = read_json(directory / "keys.json");
      const auto decoded = sde::detail::base64_decode(keys.find(named->as_string())->as_string());
      EXPECT_TRUE(decoded.has_value());
      if (decoded) options.public_keys = sde::PublicKeys::bare(*decoded);
    }
    if (const sde::Json* required = load.find("require_signature")) {
      options.require_signature = required->is_bool() && required->as_bool();
    }
  }
  return sde::load_map(read_json(directory / "map.json"), options);
}

/// A rollback as an application's own failure looks: thrown from the transaction's body.
struct Rollback {};

// --- fencing.json -------------------------------------------------------------------------------

void drive_fencing(const std::filesystem::path& directory) {
  const sde::Json want = read_json(directory / "fencing.json");
  sde::testing::MemoryFences backend;
  const sde::Json& metadata = *want.find("metadata");
  backend.identity = metadata.find("identity")->as_string();
  const std::string& column = metadata.find("column")->as_string();
  backend.column = column == "valid"      ? sde::ColumnState::valid
                   : column == "conflict" ? sde::ColumnState::conflict
                                          : sde::ColumnState::absent;
  for (const auto& [name, expression] : metadata.find("constraints")->as_object()) {
    backend.constraints[name] = expression.as_string();
  }
  sde::WriteFence fence(backend, want.find("table")->as_string(),
                        want.find("project_id")->as_string());
  for (const sde::Json& step : want.find("steps")->as_array()) {
    const std::string& op = step.find("op")->as_string();
    const auto invoke = [&]() -> sde::FenceState {
      if (op == "state") return fence.state();
      if (op == "prepare") return fence.prepare(sde::epoch_from_json(*step.find("epoch")));
      if (op == "advance") return fence.advance(sde::epoch_from_json(*step.find("epoch")));
      if (op == "freeze") return fence.freeze(step.find("request_id")->as_string());
      if (op == "release") return fence.release(step.find("request_id")->as_string());
      ADD_FAILURE() << "unknown fencing fixture operation " << op;
      return {};
    };
    if (const sde::Json* error = step.find("error")) {
      expect_refusal([&] { (void)invoke(); }, error->as_string(), step.find("match")->as_string());
    } else {
      EXPECT_EQ(invoke().as_record(), *step.find("state")) << op;
    }
  }
  sde::Json calls = sde::Json::array();
  for (const sde::Json& call : backend.calls) calls.as_array().push_back(call);
  EXPECT_EQ(calls, read_json(directory / "calls.json"));
}

// --- generation.json ----------------------------------------------------------------------------

/// Each named engine gets write fences at the case's epochs, and with them the capability a
/// generation-bearing session asks for. With `record`, every fence call joins the engines' journal.
void bind_generation_metadata(Engines& engines, const sde::Json& metadata, bool record) {
  for (const auto& [name, tables] : metadata.as_object()) {
    MemoryEngine& engine = *engines.at(name);
    std::map<std::string, std::shared_ptr<sde::testing::MemoryFences>> backends;
    for (const auto& [table, generation] : tables.as_object()) {
      auto backend = std::make_shared<sde::testing::MemoryFences>();
      backend->identity = name + "/" + table;
      backend->column = sde::ColumnState::valid;
      const std::string project = generation.find("project_id")->as_string();
      const std::string epoch = std::to_string(*generation.find("epoch")->to_int64());
      const std::string prefix(sde::FENCE_PREFIX);
      const std::string column(sde::EPOCH_COLUMN);
      backend->constraints[prefix + "owner_" + project] = "1";
      backend->constraints[prefix + "min_" + epoch] = column + " >= " + epoch;
      backend->constraints[prefix + "max_" + epoch] = column + " <= " + epoch;
      if (record) {
        backend->on_call = [&engine, engine_name = name](const sde::Json& call) {
          const auto& items = call.as_array();
          sde::Json arguments = sde::Json::object();
          arguments.set("table", items.at(1));
          sde::Json rest = sde::Json::array();
          for (std::size_t i = 2; i < items.size(); ++i) rest.as_array().push_back(items[i]);
          arguments.set("arguments", std::move(rest));
          engine.recorded().note(engine_name, "fence_" + items.at(0).as_string(),
                                 std::move(arguments));
        };
      }
      backends.emplace(table, std::move(backend));
    }
    engine.bind_fences(std::move(backends));
  }
}

std::optional<std::string> project_of(const sde::Json& want) {
  const sde::Json* project = want.find("project_id");
  if (project == nullptr || project->is_null()) return std::nullopt;
  return project->as_string();
}

void drive_generation(const std::filesystem::path& directory, const sde::Model& model,
                      const sde::PlacementMap& map, Engines& engines) {
  const sde::Json want = read_json(directory / "generation.json");
  bind_generation_metadata(engines, *want.find("engine_generations"), false);
  sde::SessionOptions options;
  options.project_id = project_of(want);
  if (const sde::Json* error = want.find("error")) {
    expect_refusal([&] { const sde::Session session(model, map, pointers(engines), options); },
                   error->as_string(), want.find("match")->as_string());
  } else {
    sde::Session session(model, map, pointers(engines), options);
    for (const sde::Json& action : want.find("actions")->as_array()) {
      const std::string& op = action.find("op")->as_string();
      const auto invoke = [&] {
        if (op == "save") {
          session.save(action.find("entity")->as_string(),
                       sde::testing::row_from_json(*action.find("values")));
        } else if (op == "get") {
          const auto row = session.get(action.find("entity")->as_string(),
                                       sde::testing::row_from_json(*action.find("key")));
          const sde::Json got = row ? sde::testing::row_to_json(*row) : sde::Json(nullptr);
          EXPECT_EQ(got, *action.find("result"));
        } else if (op == "transaction") {
          std::vector<std::string> entities;
          for (const sde::Json& entity : action.find("entities")->as_array()) {
            entities.push_back(entity.as_string());
          }
          session.transaction(entities, [&] {
            if (const sde::Json* writes = action.find("writes")) {
              for (const sde::Json& write : writes->as_array()) {
                session.save(write.find("entity")->as_string(),
                             sde::testing::row_from_json(*write.find("values")));
              }
            }
          });
        } else if (op == "backfill") {
          sde::BackfillOptions backfill;
          backfill.chunk_rows = 3;
          (void)sde::backfill(session, action.find("group")->as_string(), backfill);
        } else if (op == "verify") {
          sde::VerifyOptions verify;
          verify.chunk_rows = 3;
          verify.at = action.find("at")->as_string();
          const sde::VerifyReport report =
              sde::verify(session, action.find("group")->as_string(), verify);
          EXPECT_EQ(report.matched(), action.find("matched")->as_bool());
          EXPECT_EQ(report.as_record(), *action.find("report"));
        } else {
          ADD_FAILURE() << "unknown generation fixture operation " << op;
        }
      };
      if (const sde::Json* refusal = action.find("error")) {
        expect_refusal(invoke, refusal->as_string(), action.find("match")->as_string());
      } else {
        invoke();
      }
    }
  }
  EXPECT_EQ(tables_json(engines), *want.find("tables"));
  EXPECT_EQ(engines.begin()->second->recorded().as_json(), read_json(directory / "calls.json"));
}

// --- bulk.json ----------------------------------------------------------------------------------

void drive_bulk(const std::filesystem::path& directory, const sde::Model& model,
                const sde::PlacementMap& map, Engines& engines) {
  const sde::Json want = read_json(directory / "bulk.json");
  sde::Recorder recorder(model);
  sde::SessionOptions options;
  options.recorder = &recorder;
  sde::Session session(model, map, pointers(engines), options);
  std::vector<std::string> errors;
  std::function<void(const sde::Json&)> run = [&](const sde::Json& steps) {
    for (const sde::Json& step : steps.as_array()) {
      try {
        if (step.find("op")->as_string() == "transaction") {
          try {
            session.transaction({"Reading"}, [&] {
              run(*step.find("body"));
              if (const sde::Json* rollback = step.find("rollback");
                  rollback != nullptr && rollback->as_bool()) {
                throw Rollback{};
              }
            });
          } catch (const Rollback&) {
          }
        } else {
          std::vector<sde::Row> rows;
          const sde::Json* repeat = step.find("repeat");
          const std::int64_t times = repeat == nullptr ? 1 : *repeat->to_int64();
          for (std::int64_t i = 0; i < times; ++i) {
            for (const sde::Json& row : step.find("rows")->as_array()) {
              rows.push_back(sde::testing::row_from_json(row));
            }
          }
          const sde::Json* entity = step.find("entity");
          session.save_many(entity == nullptr ? "Reading" : entity->as_string(), rows);
        }
      } catch (const sde::SdeError& error) {
        const sde::Json* named = step.find("error");
        EXPECT_TRUE(named != nullptr) << "an unexpected refusal: " << error.what();
        if (named != nullptr) {
          EXPECT_EQ(exact_class(error), named->as_string()) << error.what();
        }
        errors.push_back(exact_class(error));
        continue;
      }
      EXPECT_EQ(step.find("error"), nullptr) << "the step was not refused";
    }
  };
  run(*want.find("operations"));
  EXPECT_EQ(engines.begin()->second->recorded().as_json(), *want.find("calls"));
  EXPECT_EQ(tables_json(engines), *want.find("tables"));
  sde::Json names = sde::Json::array();
  for (const std::string& name : errors) names.as_array().push_back(name);
  EXPECT_EQ(names, *want.find("errors"));

  const std::optional<sde::Window> window = recorder.roll();
  sde::Json shapes = sde::Json::array();
  sde::Json copies = sde::Json::array();
  if (window) {
    for (const sde::ShapeStats& stats : window->shapes) {
      sde::Json item = sde::Json::object();
      item.set("shape_id", stats.shape_id);
      item.set("entity", stats.entity);
      item.set("group", stats.group);
      item.set("kind", stats.kind);
      item.set("calls", static_cast<std::int64_t>(stats.calls));
      item.set("rows", static_cast<std::int64_t>(stats.rows));
      item.set("errors", static_cast<std::int64_t>(stats.errors));
      shapes.as_array().push_back(std::move(item));
    }
    for (const sde::FanOutStats& stats : window->fanned) {
      sde::Json item = sde::Json::object();
      item.set("group", stats.group);
      item.set("materialization", stats.materialization);
      item.set("writes", static_cast<std::int64_t>(stats.writes));
      item.set("failures", static_cast<std::int64_t>(stats.failures));
      copies.as_array().push_back(std::move(item));
    }
  }
  sde::Json metrics = sde::Json::object();
  metrics.set("shapes", std::move(shapes));
  metrics.set("copies", std::move(copies));
  EXPECT_EQ(metrics, *want.find("metrics"));
  const std::string bytes = sde::canonical_bytes(metrics);
  EXPECT_EQ(sde::Bytes{std::vector<std::uint8_t>(bytes.begin(), bytes.end())}.to_hex(),
            want.find("metrics_hex")->as_string());
}

// --- backfill.json, verify.json, verification.json, frozen.json -------------------------------

sde::BackfillOptions backfill_options(const sde::Json* options) {
  sde::BackfillOptions out;
  if (options == nullptr) return out;
  if (const sde::Json* chunk = options->find("chunk_rows")) out.chunk_rows = *chunk->to_int64();
  if (const sde::Json* stop = options->find("stop_after"); stop != nullptr && !stop->is_null()) {
    out.stop_after = *stop->to_int64();
  }
  return out;
}

void drive_backfill(const std::filesystem::path& directory, const sde::Model& model,
                    const sde::PlacementMap& map, Engines& engines) {
  const sde::Json want = read_json(directory / "backfill.json");
  sde::Session session(model, map, pointers(engines));
  const std::string& group = want.find("group")->as_string();
  const sde::BackfillOptions options = backfill_options(want.find("options"));
  if (const sde::Json* error = want.find("error")) {
    expect_refusal([&] { (void)sde::backfill(session, group, options); }, error->as_string(),
                   want.find("match")->as_string());
    return;
  }
  EXPECT_EQ(sde::backfill(session, group, options).as_record(), *want.find("progress"))
      << "the backfill progress differs from the vector";
}

void drive_verify(const std::filesystem::path& directory, const sde::Model& model,
                  const sde::PlacementMap& map, Engines& engines) {
  const sde::Json want = read_json(directory / "verify.json");
  sde::Session session(model, map, pointers(engines));
  sde::VerifyOptions options;
  if (const sde::Json* given = want.find("options")) {
    if (const sde::Json* chunk = given->find("chunk_rows")) options.chunk_rows = *chunk->to_int64();
  }
  const sde::VerifyReport report = sde::verify(session, want.find("group")->as_string(), options);
  // `at` is a clock reading, so the vector carries a placeholder: a vector holding an instant
  // would expire.
  sde::Json record = report.as_record();
  record.set("at", "<any>");
  EXPECT_EQ(record, *want.find("report"));
  EXPECT_EQ(report.matched(), want.find("matched")->as_bool());
  sde::Json differences = sde::Json::array();
  for (const sde::Difference& difference : report.differences) {
    sde::Json item = sde::Json::object();
    item.set("entity", difference.entity);
    item.set("table", difference.table);
    item.set("key", sde::testing::row_to_json(difference.key));
    sde::Json columns = sde::Json::array();
    for (const std::string& column : difference.columns) columns.as_array().push_back(column);
    item.set("columns", std::move(columns));
    differences.as_array().push_back(std::move(item));
  }
  EXPECT_EQ(differences, *want.find("differences"))
      << "the differences differ. These hold the client's own key values and are deliberately "
         "absent from the record that crosses the boundary.";
}

void drive_verification(const std::filesystem::path& directory, const sde::Model& model,
                        const sde::PlacementMap& map, Engines& engines) {
  const sde::Json want = read_json(directory / "verification.json");
  sde::SessionOptions session_options;
  session_options.project_id = project_of(want);
  sde::Session session(model, map, pointers(engines), session_options);
  const auto compare = [&] {
    sde::VerifyOptions options;
    options.request = sde::VerificationRequest::from_record(*want.find("request"));
    options.at = want.find("at")->as_string();
    const sde::Json* chunk = want.find("chunk_rows");
    options.chunk_rows = chunk == nullptr ? 3 : *chunk->to_int64();
    return sde::verify(session, want.find("group")->as_string(), options);
  };
  if (const sde::Json* error = want.find("error")) {
    expect_refusal([&] { (void)compare(); }, error->as_string(), want.find("match")->as_string());
    return;
  }
  const sde::VerifyReport report = compare();
  EXPECT_EQ(report.as_record(), *want.find("report"));
  EXPECT_EQ(report.matched(), want.find("matched")->as_bool());
}

void drive_frozen(const std::filesystem::path& directory, const sde::Model& model,
                  const sde::PlacementMap& map, Engines& engines) {
  const sde::Json want = read_json(directory / "frozen.json");
  bind_generation_metadata(engines, *want.find("engine_generations"), true);
  const std::string project = want.find("project_id")->as_string();
  const sde::InspectionContext context(model, map, pointers(engines), project);
  const sde::VerificationRequest request =
      sde::VerificationRequest::from_record(*want.find("request"));
  std::map<std::string, std::int64_t> epochs;
  for (const auto& [id, epoch] : want.find("epochs")->as_object()) {
    epochs[id] = sde::epoch_from_json(epoch);
  }
  const std::string hold = want.find("hold_id")->as_string();
  sde::FrozenOptions options;
  options.chunk_rows = 3;
  options.at = want.find("at")->as_string();
  const auto invoke = [&] {
    return sde::verify_frozen(context, want.find("group")->as_string(), request, hold, epochs,
                              options);
  };
  if (const sde::Json* error = want.find("error")) {
    expect_refusal([&] { (void)invoke(); }, error->as_string(), want.find("match")->as_string());
  } else {
    const sde::Json* retry = want.find("retry");
    const int runs = retry != nullptr && retry->as_bool() ? 2 : 1;
    for (int run = 0; run < runs; ++run) {
      const sde::FrozenVerifyReport report = invoke();
      sde::Json record = report.as_record();
      EXPECT_GE(report.elapsed_ms, 0);
      record.set("elapsed_ms", "<measured>");
      EXPECT_EQ(record, *want.find("report"));
      for (const sde::FrozenTable& barrier : report.barriers) {
        const sde::FenceState state =
            engines.at(barrier.engine)->write_fence(barrier.table, project).state();
        EXPECT_NE(std::find(state.holds.begin(), state.holds.end(), hold), state.holds.end())
            << "the barrier is not held on " << barrier.engine << "." << barrier.table;
      }
    }
  }
  EXPECT_EQ(engines.begin()->second->recorded().as_json(), read_json(directory / "calls.json"));
}

// --- watermark.json -----------------------------------------------------------------------------

void drive_watermark(const std::filesystem::path& directory, const sde::PlacementMap& map,
                     Engines& engines) {
  const sde::Json want = read_json(directory / "watermark.json");
  if (const sde::Json* error = want.find("error")) {
    expect_refusal([&] { (void)sde::enforce_forward_only(map, pointers(engines)); },
                   error->as_string(), want.find("match")->as_string());
    return;
  }
  const sde::WatermarkCheck got = sde::enforce_forward_only(map, pointers(engines));
  sde::Json record = got.as_record();
  record.erase("why");
  EXPECT_EQ(record, *want.find("expect"))
      << "the forward-only check disagrees with the vector. This decides whether a client can be "
         "silently reverted to a previous placement.";
  if (const sde::Json* fragments = want.find("why_match")) {
    for (const sde::Json& fragment : fragments->as_array()) {
      EXPECT_NE(got.why.find(fragment.as_string()), std::string::npos)
          << "the explanation does not contain " << fragment.as_string() << ": " << got.why;
    }
  }
}

// --- operations.json ----------------------------------------------------------------------------

void drive_operations(const std::filesystem::path& directory, const sde::Model& model,
                      const sde::PlacementMap& map, Engines& engines) {
  sde::Recorder recorder(model);
  sde::SessionOptions options;
  options.recorder = &recorder;
  sde::Session session(model, map, pointers(engines), options);
  std::function<void(const sde::Json&)> run = [&](const sde::Json& steps) {
    for (const sde::Json& step : steps.as_array()) {
      const std::string& op = step.find("op")->as_string();
      if (op == "save") {
        session.save(step.find("entity")->as_string(),
                     sde::testing::row_from_json(*step.find("values")));
      } else if (op == "transaction") {
        std::vector<std::string> entities;
        if (const sde::Json* named = step.find("entities")) {
          for (const sde::Json& entity : named->as_array()) entities.push_back(entity.as_string());
        }
        try {
          session.transaction(entities, [&] {
            if (const sde::Json* body = step.find("body")) run(*body);
            if (const sde::Json* rollback = step.find("rollback");
                rollback != nullptr && rollback->as_bool()) {
              throw Rollback{};
            }
          });
        } catch (const Rollback&) {
        }
      } else {
        ADD_FAILURE() << "unknown operation " << op;
      }
    }
  };
  run(read_json(directory / "operations.json"));

  // Rows compared as multisets: a fan-out written at the wrong moment loses exactly the rows a
  // migration exists not to lose, and nothing raises.
  sde::Json got = sde::Json::object();
  for (const auto& [name, engine] : engines) {
    sde::Json tables = sde::Json::object();
    for (const auto& [table, rows] : engine->tables) tables.set(table, sorted_rows(table_json(rows)));
    got.set(name, std::move(tables));
  }
  sde::Json expected = read_json(directory / "tables.json");
  for (auto& [name, tables] : expected.as_object()) {
    for (auto& [table, rows] : tables.as_object()) rows = sorted_rows(rows);
  }
  EXPECT_EQ(got, expected) << sde::dump_json(got);

  const std::optional<sde::Window> window = recorder.roll();
  const std::string& group = map.groups().begin()->first;
  const std::vector<sde::CopyFreshness> copies =
      window ? window->copies(group) : std::vector<sde::CopyFreshness>{};
  const sde::Json wanted = read_json(directory / "copies.json");
  ASSERT_EQ(copies.size(), wanted.as_array().size());
  for (std::size_t i = 0; i < copies.size(); ++i) {
    sde::Json record = copies[i].as_record();
    for (const char* measured : {"lag_p50_ms", "lag_p99_ms"}) {
      // Elapsed time, so the vector carries a placeholder; what belongs to the library is that the
      // field is there and is a number or null.
      EXPECT_EQ(*wanted.as_array()[i].find(measured), sde::Json("<any>"));
      const sde::Json* value = record.find(measured);
      ASSERT_NE(value, nullptr) << measured;
      EXPECT_TRUE(value->is_null() || value->is_number()) << measured;
      record.set(measured, "<any>");
    }
    EXPECT_EQ(record, wanted.as_array()[i]);
  }
}

// --- the family ---------------------------------------------------------------------------------

std::optional<std::string> driver_of(const std::filesystem::path& directory) {
  for (const auto* drivers : {&written_drivers(), &pending_drivers()}) {
    for (const std::string& driver : *drivers) {
      if (has_file(directory / (driver + ".json"))) return driver;
    }
  }
  return std::nullopt;
}

class MigrationVector : public ::testing::TestWithParam<std::string> {};

TEST_P(MigrationVector, TakesPartAsTheVectorSays) {
  const auto directory = vectors_root() / "migration" / GetParam();
  const std::optional<std::string> driver = driver_of(directory);
  ASSERT_TRUE(driver.has_value()) << "the case names no driver this runner knows";
  if (std::find(pending_drivers().begin(), pending_drivers().end(), *driver) !=
      pending_drivers().end()) {
    if (sde::TIER < 2) {
      GTEST_SKIP() << "the " << *driver << " driver is not written yet; this library claims Tier "
                   << sde::TIER << ", so the case is above it";
    }
    FAIL() << "this library claims Tier 2 and has no " << *driver << " driver";
  }
  if (*driver == "fencing") {
    drive_fencing(directory);
    return;
  }
  const sde::Model model = sde::load_neutral_model(read_json(directory / "model.json"));
  const sde::PlacementMap map = load_case_map(directory, model);
  Engines engines = sde::testing::engines_from(read_json(directory / "engines.json"));
  if (*driver == "generation") {
    drive_generation(directory, model, map, engines);
    return;
  }
  if (*driver == "frozen") {
    drive_frozen(directory, model, map, engines);
    return;
  }
  if (*driver == "bulk") {
    drive_bulk(directory, model, map, engines);
    return;
  }
  if (*driver == "watermark") drive_watermark(directory, map, engines);
  if (*driver == "backfill") drive_backfill(directory, model, map, engines);
  if (*driver == "verify") drive_verify(directory, model, map, engines);
  if (*driver == "verification") drive_verification(directory, model, map, engines);
  if (*driver == "operations") drive_operations(directory, model, map, engines);
  if (has_file(directory / "calls.json")) {
    // One sequence for the whole engine set: the guarantee that a row reaches the source before
    // anything is tried against the copy cannot be expressed in per-engine lists.
    EXPECT_EQ(engines.begin()->second->recorded().as_json(), read_json(directory / "calls.json"))
        << "the calls this library made to the engines differ from the vector. The counts can be "
           "right and the calls wrong - that is a library that works on a fixture and not on a "
           "table.";
  }
}

INSTANTIATE_TEST_SUITE_P(Migration, MigrationVector, ::testing::ValuesIn(cases("migration")),
                         [](const auto& param_info) { return test_name(param_info.param); });

TEST(MigrationFamily, PinsAnEngineTheNoAccountModeMustNotTouch) {
  // The no-account mode promises no table, no query and no cost: a claim about calls not made. A
  // vector holding an empty list is easy to satisfy by accident - a runner that never built the
  // engines would - so the same documents are read from the other side.
  std::size_t empty = 0;
  std::size_t busy = 0;
  for (const std::string& name : cases("migration")) {
    const auto calls = vectors_root() / "migration" / name / "calls.json";
    if (!has_file(calls)) continue;
    (read_json(calls).as_array().empty() ? empty : busy) += 1;
  }
  EXPECT_GT(empty, 0U) << "no migration vector pins an engine this library must not touch";
  EXPECT_GT(busy, 0U) << "every migration vector expects zero calls, so the runner may not run";
}

}  // namespace
