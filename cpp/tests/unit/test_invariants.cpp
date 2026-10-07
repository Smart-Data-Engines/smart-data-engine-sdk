/// Requirement 6, the product's invariants, each held by a test rather than by a sentence:
///
/// - 6.1: the core opens no socket and links no network library. Its archive is read by
///   `invariant.the_core_links_no_network` (tests/CMakeLists.txt), which needs the built file;
/// - 6.2: there is no expiry. The map's parser reads no date, and the library reads the wall clock
///   once, to stamp a verification report;
/// - 6.3: no value of the client's reaches a log event, a telemetry window or a refusal the library
///   makes. What an engine says back - a server's error, an answer that does not parse - is passed
///   on by the adapters in the engine's words, for the application to read in its own process,
///   and may quote what the engine holds: that is the engine's, said to its own client, and
///   reaches neither a log of this library's nor the control plane;
/// - 6.4: an unsigned map is the no-account mode, and costs no table and no query.
///
/// A test that greps for the word "expiry" would pass on the first field called `valid_through`,
/// so 6.2 reads what the parser reads - the keys it looks up - as the reference reads its own
/// parser's syntax tree; and 6.3 searches everything the library said, serialised, for values put
/// in on purpose, so that a field added later is in scope without anybody adding it here.

#include <algorithm>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <functional>
#include <map>
#include <memory>
#include <regex>
#include <set>
#include <sstream>
#include <string>
#include <utility>
#include <vector>

#include <gtest/gtest.h>

#include "sde/errors.hpp"
#include "sde/json.hpp"
#include "sde/log.hpp"
#include "sde/model.hpp"
#include "sde/placement.hpp"
#include "sde/query.hpp"
#include "sde/session.hpp"
#include "sde/telemetry.hpp"
#include "sde/testing/memory.hpp"
#include "support/signer.hpp"

namespace {

namespace fs = std::filesystem;

std::string read(const fs::path& path) {
  std::ifstream in(path, std::ios::binary);
  std::ostringstream text;
  text << in.rdbuf();
  return text.str();
}

std::vector<fs::path> sources() {
  std::vector<fs::path> out;
  for (const auto& entry : fs::recursive_directory_iterator(SDE_SOURCE_DIR)) {
    const std::string extension = entry.path().extension().string();
    if (entry.is_regular_file() && (extension == ".cpp" || extension == ".hpp")) {
      out.push_back(entry.path());
    }
  }
  return out;
}

// --- 6.2: no expiry -------------------------------------------------------------------------------

/// The keys a map document is read by: what `find` and `contains` are asked for in the files that
/// parse one.
std::set<std::string> keys_the_map_parser_reads() {
  const std::regex lookup(R"re(\b(?:find|contains)\("([^"]+)"\))re");
  std::set<std::string> keys;
  for (const char* file : {"placement.cpp", "physical.cpp"}) {
    const std::string text = read(fs::path(SDE_SOURCE_DIR) / file);
    for (std::sregex_iterator it(text.begin(), text.end(), lookup), end; it != end; ++it) {
      keys.insert((*it)[1].str());
    }
  }
  return keys;
}

TEST(Invariants, AMapCarriesNoDateBecauseItsParserReadsNone) {
  const std::set<std::string> keys = keys_the_map_parser_reads();
  // The extraction works: it finds what every map has.
  for (const char* known : {"contract", "model_version", "map_version", "groups", "signature",
                            "source", "layout", "routing"}) {
    EXPECT_TRUE(keys.contains(known)) << known << " is read by the parser and was not found, so "
                                      << "this test reads the wrong thing";
  }
  const std::regex dated(R"(date|time|expir|valid|issued|until|after|before|ttl|deadline|clock|age)",
                         std::regex::icase);
  for (const std::string& key : keys) {
    EXPECT_FALSE(std::regex_search(key, dated))
        << "the map parser reads " << key << ". A map carries no date, so nothing in this library "
        << "can compare one with the clock: there is no expiry to find, wherever it might be put";
  }
}

TEST(Invariants, TheWallClockIsReadOnceToStampAReport) {
  // Every way C++ and POSIX read the calendar. A monotonic clock cannot be compared with a date,
  // so it cannot gate on one; these can.
  const std::regex wall(
      R"(system_clock::now|utc_clock|tai_clock|gps_clock|file_clock|gettimeofday|CLOCK_REALTIME|localtime|gmtime|mktime|\btime\s*\(\s*(nullptr|NULL|0)\s*\))");
  std::vector<std::string> reads;
  for (const fs::path& path : sources()) {
    const std::string text = read(path);
    for (std::sregex_iterator it(text.begin(), text.end(), wall), end; it != end; ++it) {
      reads.push_back(path.filename().string() + ": " + (*it)[0].str());
    }
  }
  EXPECT_EQ(reads, (std::vector<std::string>{"migration.cpp: system_clock::now"}))
      << "the one read stamps a verification report with when it ran (`now_iso`); a second has to "
      << "say what it decides, because a clock read that decides something is where an expiry "
      << "check would live";
}

// --- 6.3: no value ------------------------------------------------------------------------------

constexpr const char* EMAIL = "MARKER-8f21c3-email";
constexpr const char* NAME = "MARKER-8f21c3-name";
constexpr const char* ID = "d0d0beef-0000-4000-8000-00000000cafe";
constexpr std::int64_t AGE = 735'119'004;  // a number that appears nowhere else

struct Said {
  std::vector<std::string> lines;
  int events = 0;
  int refusals = 0;
  [[nodiscard]] sde::LogSink sink() {
    return [this](std::string_view event, const sde::Json& fields) {
      ++events;
      lines.push_back(std::string(event) + " " + sde::dump_json(fields));
    };
  }
  /// What a refusal said; a body that was not refused counts nothing, and the test says so.
  void refusal(const std::function<void()>& body) {
    try {
      body();
    } catch (const std::exception& error) {
      ++refusals;
      lines.emplace_back(error.what());
    }
  }
  [[nodiscard]] std::vector<std::string> leaks() const {
    const std::vector<std::string> markers = {EMAIL, NAME, ID, "d0d0beef00004000800000000000cafe",
                                              std::to_string(AGE)};
    std::vector<std::string> out;
    for (const std::string& line : lines) {
      for (const std::string& marker : markers) {
        if (line.find(marker) != std::string::npos) out.push_back(marker + " in: " + line);
      }
    }
    return out;
  }
};

/// An engine that keeps what it is given and answers every read with it: what the session sends it
/// and records about it is the subject, not what it finds.
class Readable final : public sde::Engine,
                       public sde::Queryable,
                       public sde::Countable,
                       public sde::Summarizable {
 public:
  [[nodiscard]] std::string_view dialect() const noexcept override { return "postgres"; }
  std::vector<sde::PhysicalFinding> ensure_schema(const sde::PhysicalLayout&,
                                                  const sde::Keys&) override {
    return {};
  }
  void insert(const std::string&, const sde::Row& values) override { rows_.push_back(values); }
  std::optional<sde::Row> get(const std::string&, const sde::Row&) override {
    if (rows_.empty()) return std::nullopt;
    return rows_.front();
  }
  void transaction(const std::function<void()>& body) override { body(); }
  [[nodiscard]] sde::Capabilities capabilities() noexcept override {
    sde::Capabilities offered;
    offered.query = this;
    offered.count = this;
    offered.summary = this;
    return offered;
  }
  std::vector<sde::Row> select_rows(const std::string&, const sde::ReadPlan&) override {
    return {rows_.begin(), rows_.begin() + std::min<std::ptrdiff_t>(1, std::ssize(rows_))};
  }
  std::uint64_t count_rows(const std::string&, const sde::ReadPlan&) override {
    return rows_.size();
  }
  sde::SummaryRecord summarize_rows(const std::string&, const sde::ReadPlan&,
                                    const sde::ReadColumn&) override {
    return {"1", "1", "1", "1", "1"};
  }

 private:
  std::vector<sde::Row> rows_;
};

sde::Model users() {
  return sde::load_neutral_model(R"({"entities": [{"name": "User", "fields": [
      {"name": "id", "type": "uuid"}, {"name": "email", "type": "string"},
      {"name": "name", "type": "string"}, {"name": "age", "type": "int64"}],
      "key": ["id"], "pii": ["email", "name"]}]})");
}

sde::PlacementMap unsigned_map(const sde::Model& model) {
  sde::LoadOptions options;
  options.model = &model;
  return sde::load_map(R"({"contract": 3, "model_version": ")" + model.version() +
                           R"(", "map_version": 1, "groups": {"User": {"source": {"id": "User@e",
      "engine": "e", "layout": {"auto": true}}}}})",
                       options);
}

TEST(Invariants, NoValueReachesALogEventATelemetryWindowOrARefusal) {
  const sde::Model model = users();
  const sde::PlacementMap map = unsigned_map(model);
  Readable engine;
  Said said;
  sde::Recorder recorder(model);
  sde::SessionOptions options;
  options.recorder = &recorder;
  options.log = said.sink();
  sde::Session session(model, map, {{"e", &engine}}, options);
  session.ensure_schema();

  const sde::Uuid id(ID);
  const sde::Row user{{"id", id}, {"email", std::string(EMAIL)}, {"name", std::string(NAME)},
                      {"age", AGE}};
  for (int round = 0; round < 3; ++round) {
    session.save("User", user);
    (void)session.get("User", {{"id", id}});
    sde::ScanOptions scan;
    scan.where = sde::Row{{"email", std::string(EMAIL)}};
    (void)session.scan("User", scan);
    sde::CountOptions count;
    count.where = sde::Row{{"name", std::string(NAME)}};
    (void)session.count("User", count);
    sde::SummaryOptions summary;
    summary.where = sde::Row{{"email", std::string(EMAIL)}};
    (void)session.summarize("User", "age", summary);
    session.transaction({"User"}, [&] { session.save("User", user); });
    // A key of the wrong type is the engine's to refuse, and here it reaches the engine.
    (void)session.get("User", {{"id", std::string(EMAIL)}});
  }
  // What a refusal says when a value is wrong: the field, never the value.
  said.refusal([&] { (void)session.get("User", {{"id", id}, {"email", std::string(EMAIL)}}); });
  said.refusal([&] {
    sde::Row extra = user;
    extra["nickname"] = std::string(NAME);
    session.save("User", extra);
  });
  said.refusal([&] {
    sde::Row missing = user;
    missing["name"] = sde::Null{};
    session.save("User", missing);
  });
  said.refusal([&] {
    sde::ScanOptions scan;
    scan.where = sde::Row{{"email", AGE}};
    (void)session.scan("User", scan);
  });
  said.refusal([&] {
    sde::ScanOptions scan;
    scan.where = sde::Row{{"nickname", std::string(EMAIL)}};
    (void)session.scan("User", scan);
  });
  said.refusal([&] {
    sde::ScanOptions scan;
    scan.after = sde::Row{{"id", std::string(EMAIL)}};
    (void)session.scan("User", scan);
  });
  said.refusal([&] { session.save_many("User", {user, sde::Row{{"id", id}, {"email", std::string(EMAIL)}}}); });

  EXPECT_EQ(said.refusals, 7) << "each of those was meant to be refused, and a refusal's words "
                                 "are what is searched";
  EXPECT_GE(said.events, 1) << "no event was logged, so the log was not searched";
  const std::optional<sde::Window> window = recorder.roll();
  ASSERT_TRUE(window.has_value()) << "nothing was recorded, so this test proves nothing";
  said.lines.push_back(sde::dump_json(window->as_record(model)));
  EXPECT_EQ(said.leaks(), std::vector<std::string>{})
      << "a value of the client's reached a log, a window or a refusal: the privacy guarantee is "
      << "the reason this library is open source, and this is the one failure that cannot ship";
}

TEST(Invariants, TheValueSearchWouldNoticeALeak) {
  // The search above, over something that does contain a marker: if it passed on anything, it
  // would pass on this too.
  Said said;
  said.lines.emplace_back(std::string("refused because the email was ") + EMAIL);
  said.lines.emplace_back("age " + std::to_string(AGE));
  EXPECT_EQ(said.leaks().size(), 2U);
}

// --- 6.4: no account ----------------------------------------------------------------------------

std::vector<std::string> bookkeeping(const sde::testing::Recorded& journal) {
  std::vector<std::string> out;
  for (const sde::Json& call : journal.calls()) {
    const std::string name = call.find("call")->as_string();
    if (name.find("watermark") != std::string::npos || name.find("map_version") != std::string::npos) {
      out.push_back(name);
    }
  }
  return out;
}

TEST(Invariants, AnUnsignedMapCostsNoTableAndNoQuery) {
  const sde::Model model = users();
  const auto journal = std::make_shared<sde::testing::Recorded>();
  sde::testing::MemoryEngineOptions engine_options;
  engine_options.dialect = "postgres";
  engine_options.name = "e";
  engine_options.journal = journal;
  sde::testing::MemoryEngine engine(engine_options);
  {
    const sde::PlacementMap map = unsigned_map(model);
    sde::Session session(model, map, {{"e", &engine}});
    session.ensure_schema();
    session.save("User", {{"id", sde::Uuid(ID)}, {"email", std::string("a")},
                          {"name", std::string("b")}, {"age", std::int64_t{1}}});
    (void)session.get("User", {{"id", sde::Uuid(ID)}});
  }
  EXPECT_EQ(bookkeeping(*journal), std::vector<std::string>{})
      << "a map without a signature is the no-account mode: it needs no bookkeeping table and "
      << "asks the engine nothing about one";

  // And the journal would have seen it: a signed map's session asks for the watermark.
  const sde::testing_support::Signer signer("invariants");
  sde::Json document = sde::parse_json(R"({"contract": 3, "model_version": ")" + model.version() +
                                       R"(", "map_version": 1, "groups": {"User": {"source":
      {"id": "User@e", "engine": "e", "layout": {"auto": true}}}}})");
  sde::LoadOptions options;
  options.model = &model;
  options.public_keys = signer.keys();
  const sde::PlacementMap signed_map = sde::load_map(signer.signed_document(document), options);
  { const sde::Session session(model, signed_map, {{"e", &engine}}); }
  EXPECT_NE(bookkeeping(*journal), std::vector<std::string>{});
}

}  // namespace
