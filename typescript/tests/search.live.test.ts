/** Search against the engines: candidates in key order, and the rule - not the engine - decides. */
import { expect, it } from 'vitest'
import { buildModel, colocationGroups, entity, hashIdentifiers, loadMap, Recorder, Session, T,
  windowRecord, type Row, type SearchPage } from '../src/index.js'
import { withRoles } from './_runtime-roles.js'
import type { Dialect } from './_generation-engines.js'

// The cases an engine's own text handling gets wrong: a decomposed accent, the Kelvin sign, the
// dotted capital I and the sharp s. The same rows as the reference's live test.
const ROWS: Row[] = [
  { id: 1n, title: 'Red apple', body: 'fresh fruit' },
  { id: 2n, title: 'e\u0301clair', body: null },
  { id: 3n, title: '\u00e9CLAIR', body: 'a 5\u212a run' },
  { id: 4n, title: '\u0130stanbul', body: 'Stra\u00dfe' },
  { id: 5n, title: 'red_apple-pie', body: 'x' },
  { id: 6n, title: 'Plum', body: 'red' },
  { id: 7n, title: '\u65e5\u672c\u8a9e', body: 'apple' },
]

async function fixture(
  dialect: Dialect,
  body: (session: Session, recorder: Recorder, model: ReturnType<typeof buildModel>) => Promise<void>,
  hashed = false,
) {
  await withRoles(dialect, async role => {
    let model = buildModel([entity('Note', { fields: {
      id: T.int64, title: T.string, body: T.nullable(T.string),
    }, key: ['id'] })])
    let names
    if (hashed) ({ model, names } = hashIdentifiers(model, Buffer.alloc(32, 's')))
    const group = colocationGroups(model)[0]!, spec = model.entities[0]!
    const types: Record<string, string> = dialect === 'postgres'
      ? { int64: 'bigint', string: 'text' }
      : { int64: 'Int64', string: 'String' }
    const columns = Object.fromEntries(spec.fields.map(field => [field.name,
      dialect === 'clickhouse' && field.nullable ? 'Nullable(' + types[field.type]! + ')' : types[field.type]!]))
    const placement = loadMap({ contract: 3, model_version: model.version, map_version: 1, groups: {
      [group.name]: { source: { id: 'source', engine: 'db', layout: {
        tables: { [spec.name]: 'search_notes' }, columns: { [spec.name]: columns },
      } } },
    } }, { model })
    await role.operator.ensureSchema(placement.groups[group.name]!.source.layout, { keys: { [spec.name]: spec.key } })
    await role.grant('search_notes')
    const recorder = new Recorder(model.version)
    const session = await Session.open(model, placement, { db: role.runtime }, { recorder, ...(names ? { names } : {}) })
    await session.saveMany('Note', [...ROWS].reverse())
    recorder.roll()
    await body(session, recorder, model)
  })
}

const ids = (page: SearchPage) => page.rows.map(row => Number(row['id']))

for (const dialect of ['postgres', 'clickhouse'] as const) {
  const enabled = process.env[dialect === 'postgres' ? 'SDE_POSTGRES_DSN' : 'SDE_CLICKHOUSE_DSN'] !== undefined

  for (const hashed of [false, true]) it.skipIf(!enabled)(`${dialect} finds what an engine would fold differently, hashing=${hashed}`, async () => {
    await fixture(dialect, async session => {
      expect(ids(await session.search('Note', 'eclair \u00e9clair'))).toEqual([])
      expect(ids(await session.search('Note', '\u00c9CLAIR'))).toEqual([2, 3])
      expect(ids(await session.search('Note', '5k'))).toEqual([3])
      expect(ids(await session.search('Note', 'istanbul'))).toEqual([4])
      expect(ids(await session.search('Note', 'strasse'))).toEqual([])
      expect(ids(await session.search('Note', 'stra\u00dfe'))).toEqual([4])
      expect(ids(await session.search('Note', 'red apple'))).toEqual([1, 5])
      expect(ids(await session.search('Note', 'red', { fields: ['body'] }))).toEqual([6])
      expect(ids(await session.search('Note', '\u65e5\u672c'))).toEqual([])
      const page = await session.search('Note', 'apple')
      expect(ids(page)).toEqual([1, 5, 7])
      expect(page.rows[0]).toEqual({ id: 1n, title: 'Red apple', body: 'fresh fruit' })
      expect(page.examined).toBe(ROWS.length)
      expect(page.nextAfter).toBeNull()
    }, hashed)
  }, 30000)

  it.skipIf(!enabled)(`${dialect} walks the table once whatever bounds the pages`, async () => {
    await fixture(dialect, async session => {
      for (const [limit, bound] of [[1, 2], [2, 3], [1, 1], [100, 7]] as const) {
        const found: number[] = []
        let examined = 0, after: Readonly<Row> | null = null
        for (let attempt = 0; attempt < 20; attempt++) {
          const page: SearchPage = await session.search('Note', 'apple', { limit, maxExamined: bound, after })
          expect(page.rows.length).toBeLessThanOrEqual(limit)
          expect(page.examined).toBeLessThanOrEqual(bound)
          found.push(...ids(page))
          examined += page.examined
          if (page.nextAfter === null) break
          after = page.nextAfter
        }
        expect(found).toEqual([1, 5, 7])
        expect(examined).toBe(ROWS.length)
      }
    })
  }, 30000)

  it.skipIf(!enabled)(`${dialect} reports what the searches examined, never the text`, async () => {
    await fixture(dialect, async (session, recorder, model) => {
      await session.search('Note', 'apple', { fields: ['title'] })
      await session.search('Note', 'zzzyzzx')
      const document = windowRecord(recorder.roll()!, model) as { groups: Record<string, Record<string, unknown>> }
      const shapes = Object.values(document.groups)[0]!['shapes'] as Record<string, unknown>[]
      const entry = shapes.find(item => item['kind'] === 'search')!
      expect(entry['examined']).toBe(2 * ROWS.length)
      expect(entry['searched_on']).toEqual([
        { fields: ['body', 'title'], calls: 1 },
        { fields: ['title'], calls: 1 },
      ])
      const text = JSON.stringify(document)
      expect(text).not.toContain('zzzyzzx')
      expect(text).not.toContain('apple')
    })
  }, 30000)
}
