/**
 * Whose table a copy is: the mark a library writes on a copy's table when it creates it (format
 * contract §7r).
 *
 * A placement map names a copy's tables, and the library writes to them alongside the source; the
 * local operator writes over them, empties and refills them and deletes rows from them. A map is the
 * control plane's word, so the name alone cannot make a table a copy: a copy named like a table the
 * client already has would be written over. So the library that creates a copy's table marks it,
 * and a table of a copy's name without its mark is refused before anything is written to it.
 */
import { createHash } from 'node:crypto'

import type { PhysicalFinding } from './physical.js'
import type { PhysicalLayout } from './placement.js'

/** What `prepareSchema` writes on a copy's table it creates: its project, group and name. */
export const COPY_MARKER = /^sde\.copy1\.[0-9a-f]{32}\.[0-9a-f]{16}\.[0-9a-f]{16}$/

/** What a staging writes on the table it creates: its project, its stage and the table's name. */
export const STAGE_MARKER = /^sde\.stage1\.[0-9a-f]{32}\.[0-9a-f]{32}\.[0-9a-f]{16}$/

/** What a cutover writes on the copy it makes the source, in place of the copy's mark. */
export const SOURCE_MARKER = /^sde\.source1\.[0-9a-f]{32}\.[0-9a-f]{16}\.[0-9a-f]{16}$/

const STAGED_NAME = /^sde_m_([0-9a-f]{32})_[0-9]{6}$/
const PROJECT = /^[0-9a-f]{32}$/

function digest(text: string, length: number): string {
  return createHash('sha256').update(text, 'utf8').digest('hex').slice(0, length)
}

/** The mark `prepareSchema` writes on `table`, a copy of `group` in `projectId`. */
export function copyMarker(projectId: string, group: string, table: string): string {
  if (!PROJECT.test(projectId)) {
    throw new Error("a copy's mark needs the project's id: 32 lowercase hexadecimal digits")
  }
  return `sde.copy1.${projectId}.${digest(group, 16)}.${digest(table, 16)}`
}

/** The mark a staging writes on `table`, the copy it creates for stage `stageId`. */
export function stageMarker(projectId: string, stageId: string, table: string): string {
  return `sde.stage1.${projectId}.${stageId}.${digest(table, 16)}`
}

/** Why `table` is not taken as a copy's: what every library says, with what it found. */
export function copyRefusal(table: string, found: string | null): string {
  if (found && SOURCE_MARKER.test(found)) {
    return (
      `the copy's table '${table}' exists and is not one this library created for it: it carries ` +
      "a source's mark. A cutover made it the source, and a table that has been a source holds " +
      "the application's rows: it is never a copy again."
    )
  }
  const what = found ? 'another mark' : 'no mark'
  return (
    `the copy's table '${table}' exists and is not one this library created for it: it carries ` +
    `${what}. A copy is written over, emptied and refilled, so a table of its name and shape is ` +
    'not taken for it. A copy provisioned before copies were marked is made again ' +
    '(docs/upgrading.md); a table of yours is given another name in the map.'
  )
}

/** Why a source's table that carries a copy's mark is not taken as `group`'s source. */
export function sourceRefusal(table: string, group: string): string {
  return (
    `the source's table '${table}' of ${group} carries a copy's mark: a copy becomes the source only ` +
    "through a cutover, which writes a source's mark in place of the copy's, so this is a copy a " +
    'map names as a source (format contract §7r)'
  )
}

/**
 * Whether `found`, the mark read on `table`, makes it a copy of `group` in this project: the mark
 * `prepareSchema` writes for exactly this group and table, or the one a staging of this project
 * writes on the copy it names after its stage.
 */
export function ownedCopyMarker(
  projectId: string,
  group: string,
  table: string,
  found: string | null,
): boolean {
  if (!found) return false
  if (found === copyMarker(projectId, group, table)) return true
  const staged = STAGED_NAME.exec(table)
  return staged !== null && found === stageMarker(projectId, staged[1]!, table)
}

/**
 * An adapter that creates a copy's tables marked and refuses one without its mark (§7r). A shape
 * of its own rather than a member of `Engine`, which a third party implements: `prepareSchema`
 * asks for it by name and refuses a copy whose adapter has none.
 */
export interface MarksCopies {
  ensureCopy(
    layout: PhysicalLayout,
    options: {
      readonly keys: Readonly<Record<string, readonly string[]>>
      readonly projectId: string
      readonly group: string
    },
  ): Promise<readonly PhysicalFinding[]>
}
