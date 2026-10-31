#include "sde/log.hpp"

namespace sde::detail {

void emit(const LogSink& sink, std::string_view event, const Json& fields) noexcept {
  if (!sink) return;
  try {
    sink(event, fields);
  } catch (...) {
    // The sink is the application's. Its failure is not this library's operation failing, and an
    // exception escaping here would replace the one the caller is about to see, or invent one.
  }
}

}  // namespace sde::detail
