#pragma once

/// Structured logging, through a sink the application passes in.
///
/// There is no global logger, and a library given no sink says nothing: a library that prints is a
/// library people patch. Every event has a name from a closed vocabulary - the reference's, so one
/// grep finds a line from any language - and its fields are structure: versions, the names of
/// groups, engines and tables, counts. Never a row's values: there is no path by which one reaches a
/// log line.

#include <functional>
#include <string_view>

#include "sde/json.hpp"

namespace sde {

/// Receives one event: its name and an object of fields.
using LogSink = std::function<void(std::string_view event, const Json& fields)>;

/// The event names this library emits.
inline constexpr std::string_view LOG_EVENTS[] = {
    "sde.map.loaded",   "sde.map.rejected",    "sde.route.resolved",
    "sde.route.fallback",
};

}  // namespace sde
