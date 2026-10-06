#include "sde/routing.hpp"

namespace sde {

const Materialization& resolve(const PlacementMap& map, const OperationShape& shape,
                               const RouteContext& context) {
  const GroupPlacement& group = map.placement_of(shape.group);
  if (is_write_kind(shape.kind)) return group.source;
  if (context.in_write_transaction || context.fresh) return group.source;
  const auto target = map.routing().find(shape.id);
  // No entry is not an error: a hand-written map has no routing table, and the source is always a
  // correct answer.
  if (target == map.routing().end()) return group.source;
  return group.by_id(target->second);
}

}  // namespace sde
