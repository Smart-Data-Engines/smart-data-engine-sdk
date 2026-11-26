#pragma once

/// Whose table a copy is (format contract section 7r), for an engine adapter of your own that
/// implements `MarksCopies`: the mark `prepare_schema` writes on a copy's table it creates, and the
/// rule a table found under a copy's name is taken by. The statements that create a copy's tables,
/// with the mark, are `copy_statements` (`sde/schema.hpp`).

#include <optional>
#include <string>
#include <string_view>

namespace sde {

/// `sde.copy1.<project>.<sha256(group)[:16]>.<sha256(table)[:16]>`. Refuses (`MigrationRefused`) a
/// project id that is not 32 lowercase hexadecimal digits.
[[nodiscard]] std::string copy_marker(std::string_view project_id, std::string_view group,
                                      std::string_view table);

/// Whether `found`, the mark read on `table`, makes it a copy of `group` in this project: the mark
/// `copy_marker` gives, or the one a staging of this project writes on the copy it names after its
/// stage. A source's mark, which a cutover writes on the copy it makes the source, never does.
[[nodiscard]] bool owned_copy_marker(std::string_view project_id, std::string_view group,
                                     std::string_view table, const std::optional<std::string>& found);

/// Whether `found` makes a table a copy - a copy's mark or a staging's, of any project and group -
/// so that a table carrying it is not taken as a source: a copy becomes the source only through a
/// cutover, which writes a source's mark in its place.
[[nodiscard]] bool copy_marks(const std::optional<std::string>& found);

/// Why `table` is not taken for a copy's: what every library says, with what it found.
[[nodiscard]] std::string copy_refusal(std::string_view table,
                                       const std::optional<std::string>& found);

}  // namespace sde
