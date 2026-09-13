/** Canonical signing and instruction execution must retain one physical identifier. */
import { generateKeyPairSync, sign } from 'node:crypto'
import { describe, expect, it } from 'vitest'
import { canonicalBytes, loadMap, MapError } from '../src/index.js'

type Document = ReturnType<typeof document>
function document(contract = 4) {
  return { contract, model_version: '0123456789abcdef', map_version: 1,
    ...(contract >= 4 ? { project_id: '1'.repeat(32) } : {}),
    groups: { Record: { ...(contract >= 4 ? { write_epoch: 1 } : {}), source: { id: 'source', engine: 'db',
      layout: { tables: { Record: 'records' }, columns: { Record: { id: 'bigint' } as Record<string, string> } } } } },
  }
}

describe('map Unicode identity', () => {
  it.each([1, 2, 3, 4])('refuses decomposed physical names in contract %s', contract => {
    const raw = document(contract)
    raw.groups.Record.source.layout.tables.Record = 'cafe\u0301'
    expect(() => loadMap(raw)).toThrow(/NFC/)
  })
  it.each(['engine', 'id', 'column-key', 'nested-list', 'root-key'])('checks payload text at %s', where => {
    const raw: Document & Record<string, unknown> = document()
    const source = raw.groups.Record.source
    if (where === 'engine' || where === 'id') source[where] = 'e\u0301'
    else if (where === 'column-key') source.layout.columns.Record['e\u0301'] = 'integer'
    else if (where === 'nested-list') raw['metadata'] = [{ nested: ['e\u0301'] }]
    else raw['e\u0301'] = 'metadata'
    expect(() => loadMap(raw)).toThrow(/NFC/)
  })
  it.each(['\ud800', '\udfff'])('refuses unpaired Unicode surrogates %#', value => {
    const raw = document(); raw.groups.Record.source.layout.tables.Record = value
    expect(() => loadMap(raw)).toThrow(/Unicode scalar/)
  })
  it('retains NFC and astral identifiers exactly', () => {
    const raw = document(); raw.groups.Record.source.layout.tables.Record = 'caf\u00e9_\u{1f6f0}'
    const parsed = loadMap(raw)
    expect(parsed.groups['Record']?.source.layout.tables['Record']).toBe('caf\u00e9_\u{1f6f0}')
    expect(parsed.fingerprint).toBeDefined()
  })
  it('keeps the unsigned signing-key annotation as a hint', () => {
    const raw: Document & Record<string, unknown> = document(), key = generateKeyPairSync('ed25519')
    raw['signature'] = { alg: 'ed25519', key_id: 'e\u0301', value: sign(null, canonicalBytes(raw), key.privateKey).toString('base64') }
    const bytes = key.publicKey.export({ format: 'der', type: 'spki' }).subarray(-32)
    expect(loadMap(raw, { publicKey: { trusted: bytes } }).verifiedWith).toBe('trusted')
  })
  it('does not turn encoder normalization into silent identifier rewriting', () => {
    const original = document(); original.groups.Record.source.layout.tables.Record = 'caf\u00e9'
    const decomposed = structuredClone(original); decomposed.groups.Record.source.layout.tables.Record = 'cafe\u0301'
    expect(canonicalBytes(original)).toEqual(canonicalBytes(decomposed))
    expect(loadMap(original).groups['Record']?.source.layout.tables['Record']).toBe('caf\u00e9')
    expect(() => loadMap(decomposed)).toThrow(MapError)
    expect(decomposed.groups.Record.source.layout.tables.Record).toBe('cafe\u0301')
  })
})
