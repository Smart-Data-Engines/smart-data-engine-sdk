/**
 * The marks of a map's tables, read where a library writes to them (format contract §7r).
 *
 * `prepareSchema` creates a copy's tables with a mark, and the operator reads it before it writes to
 * a copy. An application's session wrote to a map's copies - its fan-out - without reading it, so a
 * forged map could still direct those writes to a table of the project of the copy's shape. A
 * session of a map with write generations now reads, when it opens, every table of the map through
 * its binding, once a binding: a copy's table carries its own mark, a source's carries no copy's,
 * and no two names of the map reach one table, a copy's or a source's. `prepareSchema` reads the
 * same before its first statement and after its last. The reference is Python's `sde.table_marks`.
 */
import { compareCodePoints } from './canonical.js'
import { MigrationRefused } from './errors.js'
import { COPY_MARKER, copyRefusal, ownedCopyMarker, sourceRefusal } from './ownership.js'
import type { PlacementMap } from './placement.js'

/** A name's table as a connection reaches it: its identities (JSON tuples) and its mark. */
export interface TableFacts {
  readonly keys: readonly string[]
  readonly mark: string | null
}

/** An adapter that can say what a map's names reach (`tableFacts`, format contract §7r). */
export interface ReadsTableFacts {
  tableFacts(tables: readonly string[]): Promise<Record<string, TableFacts>>
}

type Named = { group: string; materialization: string; entity: string; table: string; copy: boolean }

/**
 * Refuse a map whose tables are not what it calls them (format contract §7r), as the reference
 * does: for the groups with a write generation, each copy's table carries this copy's mark, each
 * source's table carries no copy's mark, and no copy's or source's table is reached by another name
 * of the map.
 * `copiesPresent` is false before `prepareSchema` creates the copies.
 */
export async function checkTableMarks(
  placement: PlacementMap,
  engines: Readonly<Record<string, unknown>>,
  projectId: string,
  options: { readonly copiesPresent?: boolean } = {},
): Promise<void> {
  if (placement.contract < 4) return
  const copiesPresent = options.copiesPresent ?? true
  const ordered: { engine: string; named: Named }[] = []
  const byEngine = new Map<string, Named[]>()
  for (const group of Object.keys(placement.groups).sort(compareCodePoints)) {
    const spot = placement.groups[group]!
    if (spot.writeEpoch === undefined) continue
    for (const material of [spot.source, ...spot.derived]) {
      const copy = material !== spot.source
      for (const entity of Object.keys(material.layout.tables).sort(compareCodePoints)) {
        const named = { group, materialization: material.id, entity, table: material.layout.tables[entity]!, copy }
        ordered.push({ engine: material.engine, named })
        byEngine.set(material.engine, [...(byEngine.get(material.engine) ?? []), named])
      }
    }
  }
  const facts = new Map<string, Record<string, TableFacts>>()
  for (const engine of [...byEngine.keys()].sort(compareCodePoints)) {
    const names = byEngine.get(engine)!
    const adapter = engines[engine] as Partial<ReadsTableFacts> | undefined
    if (adapter !== undefined && typeof adapter.tableFacts === 'function') {
      facts.set(engine, await adapter.tableFacts(names.map((named) => named.table)))
    } else if (names.some((named) => named.copy)) {
      throw new MigrationRefused(
        `engine ${engine} holds a copy, and its adapter cannot read the mark a copy's table carries ` +
          '(tableFacts, format contract §7r)',
      )
    }
  }
  const reach = new Map<string, { engine: string; named: Named }[]>()
  for (const { engine, named } of ordered) {
    const found = facts.get(engine)?.[named.table]
    if (found === undefined) continue
    if (named.copy) {
      if (found.keys.length > 0 && !ownedCopyMarker(projectId, named.group, named.table, found.mark)) {
        throw new MigrationRefused(copyRefusal(named.table, found.mark))
      }
      if (found.keys.length === 0 && copiesPresent) {
        throw new MigrationRefused(
          `the copy's table '${named.table}' of ${named.group} is not there: provision the map ` +
            '(prepareSchema) before a session writes to it',
        )
      }
    } else if (found.mark !== null && COPY_MARKER.test(found.mark)) {
      throw new MigrationRefused(sourceRefusal(named.table, named.group))
    }
    for (const key of [...found.keys].sort(compareCodePoints)) reach.set(key, [...(reach.get(key) ?? []), { engine, named }])
  }
  for (const { engine, named } of ordered) {
    const found = facts.get(engine)?.[named.table]
    if (found === undefined) continue
    for (const key of [...found.keys].sort(compareCodePoints)) {
      for (const other of reach.get(key) ?? []) {
        const same = other.named.group === named.group && other.named.materialization === named.materialization &&
          other.named.table === named.table
        if (!same) {
          const [whose, alone] = named.copy ? ["copy's", 'that copy'] : ["source's", 'its group']
          throw new MigrationRefused(
            `the ${whose} table '${named.table}' of ${named.group} is also ${other.named.group}'s table ` +
              `'${other.named.table}' for ${other.named.entity} in ${other.engine}: a ${whose} table holds ` +
              `${alone} alone, and two names of the map reach this one`,
          )
        }
      }
    }
  }
}
