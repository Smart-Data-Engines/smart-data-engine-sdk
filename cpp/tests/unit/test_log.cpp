/// The closed vocabulary of log events (`sde/log.hpp`), held to the code that emits them and to the
/// reference's. A name an `emit` call uses and the list lacks is the drift the vocabulary exists to
/// stop; a name the list carries and nothing emits is a line an alert waits for and never gets.
///
/// Read from the sources as they are in the tree, as the reference's own test reads its package: a
/// name goes unnoticed on a rare path, and a test that runs the code reaches only the paths it runs.

#include <filesystem>
#include <fstream>
#include <iterator>
#include <regex>
#include <set>
#include <sstream>
#include <string>

#include <gtest/gtest.h>

#include "sde/log.hpp"

namespace {

std::string read(const std::filesystem::path& path) {
  std::ifstream in(path, std::ios::binary);
  std::ostringstream text;
  text << in.rdbuf();
  return text.str();
}

struct Emitted {
  std::set<std::string> names;
  std::set<std::string> files;  ///< the files a name was found in
  std::size_t calls = 0;        ///< every `emit(` but its definition's
  std::size_t named = 0;        ///< those whose event is a literal name
};

Emitted emitted() {
  Emitted out;
  const std::regex call(R"(\bemit\()");
  const std::regex literal(R"re(\bemit\(\s*[^,()]+,\s*"(sde\.[a-z_]+\.[a-z_]+)")re");
  for (const auto& entry : std::filesystem::recursive_directory_iterator(SDE_SOURCE_DIR)) {
    if (entry.path().extension() != ".cpp" || entry.path().filename() == "log.cpp") continue;
    const std::string text = read(entry.path());
    out.calls += static_cast<std::size_t>(
        std::distance(std::sregex_iterator(text.begin(), text.end(), call), std::sregex_iterator()));
    for (auto match = std::sregex_iterator(text.begin(), text.end(), literal);
         match != std::sregex_iterator(); ++match) {
      ++out.named;
      out.names.insert((*match)[1].str());
      out.files.insert(entry.path().filename().string());
    }
  }
  return out;
}

std::set<std::string> listed() {
  std::set<std::string> out;
  for (const std::string_view name : sde::LOG_EVENTS) out.emplace(name);
  return out;
}

TEST(LogVocabulary, IsExactlyWhatTheLibraryEmits) {
  const Emitted found = emitted();
  EXPECT_EQ(found.calls, found.named) << "an emit call names its event with something other "
                                         "than a literal, which no reading of the sources can check";
  EXPECT_EQ(found.names, listed());
  EXPECT_EQ(listed().size(), std::size(sde::LOG_EVENTS)) << "a name is listed twice";
  // The instrument reaches the adapters, which live in a directory of their own.
  EXPECT_EQ(found.files.count("postgres.cpp"), 1U);
  EXPECT_EQ(found.names.count("sde.write.failed"), 1U);
}

TEST(LogVocabulary, EveryNameIsTheReferences) {
  const std::string reference = read(SDE_REFERENCE_LOGGING);
  const std::size_t begin = reference.find("EVENTS: Final[frozenset[str]] = frozenset(");
  ASSERT_NE(begin, std::string::npos);
  const std::size_t end = reference.find("\n)\n", begin);
  ASSERT_NE(end, std::string::npos);
  const std::string block = reference.substr(begin, end - begin);
  const std::regex literal(R"re("(sde\.[a-z_]+\.[a-z_]+)")re");
  std::set<std::string> theirs;
  for (auto match = std::sregex_iterator(block.begin(), block.end(), literal);
       match != std::sregex_iterator(); ++match) {
    theirs.insert((*match)[1].str());
  }
  // Read whole: the block holds names this library does not emit.
  EXPECT_EQ(theirs.count("sde.model.built"), 1U);
  EXPECT_EQ(theirs.count("sde.route.resolved"), 1U);
  for (const std::string& name : listed()) EXPECT_EQ(theirs.count(name), 1U) << name;
}

}  // namespace
