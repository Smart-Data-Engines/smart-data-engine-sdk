/// `telemetry/` (Tier 1): the window document and every derivation behind it.
///
/// Compared as numbers rather than bytes - the one exception in the suite. Almost every number in a
/// window is a float, and a float's text differs between languages; but each is a ratio of two
/// integers or a bucket edge over a million, and IEEE 754 rounds division correctly, so two
/// languages compute the same double from the same traffic.

#include <cstdint>
#include <optional>
#include <stdexcept>

#include <gtest/gtest.h>

#include "sde/model.hpp"
#include "sde/telemetry.hpp"
#include "support/vectors.hpp"

namespace {

using namespace sde::testing_support;

/// Feeds a case's operations to a recorder on the case's own clock and closes the window. Shapes
/// are named by identifier and looked up in this library's own enumeration, so a case cannot pin a
/// classification by asserting it in its input.
std::optional<sde::Window> recorded(const sde::Model& model, const sde::Json& operations,
                                    const std::filesystem::path& directory) {
  std::int64_t now = 0;
  sde::Recorder recorder(model, 64, [&now] { return now; });
  for (const sde::Json& operation : operations.as_array()) {
    if (const sde::Json* at = operation.find("at_ms")) now = at->to_int64().value() * 1'000'000;
    const sde::Json* event = operation.find("event");
    if (event != nullptr && event->as_string() == "storage") {
      recorder.record_storage(operation.find("group")->as_string(),
                              operation.find("total_bytes")->to_int64().value(),
                              operation.find("secondary_index_bytes")->to_int64().value());
      continue;
    }
    if (event != nullptr && event->as_string() == "roll") {
      EXPECT_TRUE(recorder.roll().has_value()) << "an earlier window recorded nothing";
      continue;
    }
    const std::string& id = operation.find("shape")->as_string();
    const sde::OperationShape* shape = model.find_shape(id);
    EXPECT_NE(shape, nullptr) << "the case names shape " << id << ", which this library does not "
                              << "enumerate";
    if (shape == nullptr) return std::nullopt;
    sde::Filter filter;
    const sde::Filter* filters = nullptr;
    if (const sde::Json* equal = operation.find("equal"); equal != nullptr && !equal->is_null()) {
      for (const sde::Json& name : equal->as_array()) filter.equal.push_back(name.as_string());
      if (const sde::Json* range = operation.find("range"); range != nullptr && !range->is_null()) {
        filter.range = range->as_string();
      }
      filters = &filter;
    }
    const sde::Json* rows = operation.find("rows");
    const sde::Json* failed = operation.find("failed");
    recorder.record(*shape, operation.find("ns")->to_int64().value(),
                    rows != nullptr ? static_cast<std::uint64_t>(rows->to_int64().value()) : 0,
                    failed != nullptr && failed->as_bool(), filters);
  }
  if (has_file(directory / "fan_out.json")) {
    const sde::Json fan_out = read_json(directory / "fan_out.json");
    for (const sde::Json& entry : fan_out.as_array()) {
      const sde::Json* failed = entry.find("failed");
      recorder.record_fan_out(entry.find("group")->as_string(),
                              entry.find("materialization")->as_string(),
                              entry.find("ns")->to_int64().value(),
                              failed != nullptr && failed->as_bool());
    }
  }
  if (has_file(directory / "clock.json")) {
    now = read_json(directory / "clock.json").find("window_ms")->to_int64().value() * 1'000'000;
  }
  auto window = recorder.roll();
  EXPECT_TRUE(window.has_value()) << "the case recorded nothing, so it pins nothing";
  return window;
}

class TelemetryVector : public ::testing::TestWithParam<std::string> {};

TEST_P(TelemetryVector, MeasuresAsTheReferenceDoes) {
  const auto directory = vectors_root() / "telemetry" / GetParam();

  if (has_file(directory / "buckets.json")) {
    const sde::Json edges = *read_json(directory / "percentiles.json").find("edges_ms");
    ASSERT_EQ(edges.as_array().size(), static_cast<std::size_t>(sde::BUCKET_COUNT));
    const sde::Json boundaries = read_json(directory / "buckets.json");
    for (const sde::Json& pair : boundaries.as_array()) {
      const std::int64_t nanoseconds = pair.as_array()[0].to_int64().value();
      const auto index = static_cast<std::size_t>(pair.as_array()[1].to_int64().value());
      sde::Histogram histogram;
      histogram.record(nanoseconds);
      std::vector<std::size_t> landed;
      for (std::size_t i = 0; i < histogram.buckets.size(); ++i) {
        if (histogram.buckets[i] != 0) landed.push_back(i);
      }
      EXPECT_EQ(landed, std::vector<std::size_t>{index}) << nanoseconds << " ns";
      EXPECT_EQ(histogram.percentile_ms(0.5), edges.as_array()[index].to_double())
          << "a single sample reports the upper edge of its bucket";
    }
  }
  if (!has_file(directory / "operations.json")) return;

  const sde::Model model = sde::load_neutral_model(read_json(directory / "model.json"));
  const sde::Json operations = read_json(directory / "operations.json");

  if (has_file(directory / "expected.json")) {
    // The class is not pinned: a wrong model is a caller's mistake, and each language raises what
    // it raises for a bad argument. The message is what a reader needs.
    const auto window = recorded(model, operations, directory);
    ASSERT_TRUE(window.has_value());
    const sde::Model against = sde::load_neutral_model(read_json(directory / "against.json"));
    const std::string match = read_json(directory / "expected.json").find("match")->as_string();
    try {
      (void)window->as_record(against);
      ADD_FAILURE() << "a window was serialised against a model it did not measure";
    } catch (const std::exception& error) {
      EXPECT_NE(std::string(error.what()).find(match), std::string::npos) << error.what();
    }
    return;
  }

  const auto window = recorded(model, operations, directory);
  ASSERT_TRUE(window.has_value());
  if (has_file(directory / "window.json")) {
    const sde::Json got = window->as_record(model);
    const sde::Json want = read_json(directory / "window.json");
    EXPECT_TRUE(got == want) << "got  " << sde::dump_json(got) << "\nwant " << sde::dump_json(want);
  }
  if (has_file(directory / "features_for.json")) {
    const sde::Json expected = read_json(directory / "features_for.json");
    for (const auto& [name, want] : expected.as_object()) {
      const sde::Group* group = nullptr;
      for (const sde::Group& candidate : model.groups()) {
        if (candidate.name == name) group = &candidate;
      }
      ASSERT_NE(group, nullptr) << name;
      const sde::Json got =
          window->features(name, sde::has_time_dimension(model, *group)).as_record();
      EXPECT_TRUE(got == want) << name << "\ngot  " << sde::dump_json(got) << "\nwant "
                               << sde::dump_json(want);
    }
  }
}

INSTANTIATE_TEST_SUITE_P(Telemetry, TelemetryVector, ::testing::ValuesIn(cases("telemetry")),
                         [](const auto& param_info) { return test_name(param_info.param); });

}  // namespace
