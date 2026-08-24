/**
 * Hashed identifiers - a port of section 2a of the format contract, not of the Python file.
 *
 * The distinction is the same one `canonical.ts` makes, and it earns its keep the same way. What is
 * being pinned here is not the HMAC; anything computes an HMAC. It is the *message*: NFC-normalised
 * parts, joined with U+0000, prefix outside, fields hashed together with their entity. Every one of
 * those four is invisible in a test whose identifiers are ASCII, and three of them are things a
 * translator would plausibly get wrong while every existing test stayed green.
 *
 * The NFC rule is here because the reference implementation got it wrong first. Section 1 normalises
 * before emitting bytes, which is why two libraries that declare an identifier in different normal
 * forms compute the same `model_version`. Hashing the raw name threw that away: two digests, two
 * versions, and a placement map issued for the Python service that this one would refuse. It passed
 * every test in both languages, because every other identifier in every other test is ASCII, where
 * the two forms coincide. `conformance/vectors/hashing/002-normalisation` exists so that neither
 * implementation can lose it again.
 *
 * One JavaScript-specific note. Ordering matters twice below - members within an atomicity group,
 * and the groups among themselves - and both use `compareCodePoints` rather than the default
 * comparator. Hashed names are `e_` plus hex, so UTF-16 and code point order agree on them today and
 * the default sort would pass every vector. It is still wrong, for the same reason it is wrong in
 * `canonical.ts`: the day a prefix or a digest alphabet changes, a silent divergence is worse than a
 * loud one.
 */

import { createHmac } from 'node:crypto'
import { compareCodePoints } from './canonical.js'
import { DeclarationError } from './errors.js'
import type { CostCeiling, EntitySpec, LogicalModel, RelationSpec } from './model.js'
import { assemble } from './model.js'

/**
 * Twelve hex characters, i.e. 48 bits. Half a `model_version`, because these end up in table names.
 * A collision would merge two entities into one, so it is checked and refused rather than assumed.
 */
export const DIGEST_CHARS = 12

const ENTITY_PREFIX = 'e_'
const FIELD_PREFIX = 'f_'
const RELATION_PREFIX = 'r_'

/**
 * U+0000, which cannot occur in an identifier - so the join is unambiguous. Written as a code point
 * rather than an escape in a string literal, because a raw NUL in a source file survives a git diff,
 * a code review and a copy-paste in ways nobody intends.
 */
const SEPARATOR = String.fromCodePoint(0)

/** The shortest salt this library will accept. A guessable salt hides nothing. */
const MIN_SALT_BYTES = 16

/**
 * The translation between what the client wrote and what we see.
 *
 * Held only in the client's process. The library needs it because the placement map is keyed by
 * hashed names while the application still says `session.get('User', ...)`, so every lookup crosses
 * this boundary and this is the only thing that does.
 */
export interface NameMap {
  readonly entities: Readonly<Record<string, string>>
  readonly fields: Readonly<Record<string, Readonly<Record<string, string>>>>
  readonly relations: Readonly<Record<string, Readonly<Record<string, string>>>>
}

function digest(salt: Buffer, prefix: string, parts: readonly string[]): string {
  const message = parts.map((part) => part.normalize('NFC')).join(SEPARATOR)
  const hex = createHmac('sha256', salt).update(message, 'utf8').digest('hex')
  return prefix + hex.slice(0, DIGEST_CHARS)
}

/** Lexicographic over two already-sorted member lists, by code point. */
function compareGroups(left: readonly string[], right: readonly string[]): number {
  for (let index = 0; index < Math.min(left.length, right.length); index += 1) {
    const order = compareCodePoints(left[index] as string, right[index] as string)
    if (order !== 0) return order
  }
  return left.length - right.length
}

/**
 * Return an equivalent model with every identifier replaced by a keyed digest.
 *
 * What is deliberately not hashed: `residency`, because a jurisdiction is not an identifier and a
 * hashed placement constraint is an unenforceable one; the cost ceiling, because it is a number and
 * a currency; and types, keys and nullability, because the physical schema is derived from them and
 * none of them is a name.
 */
export function hashIdentifiers(
  model: LogicalModel,
  salt: Buffer,
): { readonly model: LogicalModel; readonly names: NameMap } {
  if (salt.length < MIN_SALT_BYTES) {
    throw new DeclarationError(`the salt must be at least ${MIN_SALT_BYTES} bytes`)
  }

  const entityNames: Record<string, string> = {}
  const seen = new Map<string, string>()
  for (const spec of model.entities) {
    const hashed = digest(salt, ENTITY_PREFIX, [spec.name])
    const clash = seen.get(hashed)
    if (clash !== undefined) {
      throw new DeclarationError(
        `${spec.name} and ${clash} hash to the same name. Refused rather than merged: a model ` +
          'with one entity where there were two would place both in one engine and write both ' +
          'into one table. Change the salt.',
      )
    }
    seen.set(hashed, spec.name)
    entityNames[spec.name] = hashed
  }

  const fieldNames: Record<string, Record<string, string>> = {}
  const relationNames: Record<string, Record<string, string>> = {}

  const entities: EntitySpec[] = []
  for (const spec of model.entities) {
    const mapping: Record<string, string> = {}
    for (const field of spec.fields) {
      mapping[field.name] = digest(salt, FIELD_PREFIX, [spec.name, field.name])
    }
    if (new Set(Object.values(mapping)).size !== Object.keys(mapping).length) {
      throw new DeclarationError(
        `two fields of ${spec.name} hash to the same name. Refused rather than merged. ` +
          'Change the salt.',
      )
    }
    fieldNames[spec.name] = mapping

    entities.push({
      name: entityNames[spec.name] as string,
      fields: spec.fields.map((field) => ({
        name: mapping[field.name] as string,
        type: field.type,
        nullable: field.nullable,
      })),
      key: spec.key.map((field) => mapping[field] as string),
      pii: spec.pii.map((field) => mapping[field] as string),
      residency: spec.residency,
    })
  }

  const relations: RelationSpec[] = []
  for (const relation of model.relations) {
    const hashed = digest(salt, RELATION_PREFIX, [relation.source, relation.name])
    const bucket = relationNames[relation.source] ?? {}
    bucket[relation.name] = hashed
    relationNames[relation.source] = bucket
    relations.push({
      name: hashed,
      source: entityNames[relation.source] as string,
      target: entityNames[relation.target] as string,
    })
  }

  const atomic = model.atomic
    .map((group) => group.map((member) => entityNames[member] as string).sort(compareCodePoints))
    .sort(compareGroups)

  const costCeiling: CostCeiling | null = model.costCeiling
  return {
    model: assemble(entities, relations, atomic, costCeiling),
    names: { entities: entityNames, fields: fieldNames, relations: relationNames },
  }
}
