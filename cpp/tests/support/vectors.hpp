#pragma once

/// Reading the shared conformance vectors (conformance/README.md), with the conventions that are not
/// obvious from the tree: `ir.json` and `bytes.json` are bytes without a trailing newline, the `.txt`
/// files are text that ends with one, and an `errors/` case names a class and a fragment of its
/// message.

#include <filesystem>
#include <functional>
#include <string>
#include <string_view>
#include <vector>

#include <gtest/gtest.h>

#include "sde/json.hpp"

namespace sde::testing_support {

[[nodiscard]] std::filesystem::path vectors_root();

/// The case directories of a family, sorted by name. Fails the calling test when there are none:
/// a green suite that found no files is worse than a red one.
[[nodiscard]] std::vector<std::string> cases(std::string_view family);

/// The number of case directories of a family, without failing on zero (for the count check).
[[nodiscard]] std::size_t count_cases(std::string_view family);

[[nodiscard]] std::string read_bytes(const std::filesystem::path& path);
/// A `.txt` file's text with its one trailing newline removed.
[[nodiscard]] std::string read_text(const std::filesystem::path& path);
[[nodiscard]] sde::Json read_json(const std::filesystem::path& path);
[[nodiscard]] bool has_file(const std::filesystem::path& path);

/// A gtest-safe name for a case directory: `001-key-order` becomes `v001_key_order`.
[[nodiscard]] std::string test_name(const std::string& directory);

/// Whether `error` is an instance of the library class the vectors call `class_name` (a subclass
/// counts, as `isinstance` does in the reference runner).
[[nodiscard]] bool is_instance(const std::exception& error, std::string_view class_name);

/// Runs `body`, which must throw the named class with `match` in its message.
void expect_refusal(const std::function<void()>& body, std::string_view class_name,
                    std::string_view match);

}  // namespace sde::testing_support
