#include "ownership_internal.hpp"
#include "sde/ownership.hpp"

#include <regex>

#include "crypto.hpp"
#include "python_compat.hpp"
#include "sde/errors.hpp"

namespace sde::detail {

namespace {

std::string digest(std::string_view text, std::size_t length) {
  return to_hex(sha256(text)).substr(0, length);
}

bool project_id(std::string_view value) {
  if (value.size() != 32) return false;
  for (const char c : value) {
    if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'))) return false;
  }
  return true;
}

}  // namespace

std::string copy_marker(std::string_view project, std::string_view group, std::string_view table) {
  if (!project_id(project)) {
    throw MigrationRefused("a copy's mark needs the project's id: 32 lowercase hexadecimal digits");
  }
  return "sde.copy1." + std::string(project) + "." + digest(group, 16) + "." + digest(table, 16);
}

std::string stage_marker(std::string_view project, std::string_view stage_id,
                         std::string_view table) {
  return "sde.stage1." + std::string(project) + "." + std::string(stage_id) + "." +
         digest(table, 16);
}

bool owned_copy_marker(std::string_view project, std::string_view group, std::string_view table,
                       const std::optional<std::string>& found) {
  if (!found || found->empty()) return false;
  if (*found == copy_marker(project, group, table)) return true;
  static const std::regex staged("sde_m_([0-9a-f]{32})_[0-9]{6}");
  std::match_results<std::string_view::const_iterator> match;
  if (!std::regex_match(table.begin(), table.end(), match, staged)) return false;
  return *found == stage_marker(project, match[1].str(), table);
}

std::string copy_refusal(std::string_view table, const std::optional<std::string>& found) {
  static const std::regex source(R"(sde\.source1\.[0-9a-f]{32}\.[0-9a-f]{16}\.[0-9a-f]{16})");
  if (found && std::regex_match(*found, source)) {
    return "the copy's table " + python_repr(table) +
           " exists and is not one this library created for it: it carries a source's mark. A "
           "cutover made it the source, and a table that has been a source holds the "
           "application's rows: it is never a copy again.";
  }
  const char* what = found && !found->empty() ? "another mark" : "no mark";
  return "the copy's table " + python_repr(table) +
         " exists and is not one this library created for it: it carries " + what +
         ". A copy is written over, emptied and refilled, so a table of its name and shape is "
         "not taken for it. A copy provisioned before copies were marked is made again "
         "(docs/upgrading.md); a table of yours is given another name in the map.";
}

std::string source_refusal(std::string_view table, std::string_view group) {
  return "the source's table " + python_repr(table) + " of " + std::string(group) +
         " carries a copy's mark: a copy becomes the source only through a cutover, which writes a "
         "source's mark in place of the copy's, so this is a copy a map names as a source (format "
         "contract §7r)";
}

}  // namespace sde::detail

namespace sde {

std::string copy_marker(std::string_view project_id, std::string_view group,
                        std::string_view table) {
  return detail::copy_marker(project_id, group, table);
}

bool owned_copy_marker(std::string_view project_id, std::string_view group, std::string_view table,
                       const std::optional<std::string>& found) {
  return detail::owned_copy_marker(project_id, group, table, found);
}

std::string copy_refusal(std::string_view table, const std::optional<std::string>& found) {
  return detail::copy_refusal(table, found);
}

}  // namespace sde
