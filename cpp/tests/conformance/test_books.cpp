/// `books/`: a side's difference, and what a book stream sends on a clock - whole sides and changes,
/// with their times - or refuses (format contract section 8c). The vectors were written by hand, not
/// computed by a library, which is what lets them check this one.

#include <cstdint>
#include <memory>
#include <optional>
#include <stdexcept>
#include <string>
#include <vector>

#include <gtest/gtest.h>

#include "sde/books.hpp"
#include "sde/errors.hpp"
#include "support/vectors.hpp"

namespace {

using namespace sde::testing_support;

std::vector<sde::BookLevel> levels_of(const sde::Json& array) {
  std::vector<sde::BookLevel> out;
  for (const sde::Json& level : array.as_array()) {
    const sde::Json::Array& v = level.as_array();
    EXPECT_EQ(v.size(), 3U) << "a level is (price, quantity, order count)";
    out.push_back({v.at(0).to_int64().value(), v.at(1).to_int64().value(),
                   v.at(2).to_int64().value()});
  }
  return out;
}

/// What a vector says was sent, or what this library sent, in one form to compare.
struct Sent {
  std::string kind;
  std::string side;
  std::int64_t ts = 0;
  std::vector<sde::BookLevel> levels;

  friend bool operator==(const Sent&, const Sent&) = default;
};

std::ostream& operator<<(std::ostream& out, const Sent& sent) {
  out << sent.kind << " " << sent.side << " at " << sent.ts << ":";
  for (const sde::BookLevel& level : sent.levels) {
    out << " [" << level.price << "," << level.quantity << "," << level.order_count << "]";
  }
  return out;
}

class Recording final : public sde::BookSink {
 public:
  std::vector<Sent> sent;
  bool fail_next = false;

  void send_whole(sde::BookSide side, const std::vector<sde::BookLevel>& levels,
                  std::int64_t timestamp_ns) override {
    record("whole", side, levels, timestamp_ns);
  }
  void send_changes(sde::BookSide side, const std::vector<sde::BookLevel>& levels,
                    std::int64_t timestamp_ns) override {
    record("changes", side, levels, timestamp_ns);
  }

 private:
  void record(const std::string& kind, sde::BookSide side,
              const std::vector<sde::BookLevel>& levels, std::int64_t timestamp_ns) {
    if (fail_next) {
      fail_next = false;
      throw std::runtime_error("sink refused");
    }
    sent.push_back({kind, std::string(sde::to_string(side)), timestamp_ns, levels});
  }
};

std::vector<std::string> of_kind(const std::string& kind) {
  std::vector<std::string> out;
  for (const std::string& name : cases("books")) {
    const sde::Json spec = read_json(vectors_root() / "books" / name / "case.json");
    const std::string& found = spec.find("kind")->as_string();
    EXPECT_TRUE(found == "diff" || found == "stream") << "a kind this runner does not know: " << found;
    if (found == kind) out.push_back(name);
  }
  return out;
}

class BookDiffVector : public ::testing::TestWithParam<std::string> {};

TEST_P(BookDiffVector, IsTheDifferenceOrTheRefusal) {
  const sde::Json spec = read_json(vectors_root() / "books" / GetParam() / "case.json");
  SCOPED_TRACE(spec.find("why") ? spec.find("why")->as_string() : spec.find("name")->as_string());
  const sde::BookSide side = sde::book_side(spec.find("side")->as_string());
  const auto before = levels_of(*spec.find("before"));
  const auto after = levels_of(*spec.find("after"));
  if (const sde::Json* error = spec.find("error")) {
    try {
      (void)sde::book_diff(side, before, after);
      ADD_FAILURE() << "accepted";
    } catch (const std::invalid_argument& refused) {
      EXPECT_NE(std::string(refused.what()).find(error->as_string()), std::string::npos)
          << refused.what();
    }
    return;
  }
  EXPECT_EQ(sde::book_diff(side, before, after), levels_of(*spec.find("diff")));
}

INSTANTIATE_TEST_SUITE_P(Books, BookDiffVector, ::testing::ValuesIn(of_kind("diff")),
                         [](const auto& param_info) { return test_name(param_info.param); });

void step(sde::BookStream& stream, const sde::Json& step) {
  const std::string& op = step.find("op")->as_string();
  std::optional<std::int64_t> ts;
  if (const sde::Json* given = step.find("ts")) ts = given->to_int64().value();
  if (op == "resync") {
    std::optional<sde::BookSide> side;
    if (const sde::Json* given = step.find("side")) side = sde::book_side(given->as_string());
    stream.resync(side, ts);
    return;
  }
  const sde::BookSide side = sde::book_side(step.find("side")->as_string());
  const auto levels = levels_of(*step.find("levels"));
  if (op == "update") {
    stream.update(side, levels, ts);
  } else if (op == "apply") {
    stream.apply(side, levels, ts);
  } else {
    FAIL() << "an op this runner does not know: " << op;
  }
}

class BookStreamVector : public ::testing::TestWithParam<std::string> {};

TEST_P(BookStreamVector, SendsWhatTheVectorSays) {
  const sde::Json spec = read_json(vectors_root() / "books" / GetParam() / "case.json");
  SCOPED_TRACE(spec.find("name")->as_string());
  auto sink = std::make_shared<Recording>();
  double now = 0.0;
  sde::BookStreamOptions options;
  options.mode = sde::book_stream_mode(spec.find("mode")->as_string());
  const sde::Json& resync = *spec.find("resync");
  options.resync =
      sde::Resync(resync.find("updates")->to_int64().value(), resync.find("seconds")->to_double().value());
  if (const sde::Json* most = spec.find("max_levels")) {
    options.max_levels = static_cast<std::size_t>(most->to_int64().value());
  }
  options.clock = [&now] { return now; };
  const sde::Json* wall = spec.find("wall_ns");
  options.wall_clock_ns = [wall]() -> std::int64_t {
    if (wall == nullptr) {
      ADD_FAILURE() << "a step without a time in a case without the machine's clock";
      return 0;
    }
    return wall->to_int64().value();
  };
  sde::BookStream stream(sink, options);
  for (const sde::Json& each : spec.find("steps")->as_array()) {
    now = each.find("at")->to_double().value();
    if (each.find("op")->as_string() == "fail_next") {
      sink->fail_next = true;
      continue;
    }
    if (const sde::Json* error = each.find("error")) {
      const std::size_t before = sink->sent.size();
      try {
        step(stream, each);
        ADD_FAILURE() << "accepted a step the vector refuses: " << error->as_string();
      } catch (const std::exception& refused) {
        // The reference runner takes ValueError or the sink's RuntimeError; a ResourceBusy here
        // would be a defect of the stream, and its text matches nothing a vector expects.
        EXPECT_NE(std::string(refused.what()).find(error->as_string()), std::string::npos)
            << refused.what();
      }
      EXPECT_EQ(sink->sent.size(), before) << "a refused call sent something";
    } else {
      step(stream, each);
    }
  }
  std::vector<Sent> expected;
  for (const sde::Json& sent : spec.find("sent")->as_array()) {
    expected.push_back({sent.find("kind")->as_string(), sent.find("side")->as_string(),
                        sent.find("ts")->to_int64().value(), levels_of(*sent.find("levels"))});
  }
  EXPECT_EQ(sink->sent, expected);
  if (const sde::Json* book = spec.find("book")) {
    for (const auto& [side, levels] : book->as_object()) {
      EXPECT_EQ(stream.book(sde::book_side(side)), levels_of(levels)) << side;
    }
  }
}

INSTANTIATE_TEST_SUITE_P(Books, BookStreamVector, ::testing::ValuesIn(of_kind("stream")),
                         [](const auto& param_info) { return test_name(param_info.param); });

TEST(BooksFamily, EveryCaseRunsAndBothKindsAreThere) {
  EXPECT_GE(count_cases("books"), 26U);
  EXPECT_EQ(of_kind("diff").size() + of_kind("stream").size(), count_cases("books"));
  EXPECT_FALSE(of_kind("diff").empty());
  EXPECT_FALSE(of_kind("stream").empty());
}

}  // namespace
