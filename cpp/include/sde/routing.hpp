#pragma once

/// Routing (format contract section 8): which copy of a group one operation goes to.
///
/// A lookup and three conditions, and nothing else. The judgement - which copy should serve which
/// shape - needs telemetry, a cost model and an explanation, so it is made once, in the control
/// plane, and arrives here as the map's routing table. Reimplementing it in every language and
/// keeping the copies identical forever is not a plan.

#include "sde/model.hpp"
#include "sde/placement.hpp"

namespace sde {

struct RouteContext {
  /// The operation runs inside a transaction that has already written: only the source shows it.
  bool in_write_transaction = false;
  /// The caller asked for no staleness: a derived copy is behind by design.
  bool fresh = false;
};

/// The materialisation an operation of this shape goes to:
/// 1. a write or bulk write goes to the source, always, before anything else is consulted;
/// 2. so does anything inside a transaction that has written;
/// 3. so does anything that asked to be fresh;
/// 4. otherwise the routing table's entry for the shape, and the source when there is none.
/// `MapError` when the map does not place the shape's group.
[[nodiscard]] const Materialization& resolve(const PlacementMap& map, const OperationShape& shape,
                                             const RouteContext& context = {});

}  // namespace sde
