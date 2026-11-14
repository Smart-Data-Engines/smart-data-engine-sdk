/**
 * A login ClickHouse would answer short, saying nothing, gets an error through this adapter.
 *
 * Each case first shows its premise on the server it runs against - the same statement, sent as it
 * is by this login with nothing of the adapter's, comes back short and with no error - and then
 * what the adapter gets. The reference's `test_clickhouse_whole_answers_live.py` holds the same.
 */
import { randomUUID } from 'node:crypto'
import { describe, expect, it } from 'vitest'
import { ClickHouseEngine, WHOLE_ANSWERS } from '../src/engines/clickhouse.js'
import { EngineError } from '../src/errors.js'

const dsn = process.env['SDE_CLICKHOUSE_DSN']
const root = dsn ? new URL(dsn) : undefined
const plain = root !== undefined && !['clickhouses:', 'https:'].includes(root.protocol)

/** The table's rows. Read in blocks of 100, which is what makes a cap bite at a block's edge. */
const ROWS = 20_000
const CAP = 5000
/** A login TypeScript's own pinned output formats do not get refused for, when it may change nothing. */
const FORMATS = 'output_format_json_quote_64bit_integers = 1, output_format_json_quote_decimals = 1, ' +
  'output_format_decimal_trailing_zeros = 1, output_format_json_quote_denormals = 1'

async function asked(statement: string, user: string, password: string): Promise<string> {
  const response = await fetch(`http://${root!.host}/?database=${encodeURIComponent(root!.pathname.slice(1))}`, {
    method: 'POST',
    body: statement,
    headers: { Authorization: `Basic ${Buffer.from(`${user}:${password}`).toString('base64')}` },
  })
  const text = await response.text()
  if (!response.ok) throw new Error(text)
  return text
}
const admin = (statement: string): Promise<string> =>
  asked(statement, decodeURIComponent(root!.username), decodeURIComponent(root!.password))

interface Login { readonly table: string, readonly dsn: string, asItIs(statement: string): Promise<string[]> }

/** A table of `ROWS` rows and a login whose profile is `settings`, for the length of `body`. */
async function withLogin(settings: string, body: (login: Login) => Promise<void>): Promise<void> {
  const name = 'sde_whole_' + randomUUID().replaceAll('-', '').slice(0, 16)
  const user = name + '_login', password = randomUUID().replaceAll('-', '')
  try {
    await admin(`CREATE TABLE ${name} (id Int64, value Int32) ENGINE = ReplacingMergeTree ORDER BY (id)`)
    await admin(`INSERT INTO ${name} SELECT number, number % 7 FROM numbers(${ROWS})`)
    await admin(`CREATE USER ${user} IDENTIFIED WITH sha256_password BY '${password}' ` +
      `SETTINGS max_block_size = 100${settings ? ', ' + settings : ''}`)
    await admin(`GRANT SELECT ON ${name} TO ${user}`)
    const mine = new URL(dsn!)
    mine.username = user
    mine.password = password
    await body({
      table: name,
      dsn: mine.toString(),
      asItIs: async statement => (await asked(statement, user, password)).split('\n').filter(line => line !== ''),
    })
  } finally {
    await admin(`DROP USER IF EXISTS ${user}`)
    await admin(`DROP TABLE IF EXISTS ${name}`)
  }
}

const stream = (table: string): string => `SELECT id FROM ${table} FINAL ORDER BY (id) FORMAT TSV`
const ids = async (engine: ClickHouseEngine, table: string): Promise<number> =>
  (await engine.keyRange(table, ['id'])).length

describe.skipIf(!plain)('a ClickHouse login whose answers the server would cut', () => {
  it('is sent the modes this server has, and no other', async () => {
    const listed = (await admin("SELECT name FROM system.settings WHERE name LIKE '%overflow_mode%' FORMAT TSV"))
      .split('\n').filter(line => line !== '').sort()
    expect(listed).toEqual(WHOLE_ANSWERS.map(([name]) => name).filter(name => name.includes('overflow_mode')).sort())
  })

  it('reads as it did where nobody configured the login', async () => {
    await withLogin('', async ({ table, dsn, asItIs }) => {
      expect(await asItIs(stream(table))).toHaveLength(ROWS)
      const engine = new ClickHouseEngine(dsn)
      await engine.connect()
      try {
        expect(await ids(engine, table)).toBe(ROWS)
        expect(await engine.count(table)).toBe(ROWS)
      } finally { await engine.close() }
    })
  })

  it('gets an error and not the first rows of a result capped with break', async () => {
    await withLogin(`max_result_rows = ${CAP}, result_overflow_mode = 'break'`, async ({ table, dsn, asItIs }) => {
      const short = (await asItIs(stream(table))).length
      expect(short, 'the premise: this server cuts the answer and says nothing').toBeGreaterThan(0)
      expect(short).toBeLessThan(ROWS)
      const engine = new ClickHouseEngine(dsn)
      await engine.connect()
      try {
        await expect(ids(engine, table)).rejects.toThrow(/Limit for result exceeded/)
        // Under the cap nothing changes: a chunk, a row by its key, a count.
        expect(await engine.keyRange(table, ['id'], { limit: 100 })).toHaveLength(100)
        expect(await engine.get(table, { id: 7 })).toEqual({ id: 7n, value: 0 })
        expect(await engine.count(table)).toBe(ROWS)
      } finally { await engine.close() }
    })
  })

  it('gets the error of a limit reached under the server\'s own mode, however the server sends it', async () => {
    // No overflow mode of anyone's choosing here: `throw` is the server's own. 24.8 has begun the
    // answer when the limit is reached, so the error comes inside it, under HTTP 200 and after
    // its first rows; 26.9 answers 500, or cuts the body it had begun. Read for its rows alone,
    // the first of those was the start of the table returned as the table.
    await withLogin(`max_result_rows = ${CAP}`, async ({ table, dsn, asItIs }) => {
      const raw = await asItIs(`SELECT id FROM ${table} FINAL ORDER BY (id) FORMAT JSON`)
        .then(lines => lines.join('\n'), () => undefined)
      if (raw !== undefined) {
        expect(raw, 'rows, and the error after them, in one answer').toMatch(/DB::Exception/)
        expect(raw).toMatch(/"id": "0"/)
      }
      const engine = new ClickHouseEngine(dsn)
      await engine.connect()
      try {
        await expect(ids(engine, table)).rejects.toThrow(/Limit for result exceeded/)
        expect(await engine.count(table)).toBe(ROWS)
      } finally { await engine.close() }
    })
  })

  it.each([
    ['a constrained mode', `max_result_rows = ${CAP}, result_overflow_mode = 'break' CONST`, 'result_overflow_mode'],
    ['a read-only login', `max_result_rows = ${CAP}, result_overflow_mode = 'break', readonly = 1`, 'result_overflow_mode'],
    ['a constrained limit', 'limit = 10 CONST', 'limit'],
  ])('refuses %s by the setting\'s name', async (_label, settings, refused) => {
    await withLogin(settings, async ({ table, dsn, asItIs }) => {
      const short = (await asItIs(stream(table))).length
      expect(short, 'the premise').toBeGreaterThan(0)
      expect(short).toBeLessThan(ROWS)
      const error = await new ClickHouseEngine(dsn).connect().then(() => undefined, (caught: unknown) => caught)
      expect(error).toBeInstanceOf(EngineError)
      expect((error as Error).message).toContain(`this ClickHouse login's ${refused} is not `)
      expect((error as Error).message).not.toContain('DB::Exception')
    })
  })

  it('does not refuse a read-only login whose values are the ones it sends', async () => {
    // The control of the refusals: a value equal to the login's own is accepted from a login that
    // may change nothing. Its profile holds the four output formats this library also pins.
    await withLogin(`${FORMATS}, readonly = 1`, async ({ table, dsn }) => {
      const engine = new ClickHouseEngine(dsn)
      await engine.connect()
      try {
        expect(await ids(engine, table)).toBe(ROWS)
        expect(await engine.count(table)).toBe(ROWS)
      } finally { await engine.close() }
    })
  })

  it.each([
    ['a limit', 'limit = 10'],
    ['an offset', `offset = ${ROWS - 5}`],
    ['a filter on every result', "additional_result_filter = 'id < 10'"],
  ])('is not shortened by a profile with %s', async (_label, settings) => {
    await withLogin(settings, async ({ table, dsn, asItIs }) => {
      const short = (await asItIs(stream(table))).length
      expect(short, 'the premise: this server shortens the answer by itself').toBeGreaterThan(0)
      expect(short).toBeLessThan(ROWS)
      const engine = new ClickHouseEngine(dsn)
      await engine.connect()
      try {
        expect(await ids(engine, table)).toBe(ROWS)
        expect(await engine.count(table)).toBe(ROWS)
      } finally { await engine.close() }
    })
  })

  it('gets an error and not an empty answer from a statement cut off by time', async () => {
    await withLogin("max_execution_time = 0.000001, timeout_overflow_mode = 'break'", async ({ table, dsn, asItIs }) => {
      expect(await asItIs(stream(table))).toEqual([])
      expect(await asItIs(`SELECT count() FROM ${table} FINAL FORMAT TSV`)).toEqual([])
      const engine = new ClickHouseEngine(dsn)
      await expect(engine.connect().then(() => engine.count(table))).rejects.toBeInstanceOf(EngineError)
    })
  })
})
