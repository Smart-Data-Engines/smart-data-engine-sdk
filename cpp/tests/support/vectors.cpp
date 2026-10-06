#include "support/vectors.hpp"

#include <algorithm>
#include <fstream>
#include <iterator>
#include <sstream>

#include "sde/errors.hpp"

namespace sde::testing_support {

namespace fs = std::filesystem;

fs::path vectors_root() { return fs::path(SDE_VECTORS_DIR); }

std::size_t count_cases(std::string_view family) {
  const fs::path directory = vectors_root() / std::string(family);
  if (!fs::is_directory(directory)) return 0;
  std::size_t count = 0;
  for (const auto& entry : fs::directory_iterator(directory)) {
    if (entry.is_directory()) ++count;
  }
  return count;
}

std::vector<std::string> cases(std::string_view family) {
  std::vector<std::string> names;
  const fs::path directory = vectors_root() / std::string(family);
  if (fs::is_directory(directory)) {
    for (const auto& entry : fs::directory_iterator(directory)) {
      if (entry.is_directory()) names.push_back(entry.path().filename().string());
    }
  }
  std::sort(names.begin(), names.end());
  return names;
}

std::string read_bytes(const fs::path& path) {
  std::ifstream stream(path, std::ios::binary);
  if (!stream) throw std::runtime_error("cannot read " + path.string());
  return std::string(std::istreambuf_iterator<char>(stream), std::istreambuf_iterator<char>());
}

std::string read_text(const fs::path& path) {
  std::string text = read_bytes(path);
  if (!text.empty() && text.back() == '\n') text.pop_back();
  return text;
}

sde::Json read_json(const fs::path& path) { return sde::parse_json(read_bytes(path)); }

bool has_file(const fs::path& path) { return fs::exists(path); }

std::string test_name(const std::string& directory) {
  std::string name = "v";
  for (const char c : directory) {
    const bool word = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9');
    name.push_back(word ? c : '_');
  }
  return name;
}

bool is_instance(const std::exception& error, std::string_view class_name) {
  if (class_name == "SdeError") return dynamic_cast<const sde::SdeError*>(&error) != nullptr;
  if (class_name == "CanonicalError") return dynamic_cast<const sde::CanonicalError*>(&error) != nullptr;
  if (class_name == "DeclarationError")
    return dynamic_cast<const sde::DeclarationError*>(&error) != nullptr;
  if (class_name == "ModelPlanningError")
    return dynamic_cast<const sde::ModelPlanningError*>(&error) != nullptr;
  if (class_name == "BulkWriteRefused")
    return dynamic_cast<const sde::BulkWriteRefused*>(&error) != nullptr;
  if (class_name == "MapError") return dynamic_cast<const sde::MapError*>(&error) != nullptr;
  if (class_name == "MapRolledBack") return dynamic_cast<const sde::MapRolledBack*>(&error) != nullptr;
  if (class_name == "EngineError") return dynamic_cast<const sde::EngineError*>(&error) != nullptr;
  if (class_name == "ResourceBusy") return dynamic_cast<const sde::ResourceBusy*>(&error) != nullptr;
  if (class_name == "ResourceClosed") return dynamic_cast<const sde::ResourceClosed*>(&error) != nullptr;
  if (class_name == "MigrationRefused")
    return dynamic_cast<const sde::MigrationRefused*>(&error) != nullptr;
  if (class_name == "QueryRefused") return dynamic_cast<const sde::QueryRefused*>(&error) != nullptr;
  ADD_FAILURE() << "the runner does not know the error class " << class_name
                << "; an unknown class is a failure, never a pass";
  return false;
}

void expect_refusal(const std::function<void()>& body, std::string_view class_name,
                    std::string_view match) {
  try {
    body();
  } catch (const std::exception& error) {
    EXPECT_TRUE(is_instance(error, class_name))
        << "expected " << class_name << ", got a different class: " << error.what();
    EXPECT_NE(std::string_view(error.what()).find(match), std::string_view::npos)
        << "the message must contain \"" << match << "\": " << error.what();
    return;
  }
  ADD_FAILURE() << "expected " << class_name << " containing \"" << match << "\", got no refusal";
}

}  // namespace sde::testing_support
