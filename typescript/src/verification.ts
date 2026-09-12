/** A comparison bound to one request, locally configured project and exact placement map. */
import { canonicalString, compareCodePoints } from './canonical.js'
import { MigrationRefused } from './errors.js'
import type { PlacementMap } from './placement.js'
import { fingerprintOf, placementOf } from './placement.js'
import { Timestamp } from './timestamp.js'

export const REQUEST_PROTOCOL = 1

function hex(value: unknown, width: number, field: string): void {
  if (typeof value !== 'string' || !new RegExp(`^[0-9a-f]{${width}}$`).test(value)) {
    throw new MigrationRefused(`verification ${field} must be ${width} lowercase hexadecimal digits`)
  }
}

export function checkProjectId(value: string | undefined): void {
  if (value !== undefined) hex(value, 32, 'project_id')
}

function name(value: unknown): void {
  if (typeof value !== 'string' || value.length === 0) {
    throw new MigrationRefused('verification names must be nonempty strings')
  }
}

function awareTime(value: string): bigint {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d(?:\.\d{1,6})?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)$/.test(value)) {
    throw new MigrationRefused('verification time must be an ISO timestamp with an offset')
  }
  try {
    return Timestamp.from(value).epochMicroseconds
  } catch {
    throw new MigrationRefused('verification time must be an ISO timestamp with an offset')
  }
}

function exact(value: unknown, fields: readonly string[], label: string): asserts value is Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value) ||
      Object.keys(value).sort().join('|') !== [...fields].sort().join('|')) {
    throw new MigrationRefused(`verification ${label} has missing or unknown fields`)
  }
}

interface VerificationFields {
  readonly requestId: string
  readonly projectId: string
  readonly modelVersion: string
  readonly mapVersion: number
  readonly mapFingerprint: string
  readonly group: string
  readonly sourceEngine: string
  readonly sourceId: string
  readonly targets: readonly (readonly [string, string])[]
  readonly requestedAt: string
  readonly requiresSignature: boolean
}

export class VerificationRequest implements VerificationFields {
  readonly requestId: string
  readonly projectId: string
  readonly modelVersion: string
  readonly mapVersion: number
  readonly mapFingerprint: string
  readonly group: string
  readonly sourceEngine: string
  readonly sourceId: string
  readonly targets: readonly (readonly [string, string])[]
  readonly requestedAt: string
  readonly requiresSignature: boolean

  constructor(fields: VerificationFields) {
    hex(fields.requestId, 32, 'request_id')
    hex(fields.projectId, 32, 'project_id')
    hex(fields.modelVersion, 16, 'model_version')
    hex(fields.mapFingerprint, 64, 'map_fingerprint')
    if (!Number.isSafeInteger(fields.mapVersion) || fields.mapVersion < 1) {
      throw new MigrationRefused('verification map_version must be a positive integer')
    }
    if (typeof fields.requiresSignature !== 'boolean') {
      throw new MigrationRefused('verification requires_signature must be a boolean')
    }
    for (const value of [fields.group, fields.sourceEngine, fields.sourceId]) name(value)
    if (!Array.isArray(fields.targets) || fields.targets.length === 0) {
      throw new MigrationRefused('verification targets must be nonempty engine/id pairs')
    }
    for (const target of fields.targets) {
      if (!Array.isArray(target) || target.length !== 2) {
        throw new MigrationRefused('verification targets must be nonempty engine/id pairs')
      }
      name(target[0]); name(target[1])
      if (target[1] === fields.sourceId) {
        throw new MigrationRefused('verification source cannot also be a target')
      }
    }
    if (new Set(fields.targets.map((target) => target[1])).size !== fields.targets.length) {
      throw new MigrationRefused('verification target ids must be unique')
    }
    const sorted = [...fields.targets].sort(compareTargets)
    if (canonicalString(sorted) !== canonicalString(fields.targets)) {
      throw new MigrationRefused('verification targets must be sorted by engine and id')
    }
    awareTime(fields.requestedAt)
    this.requestId = fields.requestId
    this.projectId = fields.projectId
    this.modelVersion = fields.modelVersion
    this.mapVersion = fields.mapVersion
    this.mapFingerprint = fields.mapFingerprint
    this.group = fields.group
    this.sourceEngine = fields.sourceEngine
    this.sourceId = fields.sourceId
    this.targets = Object.freeze(fields.targets.map(([engine, id]) => Object.freeze([engine, id] as const)))
    this.requestedAt = fields.requestedAt
    this.requiresSignature = fields.requiresSignature
    Object.freeze(this)
  }

  asRecord(): Record<string, unknown> {
    return {
      protocol: REQUEST_PROTOCOL,
      request_id: this.requestId, project_id: this.projectId,
      model_version: this.modelVersion, map_version: this.mapVersion,
      map_fingerprint: this.mapFingerprint, group: this.group,
      source: { engine: this.sourceEngine, id: this.sourceId },
      targets: this.targets.map(([engine, id]) => ({ engine, id })),
      requested_at: this.requestedAt, requires_signature: this.requiresSignature,
    }
  }

  static fromRecord(value: unknown): VerificationRequest {
    exact(value, ['protocol', 'request_id', 'project_id', 'model_version', 'map_version',
      'map_fingerprint', 'group', 'source', 'targets', 'requested_at', 'requires_signature'], 'request')
    if (value['protocol'] !== REQUEST_PROTOCOL) {
      throw new MigrationRefused('unsupported verification request protocol')
    }
    const source = value['source']
    exact(source, ['engine', 'id'], 'source')
    if (!Array.isArray(value['targets'])) {
      throw new MigrationRefused('verification targets must contain exactly engine and id')
    }
    const targets = (value['targets'] as unknown[]).map((target): readonly [string, string] => {
      exact(target, ['engine', 'id'], 'target')
      return [target['engine'] as string, target['id'] as string]
    })
    return new VerificationRequest({
      requestId: value['request_id'] as string, projectId: value['project_id'] as string,
      modelVersion: value['model_version'] as string, mapVersion: value['map_version'] as number,
      mapFingerprint: value['map_fingerprint'] as string, group: value['group'] as string,
      sourceEngine: source['engine'] as string, sourceId: source['id'] as string, targets,
      requestedAt: value['requested_at'] as string, requiresSignature: value['requires_signature'] as boolean,
    })
  }

  checkSession(placement: PlacementMap, projectId: string | undefined, group: string): void {
    if (projectId !== this.projectId) {
      throw new MigrationRefused(
        'verification request names another project, or this session has no project_id. ' +
        "Configure the project id from the client's enrollment, not from the request.",
      )
    }
    if (group !== this.group) throw new MigrationRefused('verification request names another group')
    const expected = verificationRequest(placement, {
      group, projectId, requestId: this.requestId, requestedAt: this.requestedAt,
    })
    if (canonicalString(this.asRecord()) !== canonicalString(expected.asRecord())) {
      throw new MigrationRefused(
        "verification request does not match this session's model, map, source or targets; " +
        'no comparison was started',
      )
    }
  }

  checkTime(at: string): void {
    if (awareTime(at) < awareTime(this.requestedAt)) {
      throw new MigrationRefused(
        "verification predates its request; check the verifier's clock and run the " +
        'comparison for the current request',
      )
    }
  }
}

function compareTargets(left: readonly [string, string], right: readonly [string, string]): number {
  return compareCodePoints(left[0], right[0]) || compareCodePoints(left[1], right[1])
}

export function verificationRequest(placement: PlacementMap, options: {
  readonly group: string; readonly projectId: string; readonly requestId: string; readonly requestedAt: string
}): VerificationRequest {
  const fingerprint = fingerprintOf(placement)
  if (fingerprint === undefined) {
    throw new MigrationRefused('verification needs a loaded, canonically encodable placement map')
  }
  const spot = placementOf(placement, options.group)
  return new VerificationRequest({
    ...options, modelVersion: placement.modelVersion, mapVersion: placement.mapVersion,
    mapFingerprint: fingerprint, sourceEngine: spot.source.engine, sourceId: spot.source.id,
    targets: spot.alsoWrite.map((target): readonly [string, string] => [target.engine, target.id]).sort(compareTargets),
    requiresSignature: placement.signed,
  })
}
