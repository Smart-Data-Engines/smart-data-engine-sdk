/**
 * Colocation groups: the unit of placement.
 *
 * Entities queried together, or declared to change together, live in the same engine. A group is a
 * connected component of the graph whose edges are relations and declared atomicity.
 *
 * The argument for why relation-as-edge does not collapse a normalised model into a single group is
 * in the Python implementation and in the format contract. It is not repeated here: a second copy of
 * an argument is a second thing that has to stay true.
 */

import { compareCodePoints } from './canonical.js'
import { ModelPlanningError } from './errors.js'
import type { LogicalModel } from './model.js'

export interface Group {
  readonly name: string
  readonly members: readonly string[]
}

export function colocationGroups(model: LogicalModel): readonly Group[] {
  const names = model.entities.map((e) => e.name).sort(compareCodePoints)
  const parent = new Map<string, string>()
  for (const name of names) parent.set(name, name)

  const find = (x: string): string => {
    let cur = x
    while (parent.get(cur) !== cur) {
      const next = parent.get(cur)!
      parent.set(cur, parent.get(next)!)
      cur = parent.get(cur)!
    }
    return cur
  }
  const union = (a: string, b: string): void => {
    const ra = find(a)
    const rb = find(b)
    if (ra === rb) return
    // Attach to the smaller root so a component's representative never depends on visit order.
    const [small, large] = compareCodePoints(ra, rb) <= 0 ? [ra, rb] : [rb, ra]
    parent.set(large, small)
  }

  for (const relation of model.relations) union(relation.source, relation.target)
  for (const group of model.atomic) {
    const first = group[0]!
    for (const other of group.slice(1)) union(first, other)
  }

  const buckets = new Map<string, string[]>()
  for (const name of names) {
    const root = find(name)
    const bucket = buckets.get(root)
    if (bucket) bucket.push(name)
    else buckets.set(root, [name])
  }

  return [...buckets.values()]
    .map((members) => {
      const sorted = [...members].sort(compareCodePoints)
      return { name: sorted[0]!, members: sorted }
    })
    .sort((a, b) => compareCodePoints(a.name, b.name))
}

export function groupOf(groups: readonly Group[], entity: string): Group {
  const found = groups.find((g) => g.members.includes(entity))
  if (!found) {
    throw new ModelPlanningError(`${entity} is not in any group, which means it is not in the model`)
  }
  return found
}
