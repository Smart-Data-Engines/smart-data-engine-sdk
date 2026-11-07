/**
 * A `json` value that is an array, written to PostgreSQL as JSON.
 *
 * `pg` sends a JavaScript array as a PostgreSQL array literal, which a `jsonb` column refuses. Until
 * 7 November 2026 an entity whose `json` field held an array could not be saved on PostgreSQL - by
 * `insert`, `insertMany` or `copyIn` - while an object could, and the reference implementation
 * wrote both. Each way a row is written is checked on its own, because each sends its parameters
 * itself. Skipped rather than faked without a server, as every live test here.
 */

import { randomUUID } from 'node:crypto'

import pg from 'pg'
import { afterAll, beforeAll, expect, it } from 'vitest'

import { PostgresEngine } from '../src/engines/postgres.js'

const DSN = process.env['SDE_POSTGRES_DSN']
const enabled = DSN !== undefined
const table = `json_arrays_${randomUUID().replaceAll('-', '').slice(0, 12)}`
let engine: PostgresEngine
let raw: pg.Client

beforeAll(async () => {
  if (!enabled) return
  raw = new pg.Client({ connectionString: DSN })
  await raw.connect()
  await raw.query(`CREATE TABLE ${table} (id bigint PRIMARY KEY, doc jsonb)`)
  engine = new PostgresEngine(DSN as string)
  await engine.connect()
})

afterAll(async () => {
  if (!enabled) return
  await engine.close()
  await raw.query(`DROP TABLE IF EXISTS ${table}`)
  await raw.end()
})

const DOCUMENTS: readonly unknown[] = [
  [1, 'x'],
  [],
  [[1, [2]], { a: [3] }],
  [null, true, 0.5, 'zażółć'],
  { a: [1, 2] }, // an object, written as before: the control
]

it.skipIf(!enabled)('insert, insertMany and copyIn each write an array as JSON', async () => {
  const written: [bigint, unknown][] = []
  let id = 0n
  for (const doc of DOCUMENTS) {
    await engine.insert(table, { id: ++id, doc })
    written.push([id, doc])
    await engine.insertMany(table, [{ id: ++id, doc }])
    written.push([id, doc])
    await engine.copyIn(table, [{ id: ++id, doc }])
    written.push([id, doc])
  }
  for (const [key, doc] of written) {
    expect((await engine.get(table, { id: key }))?.['doc'], `id ${key}`).toEqual(doc)
  }
  // As the server stores it: a JSON array, not text and not a PostgreSQL array.
  const kinds = await raw.query(`SELECT DISTINCT jsonb_typeof(doc) AS kind FROM ${table} ORDER BY 1`)
  expect(kinds.rows.map((row: { kind: string }) => row.kind)).toEqual(['array', 'object'])
})
