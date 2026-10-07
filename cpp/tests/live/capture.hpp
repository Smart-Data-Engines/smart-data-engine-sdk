#pragma once

/// What the live tests compare besides values: the events an adapter or a session wrote, and the
/// text of a refusal of one expected class.

#include <functional>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

#include <gtest/gtest.h>

#include "sde/canonical.hpp"
#include "sde/json.hpp"
#include "sde/log.hpp"

namespace sde::live {

/// Every event written to its sink, in order.
struct Captured {
  std::vector<std::pair<std::string, Json>> events;
  [[nodiscard]] LogSink sink() {
    return [this](std::string_view event, const Json& fields) {
      events.emplace_back(std::string(event), fields);
    };
  }
  /// The events of one name, each as canonical JSON.
  [[nodiscard]] std::vector<std::string> of(std::string_view name) const {
    std::vector<std::string> out;
    for (const auto& [event, fields] : events) {
      if (event == name) out.push_back(canonical_bytes(fields));
    }
    return out;
  }
  [[nodiscard]] std::vector<std::string> names() const {
    std::vector<std::string> out;
    for (const auto& [event, fields] : events) out.push_back(event);
    return out;
  }
};

/// The message of the `Error` the body throws; a failure of the test when it throws nothing.
template <typename Error>
std::string refusal(const std::function<void()>& body) {
  try {
    body();
  } catch (const Error& error) {
    return error.what();
  }
  ADD_FAILURE() << "nothing was refused";
  return "";
}

}  // namespace sde::live
