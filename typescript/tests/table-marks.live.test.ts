/**
 * A session reads the marks of its map's tables when it opens (format contract §7r), on live
 * engines - the cases of the reference's `test_table_marks_live.py`.
 *
 * Two independent reviews of copy ownership wrote over another group's rows through a session that
 * read no mark. A session of a map with write generations now refuses, before it writes: a copy's
 * table without its own mark, a source's table with a copy's mark, and a copy's or a source's table
 * that another name of the map reaches. `prepareSchema` refuses the same. The control: a map
 * provisioned as `prepareSchema` provisions it opens.
 */
import { describe, expect, it } from 'vitest'
import { buildModel, entity, loadMap, prepareSchema, Session, T } from '../src/index.js'
import { checkTableMarks } from '../src/table-marks.js'
import { fixture as engineFixture, type Database, type Dialect } from './_generation-engines.js'

const LIVE = process.env['SDE_POSTGRES_DSN'] !== undefined && process.env['SDE_CLICKHOUSE_DSN'] !== undefined
const PROJECT = '3'.repeat(32)
const SHAPE: Record<Dialect, Record<string, string>> = {
  postgres: { id: 'bigint', value: 'integer' },
  clickhouse: { id: 'Int64', value: 'Int32' },
}
type Placed = Record<string, [[string, string], [string, string][] | null]>

function model() {
  return buildModel([
    entity('Account', { fields: { id: T.int64, value: T.int32 }, key: ['id'] }),
    entity('Event', { fields: { id: T.int64, value: T.int32 }, key: ['id'] }),
  ])
}

function map(built: ReturnType<typeof model>, dialect: Dialect, groups: Placed, version = 1, epoch = 1) {
  const material = (engine: string, id: string, entityName: string, table: string) => ({
    engine, id, layout: { tables: { [entityName]: table }, columns: { [entityName]: SHAPE[dialect] } },
  })
  const body: Record<string, unknown> = {
    contract: 4, project_id: PROJECT, model_version: built.version, map_version: version, groups: {},
  }
  for (const [group, [source, copies]] of Object.entries(groups)) {
    const placed: Record<string, unknown> = {
      write_epoch: epoch, source: material(source[0], `${group}@source`, group, source[1]),
    }
    if (copies !== null) {
      placed['derived'] = copies.map(([engine, table], n) => ({
        ...material(engine, `${group}@copy${n}`, group, table), lag_budget_ms: 60000,
      }))
      placed['also_write'] = copies.map((_, n) => `${group}@copy${n}`)
    }
    ;(body['groups'] as Record<string, unknown>)[group] = placed
  }
  return loadMap(body, { model: built })
}

async function unmark(engine: Database, table: string): Promise<void> {
  const raw = engine as unknown as { run?: (s: string) => Promise<unknown>; command?: (s: string) => Promise<void> }
  if (engine.dialect === 'postgres') await raw.run!(`COMMENT ON TABLE "${table}" IS NULL`)
  else await raw.command!(`ALTER TABLE \`${table}\` MODIFY COMMENT ''`)
}

async function definition(engine: Database, table: string): Promise<unknown> {
  // What a statement of prepareSchema could change on the table: its columns and constraints.
  const raw = engine as unknown as {
    run?: (s: string, v?: readonly unknown[]) => Promise<{ rows: unknown[] }>
    query?: (s: string) => Promise<unknown[]>
  }
  if (engine.dialect === 'postgres') {
    return (await raw.run!(
      'SELECT conname, pg_get_constraintdef(oid) AS def FROM pg_constraint WHERE conrelid = to_regclass($1) ORDER BY conname',
      [`"${table}"`],
    )).rows
  }
  return raw.query!(`SHOW CREATE TABLE \`${table}\``)
}

describe.skipIf(!LIVE)("a session reads the marks of its map's tables", () => {
  it.each(['postgres', 'clickhouse'] as const)('a map provisioned as prepareSchema provisions it opens (%s)', async (dialect) => {
    await engineFixture(dialect, async (engine) => {
      const built = model()
      const engines = { e: engine }
      const placement = map(built, dialect, { Account: [['e', 'acct'], [['e', 'acct_copy']]], Event: [['e', 'ev'], null] })
      await prepareSchema(built, placement, engines, { projectId: PROJECT })
      await Session.open(built, placement, engines, { projectId: PROJECT })
    })
  })

  it.each(['postgres', 'clickhouse'] as const)('a session refuses a copy without its mark (%s)', async (dialect) => {
    await engineFixture(dialect, async (engine) => {
      const built = model()
      const engines = { e: engine }
      const placement = map(built, dialect, { Account: [['e', 'acct'], [['e', 'acct_copy']]], Event: [['e', 'ev'], null] })
      await prepareSchema(built, placement, engines, { projectId: PROJECT })
      await unmark(engine, 'acct_copy')
      await expect(Session.open(built, placement, engines, { projectId: PROJECT }))
        .rejects.toThrow(/is not one this library created for it/)
    })
  })

  it.each(['postgres', 'clickhouse'] as const)("a marked copy named as another group's source is refused (%s)", async (dialect) => {
    await engineFixture(dialect, async (engine) => {
      const built = model()
      const engines = { e: engine }
      const first = map(built, dialect, { Account: [['e', 'acct'], [['e', 'shared']]], Event: [['e', 'ev'], null] })
      await prepareSchema(built, first, engines, { projectId: PROJECT })
      const second = map(built, dialect, { Account: [['e', 'acct'], null], Event: [['e', 'shared'], null] }, 2)
      await expect(prepareSchema(built, second, engines, { projectId: PROJECT })).rejects.toThrow(/carries a copy's mark/)
      await expect(Session.open(built, second, engines, { projectId: PROJECT })).rejects.toThrow(/carries a copy's mark/)
    })
  })

  it.each(['postgres', 'clickhouse'] as const)('two engine names bound to one database are refused (%s)', async (dialect) => {
    await engineFixture(dialect, async (engine) => {
      const built = model()
      const engines = { a: engine, b: engine }
      const placement = map(built, dialect, { Account: [['a', 'acct'], [['b', 'shared']]], Event: [['a', 'shared'], null] })
      await expect(prepareSchema(built, placement, engines, { projectId: PROJECT })).rejects.toThrow(/carries a copy's mark/)
      await expect(Session.open(built, placement, engines, { projectId: PROJECT })).rejects.toThrow(/carries a copy's mark/)
    })
  })

  it.each([
    ['postgres', 'b'], ['clickhouse', 'b'], ['postgres', 'a'], ['clickhouse', 'a'],
  ] as const)("two groups' sources reaching one table are refused (%s, Event's on %s)", async (dialect, other) => {
    // A session would write both groups' rows into the table, and an erasure of one group's
    // subject would delete the other's: refused after prepareSchema's last statement, and before a
    // session's first write.
    await engineFixture(dialect, async (engine) => {
      const built = model()
      const engines = { a: engine, b: engine }
      const placement = map(built, dialect, { Account: [['a', 'shared'], null], Event: [[other, 'shared'], null] })
      const refused = /a source's table holds one entity of its group alone, and two names of the map reach this one/
      await expect(prepareSchema(built, placement, engines, { projectId: PROJECT })).rejects.toThrow(refused)
      await expect(Session.open(built, placement, engines, { projectId: PROJECT })).rejects.toThrow(refused)
    })
  })

  it.each(['postgres', 'clickhouse'] as const)('a copy another engine name reaches is refused (%s)', async (dialect) => {
    await engineFixture(dialect, async (engine) => {
      const built = model()
      const engines = { a: engine, b: engine }
      const placement = map(built, dialect, {
        Account: [['a', 'acct'], null], Event: [['a', 'ev'], [['a', 'ev_copy'], ['b', 'ev_copy']]],
      })
      await expect(prepareSchema(built, placement, engines, { projectId: PROJECT }))
        .rejects.toThrow(/two names of the map reach this one/)
      await expect(Session.open(built, placement, engines, { projectId: PROJECT }))
        .rejects.toThrow(/two names of the map reach this one/)
    })
  })
  it.each(['postgres', 'clickhouse'] as const)("prepareSchema refuses a marked copy named as a source before its first statement (%s)", async (dialect) => {
    // The next map names Account's marked copy as Event's source, at a new generation: refused before
    // Event's fence is written on the table.
    await engineFixture(dialect, async (engine) => {
      const built = model()
      const engines = { e: engine }
      const first = map(built, dialect, { Account: [['e', 'acct'], [['e', 'shared']]], Event: [['e', 'ev'], null] })
      await prepareSchema(built, first, engines, { projectId: PROJECT })
      const before = await definition(engine, 'shared')
      const second = map(built, dialect, { Account: [['e', 'acct'], null], Event: [['e', 'shared'], null] }, 2, 2)
      await expect(prepareSchema(built, second, engines, { projectId: PROJECT })).rejects.toThrow(/carries a copy's mark/)
      expect(await definition(engine, 'shared')).toEqual(before)
    })
  })

  it.each(['postgres', 'clickhouse'] as const)("a session's ensureSchema reads the marks again (%s)", async (dialect) => {
    await engineFixture(dialect, async (engine) => {
      const built = model()
      const engines = { e: engine }
      const placement = map(built, dialect, { Account: [['e', 'acct'], [['e', 'acct_copy']]], Event: [['e', 'ev'], null] })
      await prepareSchema(built, placement, engines, { projectId: PROJECT })
      const session = await Session.open(built, placement, engines, { projectId: PROJECT })
      await unmark(engine, 'acct_copy')
      await expect(session.ensureSchema()).rejects.toThrow(/is not one this library created for it/)
    })
  })

  it.each(['postgres', 'clickhouse'] as const)('two entities of one group in one source table are refused (%s)', async (dialect) => {
    // Event's writes would replace Account's rows, and an erasure of one entity's subject would delete the other's.
    await engineFixture(dialect, async (engine) => {
      const built = buildModel([
        entity('Account', { fields: { id: T.int64, value: T.int32 }, key: ['id'], atomicWith: ['Event'] }),
        entity('Event', { fields: { id: T.int64, value: T.int32 }, key: ['id'] }),
      ])
      const placement = loadMap({
        contract: 4, project_id: PROJECT, model_version: built.version, map_version: 1,
        groups: { Account: { write_epoch: 1, source: { engine: 'e', id: 'Account@source', layout: {
          tables: { Account: 'shared', Event: 'shared' }, columns: { Account: SHAPE[dialect], Event: SHAPE[dialect] },
        } } } },
      }, { model: built })
      const engines = { e: engine }
      const refused = /a source's table holds one entity of its group alone/
      await expect(prepareSchema(built, placement, engines, { projectId: PROJECT })).rejects.toThrow(refused)
      await expect(Session.open(built, placement, engines, { projectId: PROJECT })).rejects.toThrow(refused)
    })
  })
})

it("a copy its adapter gives no answer for is not there", async () => {
  // An adapter of one's own whose tableFacts leaves a copy's name out: the name reaches no table.
  const built = model()
  const placement = map(built, 'postgres', { Account: [['e', 'acct'], [['e', 'acct_copy']]], Event: [['e', 'ev'], null] })
  const silent = { tableFacts: async () => ({}) }
  await expect(checkTableMarks(placement, { e: silent }, PROJECT)).rejects.toThrow(/is not there/)
})
