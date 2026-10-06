#pragma once

#include <string>
#include <string_view>

namespace sde::detail {

/// Refuses a type outside the vocabulary of section 3, with the decimal-specific message for
/// anything beginning with "decimal". `where` names the field, as `Entity.field`.
void check_type(std::string_view type, const std::string& where);

}  // namespace sde::detail
