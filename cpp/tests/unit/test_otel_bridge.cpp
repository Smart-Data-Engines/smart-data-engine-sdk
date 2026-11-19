/// The OpenTelemetry bridge of docs/observability.md, compiled and run: a hook that makes spans and
/// a histogram - the counterpart of python/tests/test_otel_bridge.py.
///
/// The library depends on no tracing library, so the bridge is the application's code. The block
/// between the markers is the one the document shows, and the last test holds the two equal. The
/// namespaces below are stand-ins with opentelemetry-cpp's names and shapes for exactly what the
/// block uses, so the block compiles here unchanged and nothing is installed for it.

#include <chrono>
#include <cstdint>
#include <fstream>
#include <initializer_list>
#include <memory>
#include <optional>
#include <sstream>
#include <string>
#include <string_view>
#include <utility>
#include <variant>
#include <vector>

#include <gtest/gtest.h>

#include "sde/hook.hpp"

namespace opentelemetry {
namespace nostd {
using std::shared_ptr;
using std::string_view;
}  // namespace nostd
namespace common {
using AttributeValue = std::variant<bool, std::int64_t, double, nostd::string_view>;
struct SystemTimestamp {
  SystemTimestamp() = default;
  explicit SystemTimestamp(std::chrono::system_clock::time_point when) : at(when) {}
  std::chrono::system_clock::time_point at;
};
struct SteadyTimestamp {
  SteadyTimestamp() = default;
  explicit SteadyTimestamp(std::chrono::steady_clock::time_point when) : at(when) {}
  std::chrono::steady_clock::time_point at;
};
using Attributes = std::initializer_list<std::pair<nostd::string_view, AttributeValue>>;
}  // namespace common
namespace context {
struct Context {};
}  // namespace context
namespace trace {
enum class StatusCode { kUnset, kOk, kError };
struct StartSpanOptions {
  common::SystemTimestamp start_system_time;
  common::SteadyTimestamp start_steady_time;
};
struct EndSpanOptions {
  common::SteadyTimestamp end_steady_time;
};
struct Span {
  std::string name;
  std::vector<std::pair<std::string, std::string>> attributes;
  StartSpanOptions start;
  std::optional<std::pair<StatusCode, std::string>> status;
  std::optional<EndSpanOptions> ended;
  void SetStatus(StatusCode code, nostd::string_view description) {
    status = {code, std::string(description)};
  }
  void End(const EndSpanOptions& options = {}) { ended = options; }
};
struct Tracer {
  std::vector<std::shared_ptr<Span>> spans;
  nostd::shared_ptr<Span> StartSpan(nostd::string_view name, common::Attributes attributes,
                                    const StartSpanOptions& options = {}) {
    auto span = std::make_shared<Span>();
    span->name = std::string(name);
    for (const auto& [key, value] : attributes) {
      std::string text = std::holds_alternative<nostd::string_view>(value)
                             ? std::string(std::get<nostd::string_view>(value))
                             : std::to_string(std::get<std::int64_t>(value));
      span->attributes.emplace_back(std::string(key), std::move(text));
    }
    span->start = options;
    spans.push_back(span);
    return span;
  }
};
}  // namespace trace
namespace metrics {
template <class T>
struct Histogram {
  std::vector<std::pair<T, std::vector<std::pair<std::string, std::string>>>> values;
  void Record(T value, common::Attributes attributes, const context::Context&) {
    std::vector<std::pair<std::string, std::string>> kept;
    for (const auto& [key, item] : attributes) {
      kept.emplace_back(std::string(key), std::string(std::get<nostd::string_view>(item)));
    }
    values.emplace_back(value, std::move(kept));
  }
};
}  // namespace metrics
}  // namespace opentelemetry

namespace {

// bridge:begin
/// A span and a histogram value for each operation the session completes.
sde::OperationHook opentelemetry_hook(
    opentelemetry::nostd::shared_ptr<opentelemetry::trace::Tracer> tracer,
    opentelemetry::metrics::Histogram<double>* histogram) {
  return [tracer, histogram](const sde::OperationRecord& record) {
    const auto took = std::chrono::nanoseconds(record.duration_ns);
    const auto end = std::chrono::steady_clock::now();
    opentelemetry::trace::StartSpanOptions options;
    options.start_system_time =
        opentelemetry::common::SystemTimestamp(std::chrono::system_clock::now() - took);
    options.start_steady_time = opentelemetry::common::SteadyTimestamp(end - took);
    auto span = tracer->StartSpan("sde." + record.operation,
                                  {{"db.system", record.dialect},
                                   {"sde.entity", record.entity},
                                   {"sde.group", record.group},
                                   {"sde.shape", record.shape},
                                   {"sde.engine", record.engine},
                                   {"sde.materialization", record.materialization},
                                   {"sde.rows", static_cast<std::int64_t>(record.rows)}},
                                  options);
    if (record.outcome != "ok") {
      span->SetStatus(opentelemetry::trace::StatusCode::kError, record.outcome);
    }
    opentelemetry::trace::EndSpanOptions ending;
    ending.end_steady_time = opentelemetry::common::SteadyTimestamp(end);
    span->End(ending);
    histogram->Record(static_cast<double>(record.duration_ns) / 1e9,
                      {{"sde.operation", record.operation},
                       {"db.system", record.dialect},
                       {"sde.outcome", record.outcome}},
                      opentelemetry::context::Context{});
  };
}
// bridge:end

sde::OperationRecord record(std::string outcome = "ok") {
  sde::OperationRecord made;
  made.operation = "get";
  made.entity = "Note";
  made.group = "Note";
  made.shape = "a1b2c3";
  made.engine = "pg-main";
  made.dialect = "postgres";
  made.materialization = "notes@pg";
  made.duration_ns = 1'500'000;
  made.rows = 1;
  made.outcome = std::move(outcome);
  return made;
}

TEST(OtelBridge, ARecordIsASpanThatEndsNowAndBeganItsDurationBefore) {
  auto tracer = std::make_shared<opentelemetry::trace::Tracer>();
  opentelemetry::metrics::Histogram<double> histogram;
  opentelemetry_hook(tracer, &histogram)(record());
  ASSERT_EQ(tracer->spans.size(), 1U);
  const opentelemetry::trace::Span& span = *tracer->spans[0];
  EXPECT_EQ(span.name, "sde.get");
  ASSERT_TRUE(span.ended.has_value());
  EXPECT_EQ(span.ended->end_steady_time.at - span.start.start_steady_time.at,
            std::chrono::nanoseconds(1'500'000));
  EXPECT_EQ(span.attributes[0], (std::pair<std::string, std::string>{"db.system", "postgres"}));
  EXPECT_FALSE(span.status.has_value());
  ASSERT_EQ(histogram.values.size(), 1U);
  EXPECT_DOUBLE_EQ(histogram.values[0].first, 0.0015);
}

TEST(OtelBridge, AnErrorIsTheSpanStatusWithItsClass) {
  auto tracer = std::make_shared<opentelemetry::trace::Tracer>();
  opentelemetry::metrics::Histogram<double> histogram;
  opentelemetry_hook(tracer, &histogram)(record("EngineError"));
  ASSERT_TRUE(tracer->spans[0]->status.has_value());
  EXPECT_EQ(tracer->spans[0]->status->first, opentelemetry::trace::StatusCode::kError);
  EXPECT_EQ(tracer->spans[0]->status->second, "EngineError");
}

/// The lines of `text` that are code: without includes and without blank lines at either end.
std::string code(const std::string& text) {
  std::istringstream lines(text);
  std::string line;
  std::string out;
  while (std::getline(lines, line)) {
    if (line.starts_with("#include")) continue;
    out += line + "\n";
  }
  const auto first = out.find_first_not_of('\n');
  const auto last = out.find_last_not_of('\n');
  return first == std::string::npos ? "" : out.substr(first, last - first + 1);
}

std::string read(const std::string& path) {
  std::ifstream in(path);
  std::ostringstream text;
  text << in.rdbuf();
  return text.str();
}

TEST(OtelBridge, IsTheCodeTheDocumentShows) {
  const std::string source = read(SDE_SOURCE_DIR "/../tests/unit/test_otel_bridge.cpp");
  const std::string document = read(SDE_SOURCE_DIR "/../../docs/observability.md");
  const auto begin = source.find("// bridge:begin\n");
  const auto end = source.find("// bridge:end");
  ASSERT_NE(begin, std::string::npos);
  const std::string ours = source.substr(begin + 16, end - begin - 16);
  const std::string marker = "<!-- bridge:cpp -->\n```cpp\n";
  const auto shown = document.find(marker);
  ASSERT_NE(shown, std::string::npos) << "docs/observability.md shows no C++ bridge";
  const auto closing = document.find("```", shown + marker.size());
  EXPECT_EQ(code(document.substr(shown + marker.size(), closing - shown - marker.size())),
            code(ours));
}

}  // namespace
