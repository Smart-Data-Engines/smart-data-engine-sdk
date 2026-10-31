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

/// The event names this library emits: every one is also the reference's.
inline constexpr std::string_view LOG_EVENTS[] = {
    "sde.map.loaded",
    "sde.map.rejected",
    "sde.map.forward_only",
    "sde.map.rollback_unprotected",
    "sde.route.resolved",
    "sde.route.fallback",
    "sde.schema.applied",
    "sde.schema.physical_mismatch",
    "sde.migration.divergence",
    "sde.migration.backfill_progress",
    "sde.telemetry.storage_unavailable",
};

namespace detail {

/// Emits one event through the sink, and never raises: the sink belongs to the application, and a
/// library that fails an operation because its own log line could not be written has no business
/// being in somebody else's process.
void emit(const LogSink& sink, std::string_view event, const Json& fields) noexcept;

}  // namespace detail

}  // namespace sde
