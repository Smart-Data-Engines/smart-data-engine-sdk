/**
 * The one derivation this library needs from a model that a map does not already carry.
 *
 * A placement map states a layout - table names and **dialect** column types - so nothing here has
 * to work out what a column is called or how PostgreSQL spells a timestamp. That is the planner's
 * job and it is in the control plane.
 *
 * What a map does not carry is the **neutral** type of a column, and one gate needs it: the
 * migration precision check. `timestamptz` is microseconds in PostgreSQL and milliseconds in
 * ClickHouse, so copying that column in that direction truncates every value that has more
 * precision - silently, because the insert succeeds and the value comes back changed. Deciding that
 * from the dialect spellings would mean parsing `DateTime64(3, 'UTC')`, which is a second
 * implementation of the type mapping; deciding it from the neutral vocabulary is a lookup.
 *
 * So this file is deliberately one function and not a layout module. The reference implementation
 * has a much larger one because it also derives the layout itself for the control plane to issue;
 * a client library never does that, and porting it here would be code with no caller and a rule to
 * keep in sync for nothing.
 */

import { compareCodePoints } from './canonical.js'
import type { Group } from './groups.js'
import type { LogicalModel } from './model.js'

/**
 * Every column a group's tables need, per entity, in the neutral vocabulary.
 *
 * A group has a foreign-key column for every relation whose source is a member, named
 * `<relation>_<target key field>` - one column per key field, for a single-field key and a
 * composite one alike. Today those add no *type* the group did not already have, because a relation
 * unions its two ends into one colocation group, so the target's key fields are declared fields of
 * a member. That is a fact about how groups are formed rather than about layouts, and this
 * derivation does not rely on it.
 *
 * Declared field order first, then relations in name order - the same order the reference produces,
 * because the migration gate iterates these and a refusal has to name the same column first in both
 * languages.
 */
export function groupColumns(
  model: LogicalModel,
  group: Group,
): Readonly<Record<string, Readonly<Record<string, string>>>> {
  const out: Record<string, Record<string, string>> = {}
  const relations = [...model.relations].sort((a, b) => compareCodePoints(a.name, b.name))
  for (const member of group.members) {
    const spec = model.entities.find((entity) => entity.name === member)
    if (spec === undefined) continue
    const columns: Record<string, string> = {}
    for (const field of spec.fields) columns[field.name] = field.type
    for (const relation of relations) {
      if (relation.source !== member) continue
      const target = model.entities.find((entity) => entity.name === relation.target)
      if (target === undefined) continue
      for (const keyField of target.key) {
        const declared = target.fields.find((field) => field.name === keyField)
        if (declared === undefined) continue
        columns[`${relation.name}_${keyField}`] = declared.type
      }
    }
    out[member] = columns
  }
  return out
}
