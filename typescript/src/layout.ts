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
import type { PhysicalLayout } from './placement.js'

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

// The spellings of a temporal column type each dialect is read for a zone in (format contract 7a).
// Spaces only and ASCII digits: `\s` and `\d` match more in some languages than in others.
const CH_ZONED = /^(?:DateTime64\( *[0-9]+ *, *'[^']*' *\)|DateTime\( *'[^']*' *\))$/
const CH_ZONELESS = /^(?:DateTime64\( *[0-9]+ *\)|DateTime)$/
const PG_ZONED = /^(?:timestamptz( *\( *[0-9]+ *\))?|timestamp( *\( *[0-9]+ *\))? +with +time +zone)$/
const PG_ZONELESS = /^timestamp( *\( *[0-9]+ *\))?( +without +time +zone)?$/

/**
 * The type inside one `Nullable(...)`, without the spaces around it, or the type itself.
 *
 * Read without a regular expression: a lazy group beside optional spaces, `/^Nullable\( *(.*?) *\)$/`,
 * backtracks through every split of a long run of spaces, which is quadratic in it (CodeQL's
 * `js/polynomial-redos`, on a layout's column type). The same text either way, as the reference's.
 */
function nullableInner(columnType: string): string {
  if (!columnType.startsWith('Nullable(') || !columnType.endsWith(')')) return columnType
  let start = 'Nullable('.length
  let end = columnType.length - 1
  while (start < end && columnType[start] === ' ') start++
  while (end > start && columnType[end - 1] === ' ') end--
  return columnType.slice(start, end)
}

function asciiLower(text: string): string {
  return text.replace(/[A-Z]/g, (letter) => String.fromCharCode(letter.charCodeAt(0) + 32))
}

/** A string as Python's `repr` writes one without escapes: double quotes when it holds a `'`. */
function pythonQuoted(text: string): string {
  return text.includes("'") && !text.includes('"') ? `"${text}"` : `'${text}'`
}

/**
 * Whether a temporal column type carries a zone in this dialect, or `null` when this is not a
 * temporal spelling of the dialect, or not a dialect the rule reads (format contract 7a).
 *
 * ClickHouse: `DateTime64(p, 'zone')` and `DateTime('zone')` carry one, `DateTime64(p)` and
 * `DateTime` do not, inside a `Nullable(...)` too. PostgreSQL, in any ASCII case: `timestamptz`,
 * `timestamptz(p)` and `timestamp(p) with time zone` carry one; `timestamp`, `timestamp(p)` and
 * `timestamp(p) without time zone` do not. Elasticsearch: `long/timestamptz` and `long/timestamp`.
 */
export function columnZone(columnType: string, dialect: string): boolean | null {
  if (dialect === 'clickhouse') {
    const inner = nullableInner(columnType)
    if (CH_ZONED.test(inner)) return true
    return CH_ZONELESS.test(inner) ? false : null
  }
  if (dialect === 'postgres') {
    const lowered = asciiLower(columnType)
    if (PG_ZONED.test(lowered)) return true
    return PG_ZONELESS.test(lowered) ? false : null
  }
  if (dialect === 'elasticsearch') {
    if (columnType === 'long/timestamptz') return true
    return columnType === 'long/timestamp' ? false : null
  }
  return null
}

/**
 * The refusal of a layout whose temporal column does not keep its field's zone, or `null`.
 *
 * A scan reads a value by its field's type and a read by key by its column's, so a `timestamptz`
 * field in a column without a zone - or a `timestamp` in one with a zone - would be read as two
 * types (finding 27 of the C++ port). Entities in name order, fields in name order.
 */
export function zoneRefusal(
  model: LogicalModel,
  group: string,
  materialId: string,
  layout: PhysicalLayout,
  dialect: string,
): string | null {
  const declared = new Map(model.entities.map((spec) => [spec.name, spec]))
  for (const entity of Object.keys(layout.columns).sort(compareCodePoints)) {
    const spec = declared.get(entity)
    if (spec === undefined) continue
    const columns = layout.columns[entity] ?? {}
    for (const field of [...spec.fields].sort((a, b) => compareCodePoints(a.name, b.name))) {
      const kind = Object.hasOwn(columns, field.name) ? columns[field.name] : undefined
      if ((field.type !== 'timestamp' && field.type !== 'timestamptz') || kind === undefined) continue
      const zoned = columnZone(kind, dialect)
      const wants = field.type === 'timestamptz'
      if (zoned === null || zoned === wants) continue
      const [must, found] = wants ? ['carry', 'has none'] : ['not carry', 'does']
      return (
        `group '${group}': '${materialId}': ${entity}.${field.name} is ${field.type}, so its ` +
        `${dialect} column must ${must} a zone, and ${pythonQuoted(kind)} ${found}. A scan reads a ` +
        `value by its field's type and a read by key by its column's, so one row would be read as ` +
        `two types (format contract 7a).`
      )
    }
  }
  return null
}
