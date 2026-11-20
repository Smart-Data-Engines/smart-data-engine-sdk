#pragma once

/// Whose table a copy is (format contract section 7r): the mark a library writes on a copy's table
/// when it creates it, and the rule every library reads it by. A map is the control plane's word,
/// so a table is a copy because the library created it as one and marked it, not because a map
/// names it: a copy named like a table the client already has would otherwise be written over.

#include <optional>
#include <string>
#include <string_view>

namespace sde::detail {

/// `sde.copy1.<project>.<sha256(group)[:16]>.<sha256(table)[:16]>`: what `prepare_schema` writes on
/// a copy's table it creates. Refuses (`MigrationRefused`) a project id that is not 32 lowercase
/// hexadecimal digits.
[[nodiscard]] std::string copy_marker(std::string_view project_id, std::string_view group,
                                      std::string_view table);

/// `sde.stage1.<project>.<stage>.<sha256(table)[:16]>`: what a staging writes on the copy it
/// creates, which it names `sde_m_<stage>_<NNNNNN>`.
[[nodiscard]] std::string stage_marker(std::string_view project_id, std::string_view stage_id,
                                       std::string_view table);

/// Whether `found`, the mark read on `table`, makes it a copy of `group` in this project.
[[nodiscard]] bool owned_copy_marker(std::string_view project_id, std::string_view group,
                                     std::string_view table, const std::optional<std::string>& found);

/// Why a source's table that carries a copy's mark is not taken as `group`'s source.
[[nodiscard]] std::string source_refusal(std::string_view table, std::string_view group);

/// Why `table` is not taken as a copy's: what every library says, with what it found.
[[nodiscard]] std::string copy_refusal(std::string_view table,
                                       const std::optional<std::string>& found);

}  // namespace sde::detail
