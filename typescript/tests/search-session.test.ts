/** Session.search: the rule decides, the engine supplies candidates, the page is bounded. */
import { describe, expect, it } from 'vitest'
import {
  buildModel, entity, enumerateShapes, loadMap, MapError, matches, QueryRefused, Recorder,
  SearchRefused, Session, shapeId, T, windowRecord, type ReadPlan, type Row, type SearchPage,
} from '../src/index.js'
import { MemoryEngine } from '../src/testing/memory.js'

const NOTES: [number, string, string | null][] = [
  [1, 'Red apple', 'fresh fruit'],
  [2, 'Green pear', 'fresh'],
  [3, 'red APPLE pie', 'baked'],
  [4, 'Plum', null],
  [5, 'Apple juice', 'red label'],
  [6, 'Kiwi', 'green'],
  [7, 'Red apple', 'last row'],
]

/** An engine without a text index: rows in key order, a page at a time. */
class Scanned extends MemoryEngine {
  readonly pages: number[] = []
  protected ordered(table: string, plan: ReadPlan): Row[] {
    const names = plan.order.map(column => column.name)
    const key = (row: Row) => names.map(name => row[name] as bigint)
    const before = (a: bigint[], b: readonly unknown[]) => {
      for (const [index, value] of a.entries()) {
        const other = BigInt(b[index] as bigint)
        if (value !== other) return value > other
      }
      return false
    }
    return [...(this.tables[table] ?? [])]
      .sort((a, b) => Number((key(a)[0] as bigint) - (key(b)[0] as bigint)))
      .filter(row => plan.after === null || before(key(row), plan.after))
  }
  async selectRows(table: string, plan: ReadPlan): Promise<Row[]> {
    this.pages.push(plan.limit)
    return this.ordered(table, plan).slice(0, plan.limit + 1).map(row => ({ ...row }))
  }
}

/** An engine with a text index that is generous: it also supplies rows that do not match. */
class Indexed extends Scanned {
  readonly asked: [string[], string[], number][] = []
  constructor(readonly extra: ReadonlySet<bigint>) { super() }
  override async selectRows(): Promise<Row[]> {
    throw new Error('an engine with a text index is asked through it')
  }
  async searchRows(table: string, plan: ReadPlan, query: readonly string[], fields: readonly string[]): Promise<Row[]> {
    this.asked.push([[...query], [...fields], plan.limit])
    return this.ordered(table, plan)
      .filter(row => this.extra.has(row['id'] as bigint) || matches(row, query, fields))
      .slice(0, plan.limit + 1)
  }
}

async function fixture(source: Scanned = new Scanned(), routedScan = false) {
  const model = buildModel([
    entity('Note', { fields: { id: T.int64, title: T.string, body: T.nullable(T.string) }, key: ['id'] }),
    entity('Counter', { fields: { id: T.int64, hits: T.int64 }, key: ['id'] }),
  ])
  const shapes = new Map(enumerateShapes(model).map(shape => [`${shape.entity}/${shape.kind}`, shapeId(shape)]))
  const layout = {
    tables: { Note: 'note', Counter: 'counter' },
    columns: { Note: { id: 'bigint', title: 'text', body: 'text' }, Counter: { id: 'bigint', hits: 'bigint' } },
  }
  const group = {
    source: { id: 'source', engine: 'source', layout },
    derived: [{ id: 'copy', engine: 'copy', layout, lag_budget_ms: 30000 }],
  }
  const document: Record<string, unknown> = {
    contract: 3, model_version: model.version, map_version: 1, groups: { Note: group, Counter: group },
    ...(routedScan ? { routing: { [shapes.get('Note/full_scan')!]: 'copy' } } : {}),
  }
  const placement = loadMap(document, { model })
  for (const [id, title, body] of NOTES) {
    ;(source.tables['note'] ??= []).push({ id: BigInt(id), title, body })
  }
  const copy = new Scanned()
  const recorder = new Recorder(model.version)
  const session = await Session.open(model, placement, { source, copy }, { recorder })
  return { session, source, copy, recorder, model, document, shapes }
}

const ids = (page: SearchPage) => page.rows.map(row => Number(row['id']))

async function walk(session: Session, text: string, options: Record<string, unknown> = {}) {
  const found: number[] = []
  let pages = 0, examined = 0, after: Readonly<Row> | null = null
  for (;;) {
    const page: SearchPage = await session.search('Note', text, { ...options, after })
    found.push(...ids(page))
    pages += 1
    examined += page.examined
    if (page.nextAfter === null) return { found, pages, examined }
    after = page.nextAfter
  }
}

describe('Session.search', () => {
  it('finds every token in some chosen field, in key order', async () => {
    const { session } = await fixture()
    const page = await session.search('Note', 'apple RED')
    expect(ids(page)).toEqual([1, 3, 5, 7])
    expect(page.nextAfter).toBeNull()
    expect(page.examined).toBe(NOTES.length)
    expect(page.rows[0]).toEqual({ id: 1n, title: 'Red apple', body: 'fresh fruit' })
    expect(ids(await session.search('Note', 'apple red', { fields: ['title'] }))).toEqual([1, 3, 7])
    expect(ids(await session.search('Note', 'fresh', { fields: ['body'] }))).toEqual([1, 2])
  })

  it('carries the position of the last row of a full page', async () => {
    const { session } = await fixture()
    const first = await session.search('Note', 'apple', { limit: 2 })
    expect(ids(first)).toEqual([1, 3])
    expect(first.nextAfter).toEqual({ id: 3n })
    const second = await session.search('Note', 'apple', { limit: 2, after: first.nextAfter })
    expect(ids(second)).toEqual([5, 7])
    expect(second.nextAfter).toBeNull()
  })

  it('cuts a page short at the examined bound, and the walk still sees every row once', async () => {
    const { session, source } = await fixture()
    const page = await session.search('Note', 'apple', { maxExamined: 2 })
    expect(ids(page)).toEqual([1])
    expect(page.examined).toBe(2)
    expect(page.nextAfter).toEqual({ id: 2n })
    expect(source.pages.at(-1)).toBe(2)
    expect(await walk(session, 'apple', { maxExamined: 2 })).toEqual({ found: [1, 3, 5, 7], pages: 4, examined: 7 })
    expect((await walk(session, 'red', { limit: 1, maxExamined: 3 })).found).toEqual([1, 3, 5, 7])
  })

  it('asks a text index for what the page needs and drops the rows it should not have given', async () => {
    const index = new Indexed(new Set([2n, 4n, 6n]))
    const { session } = await fixture(index)
    const page = await session.search('Note', 'Apple', { limit: 3 })
    expect(ids(page)).toEqual([1, 3, 5])
    expect(page.nextAfter).toEqual({ id: 5n })
    expect(index.asked[0]).toEqual([['apple'], ['body', 'title'], 3])
    expect(page.examined).toBe(5)
    expect(index.asked.map(([, , limit]) => limit)).toEqual([3, 1, 1])
  })

  it('reads the source even where scans are routed to a copy', async () => {
    const { session, source, copy } = await fixture(new Scanned(), true)
    await session.scan('Note')
    expect(copy.pages.length).toBe(1)
    expect(source.pages.length).toBe(0)
    expect(ids(await session.search('Note', 'pear'))).toEqual([2])
    expect(source.pages.length).toBeGreaterThan(0)
  })

  it('does not follow a search route in a map loaded without its model', async () => {
    // Without the model a loader cannot tell a search's identifier from any other, so the entry
    // loads; the router is what keeps the search on the source.
    const { model, document, shapes, source, copy } = await fixture()
    const placement = loadMap({ ...document, routing: { [shapes.get('Note/search')!]: 'copy' } })
    const session = await Session.open(model, placement, { source, copy })
    expect(ids(await session.search('Note', 'pear'))).toEqual([2])
    expect(source.pages.length).toBeGreaterThan(0)
    expect(copy.pages).toEqual([])
  })

  it('refuses a map that routes a search, even to the source', async () => {
    const { model, document, shapes } = await fixture()
    for (const target of ['copy', 'source']) {
      expect(() => loadMap({ ...document, routing: { [shapes.get('Note/search')!]: target } }, { model }))
        .toThrow(MapError)
    }
    expect(shapes.has('Note/search')).toBe(true)
    expect(shapes.has('Counter/search')).toBe(false)
  })

  const refusals: [string, unknown, Record<string, unknown>, RegExp][] = [
    ['Note', '', {}, /no token/],
    ['Note', '--- ...', {}, /no token/],
    ['Note', 42, {}, /query is text/],
    ['Note', 'apple', { fields: [] }, /non-empty list/],
    ['Note', 'apple', { fields: 'title' }, /non-empty list/],
    ['Note', 'apple', { fields: [1] }, /non-empty list/],
    ['Note', 'apple', { fields: ['id', 'title'] }, /string only, not in \["id"\]/],
    ['Note', 'apple', { fields: ['missing'] }, /declares no field named/],
    ['Note', 'apple', { limit: 0 }, /limit/],
    ['Note', 'apple', { limit: 1001 }, /limit/],
    ['Note', 'apple', { limit: 1.5 }, /limit/],
    ['Note', 'apple', { maxExamined: 0 }, /maxExamined/],
    ['Note', 'apple', { maxExamined: 1_000_001 }, /maxExamined/],
    ['Counter', 'apple', {}, /no field of type string/],
  ]
  for (const [name, text, options, match] of refusals) {
    it(`refuses before any engine is asked: ${name} ${JSON.stringify(text)} ${JSON.stringify(options)}`, async () => {
      const { session, source, copy } = await fixture()
      const attempt = session.search(name, text as string, options)
      await expect(attempt).rejects.toThrow(SearchRefused)
      await expect(attempt).rejects.toThrow(match)
      await expect(attempt).rejects.toBeInstanceOf(QueryRefused)
      expect(source.pages).toEqual([])
      expect(copy.pages).toEqual([])
    })
  }

  it('reports what it examined and where, never the text', async () => {
    const { session, recorder, model, shapes } = await fixture()
    await session.search('Note', 'zebracrossing')
    await session.search('Note', 'apple', { fields: ['title'], limit: 1 })
    const window = recorder.roll()!
    const document = windowRecord(window, model) as { groups: Record<string, Record<string, unknown>> }
    const note = document.groups['Note']!
    const entry = (note['shapes'] as Record<string, unknown>[]).find(item => item['kind'] === 'search')!
    expect(entry['id']).toBe(shapes.get('Note/search'))
    expect(entry).toMatchObject({ calls: 2, rows: 1, fields: [], examined: NOTES.length + 1 })
    expect(entry['searched_on']).toEqual([
      { fields: ['body', 'title'], calls: 1 },
      { fields: ['title'], calls: 1 },
    ])
    expect(note['shape_mix']).toEqual({ search: 1 })
    const text = JSON.stringify(document)
    expect(text).not.toContain('zebracrossing')
    expect(text).not.toContain('apple')
  })
})
