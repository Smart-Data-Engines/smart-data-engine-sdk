#pragma once

#include <string>
#include <string_view>

namespace sde::detail {

/// The reference's `_predicate`: a constraint's text as the engine wrote it back, normalised for
/// this library's own predicate grammar only - `1`, `0` or `__sde_write_epoch>=N` / `<=N` - and
/// `<unrecognized>` for anything else.
[[nodiscard]] std::string fence_predicate(std::string_view raw);

}  // namespace sde::detail
