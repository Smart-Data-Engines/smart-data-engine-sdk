/** The TypeScript Elasticsearch adapter without a server: its profile, its JSON and its refusals. */
import { inspect } from 'node:util'

import { describe, expect, it } from 'vitest'

import { EngineError, searchEngine, SearchRefused } from '../src/index.js'
import { decimalBound, stored } from '../src/elasticsearch-document.js'
import { ElasticsearchEngine, parseDsn, whole, writeErrorOf } from '../src/engines/elasticsearch.js'
import { OrderbookEngine } from '../src/engines/orderbook.js'
import { parseExactJson, stringifyExactJson } from '../src/engines/_json.js'

describe('the connection profile', () => {
  it('reads what is written and never prints a secret', () => {
    const basic = parseDsn('elasticsearch+https://app:p%40ss:w@search.example.com/')
    expect([basic.host, basic.port, basic.tls]).toEqual(['search.example.com', 9200, true])
    expect(basic.authorization).toBe('Basic YXBwOnBAc3M6dw==')
    const keyed = parseDsn('elasticsearch+https://[::1]:9243?api_key=Zm9vOmJhcg==&connect_timeout=2.5')
    expect([keyed.host, keyed.port, keyed.authorization]).toEqual(['::1', 9243, 'ApiKey Zm9vOmJhcg=='])
    expect([keyed.connectTimeoutMs, keyed.sendReceiveTimeoutMs]).toEqual([2500, 30000])
    expect(parseDsn('elasticsearch+http://elastic:x@127.0.0.1:9200').tls).toBe(false)
  })

  const refused: [string, RegExp][] = [
    ['https://host:9200', /starts/],
    ['elasticsearch+http://elastic:x@search.example.com:9200', /loopback/],
    ['elasticsearch+http://10.0.0.5:9200', /loopback/],
    ['elasticsearch+https://host/?verify=false', /disabling/],
    ['elasticsearch+https://host/?unknown=1', /unknown/],
    ['elasticsearch+https://host/?connect_timeout=1&connect_timeout=2', /twice/],
    ['elasticsearch+https://host/?connect_timeout=0', /between/],
    ['elasticsearch+https://host/?ca_cert=relative.pem', /absolute/],
    ['elasticsearch+https://host/?ca_cert=%2Fno%2Fsuch%2Ffile.pem', /CA file/],
    ['elasticsearch+https://u:p@host/?api_key=Zm9v', /not both/],
    ['elasticsearch+https://host/?api_key=not%20base64!', /encoded key/],
    ['elasticsearch+https://host/index', /no path/],
    ['elasticsearch+http://127.0.0.1/?ca_cert=%2Fetc%2Fhosts', /belong to/],
    // As Python reads them (second review, finding 7): a password without a user was dropped and
    // the connection made without it.
    ['elasticsearch+http://:secret@127.0.0.1:9200', /nonempty/],
    ['elasticsearch+http://@127.0.0.1:9200', /nonempty/],
    ['elasticsearch+https://:secret@host/?api_key=Zm9v', /not both/],
    ['elasticsearch+https://host/?api_key=Zm9vY', /encoded key/],
    ['elasticsearch+https://us%zzer:pw@host/', /percent-encoded/],
    ['elasticsearch+https://user:%ff@host/', /percent-encoded/],
    ['elasticsearch+https://host/?connect_timeout=1e1', /between/],
    ['elasticsearch+https://[127.0.0.1]:9200', /URL/],
  ]
  for (const [dsn, match] of refused) {
    it(`refuses ${dsn}`, () => {
      expect(() => new ElasticsearchEngine(dsn)).toThrow(EngineError)
      expect(() => new ElasticsearchEngine(dsn)).toThrow(match)
    })
  }
})

describe('the credentials', () => {
  it('are printed by nothing that inspects the engine or its profile', () => {
    const dsn = 'elasticsearch+http://elastic:s3cret-pass@127.0.0.1:9200'
    const encoded = Buffer.from('elastic:s3cret-pass').toString('base64')
    const engine = new ElasticsearchEngine(dsn)
    const profile = parseDsn(dsn)
    expect(profile.authorization).toBe(`Basic ${encoded}`)
    const printed = [
      inspect(engine, { depth: 8 }), inspect(engine, { depth: 8, customInspect: false }),
      JSON.stringify(engine), String(engine), `${engine}`,
      inspect(profile, { depth: 8 }), JSON.stringify(profile), JSON.stringify({ ...profile }),
    ]
    for (const text of printed) {
      expect(text).not.toContain('s3cret')
      expect(text).not.toContain(encoded)
    }
    expect(String(engine)).toBe('ElasticsearchEngine(http 127.0.0.1:9200, login=Basic)')
    expect(inspect(engine)).toBe(String(engine))
    expect(JSON.stringify(engine)).toBe(JSON.stringify(String(engine)))
  })
})

describe('values and predicates', () => {
  it('keeps a float32 as the float32 the engine indexes', () => {
    expect(stored('float', 0.99999999)).toBe(1)
    expect(stored('float', 0.1)).toBe(0.10000000149011612)
    expect(stored('double', 0.1)).toBe(0.1)
    expect(() => stored('float', 1e39)).toThrow(/outside float32/)
  })

  it('refuses a lone surrogate, which UTF-8 cannot carry', () => {
    expect(() => stored('keyword', 'a\ud800b')).toThrow(/lone surrogate/)
    expect(() => stored('keyword', '\udc00')).toThrow(/lone surrogate/)
    expect(stored('keyword', '\ud83d\ude00')).toBe('\ud83d\ude00')
  })

  // The same table as Python's `test_a_decimal_predicate_is_a_question_about_the_column`.
  const bounds: [string, 'eq' | 'ge' | 'lt', bigint | null][] = [
    ['0.005', 'ge', 1n], ['0.005', 'lt', 1n], ['-0.005', 'ge', 0n], ['0.01', 'ge', 1n],
    ['0.005', 'eq', null], ['0.01', 'eq', 1n], ['1000000000000000000000000000000', 'ge', 10n ** 18n],
    ['-1000000000000000000000000000000', 'lt', -(10n ** 18n)], ['1000000000000000000000000000000', 'eq', null],
    ['-1.234', 'ge', -123n], ['1.234', 'lt', 124n], ['9999999999999999.995', 'ge', 10n ** 18n],
    ['10000000000000000', 'eq', null],
  ]
  for (const [value, operation, bound] of bounds) {
    it(`answers ${operation} ${value} on decimal(18,2) as a question about the column`, () => {
      expect(decimalBound('long/decimal(18,2)', value, operation)).toBe(bound)
    })
  }

  it('refuses an answer some shard did not contribute to', () => {
    whole({ timed_out: false, _shards: { total: 2, failed: 0 } }, 'a page')
    expect(() => whole({ timed_out: true, _shards: { failed: 0 } }, 'a page')).toThrow(/partial/)
    expect(() => whole({ timed_out: false, _shards: { failed: 1 } }, 'a count')).toThrow(/1 failed shard/)
  })

  it('asks the engine to fail rather than answer in part, and refuses an answer that still is', async () => {
    const { elasticsearchMapping } = await import('../src/schema.js')
    const layout = { tables: { Note: 'note' }, columns: { Note: { id: 'long' } }, indexes: [], partitionBy: {} }
    const mapping = elasticsearchMapping(layout, 'Note', { Note: ['id'] })
    const column = { name: 'id', type: 'int64' }
    const plan = { columns: [column], filters: [], order: [column], descending: false, after: null, limit: 10 }
    const sent: string[] = []
    const answering = (partial: Record<string, unknown>) =>
      async (method: string, path: string): Promise<[number, unknown]> => {
        sent.push(path)
        if (path.endsWith('/_mapping')) return [200, { note: { mappings: mapping } }]
        if (path.startsWith('/sde_map_state') && method === 'PUT') return [200, {}]
        if (path.includes('/_count')) return [200, { count: 3, ...partial }]
        return [200, { hits: { hits: [] }, ...partial }]
      }
    const cases: [Record<string, unknown>, boolean][] = [
      [{ timed_out: false, _shards: { total: 1, successful: 1, failed: 0 } }, false],
      [{ timed_out: true, _shards: { failed: 0 } }, true],
      [{ timed_out: false, _shards: { failed: 1 } }, true],
    ]
    for (const [partial, refused] of cases) {
      const engine = new ElasticsearchEngine('elasticsearch+http://127.0.0.1:9200')
      Object.assign(engine, { request: answering(partial) })
      const calls = [
        () => engine.selectRows('note', plan), () => engine.searchRows('note', plan, ['x'], []),
        () => engine.countRows('note', plan), () => engine.mapWatermark(),
      ]
      for (const call of calls) {
        if (refused) await expect(call()).rejects.toThrow(/partial/)
        else await call()
      }
    }
    const searches = sent.filter((path) => path.includes('_search'))
    expect(searches.length).toBeGreaterThan(0)
    for (const path of searches) expect(path.endsWith('?allow_partial_search_results=false')).toBe(true)
  })

  it('names a refused write by its type, never by a reason that quotes the value', async () => {
    const refused = writeErrorOf(400, { error: { type: 'illegal_argument_exception', reason: "Preview: 'secret'" } })
    expect(refused).toBe('400 illegal_argument_exception')
    const { elasticsearchMapping } = await import('../src/schema.js')
    const layout = { tables: { Note: 'note' }, columns: { Note: { id: 'long', body: 'keyword' } }, indexes: [], partitionBy: {} }
    const mapping = elasticsearchMapping(layout, 'Note', { Note: ['id'] })
    const engine = new ElasticsearchEngine('elasticsearch+http://127.0.0.1:9200')
    Object.assign(engine, {
      request: async (_method: string, path: string): Promise<[number, unknown]> => path.endsWith('/_mapping')
        ? [200, { note: { mappings: mapping } }]
        : [400, { error: { type: 'document_parsing_exception', reason: "Preview of field's value: 'secret'" } }],
    })
    const row = { id: 1n, body: 'secret' }
    for (const call of [() => engine.insert('note', row), () => engine.insertMany('note', [row])]) {
      const error = await call().then(() => null, (caught: unknown) => caught as Error)
      expect(error?.message).toContain('document_parsing_exception')
      expect(error?.message).not.toContain('secret')
    }
  })
})

describe('exact JSON', () => {
  it('keeps every integer past 2^53 as a bigint, both ways', () => {
    const text = '{"a":9223372036854775807,"b":-9007199254740993,"c":12,"d":0.1,' +
      '"e":"x\\u00e9\\"y\\ud83d\\ude00","f":[true,false,null,-1e-7],"g":{},"h":[]}'
    const value = parseExactJson(text) as Record<string, unknown>
    expect(value['a']).toBe(9223372036854775807n)
    expect(value['b']).toBe(-9007199254740993n)
    expect(value['c']).toBe(12)
    expect(value['d']).toBe(0.1)
    expect(value['e']).toBe('xé"y\u{1F600}')
    expect(value['f']).toEqual([true, false, null, -1e-7])
    expect(parseExactJson(stringifyExactJson(value))).toEqual(value)
    expect(stringifyExactJson({ n: 2n ** 63n - 1n })).toBe('{"n":9223372036854775807}')
  })

  it('keeps the sign of a zero, as Python writes it', () => {
    expect(stringifyExactJson({ x: -0 })).toBe('{"x":-0.0}')
    expect(Object.is(parseExactJson('-0.0'), -0)).toBe(true)
    expect(stringifyExactJson(0)).toBe('0')
  })

  it('refuses what is not JSON', () => {
    for (const bad of ['{', '[1,]', '01', 'nul', '{"a":1} x', '"open']) {
      expect(() => parseExactJson(bad)).toThrow(SyntaxError)
    }
    expect(() => stringifyExactJson(Number.NaN)).toThrow(TypeError)
  })
})

describe('what the adapter refuses', () => {
  it('opens no transaction and names why it gives no summary', async () => {
    const engine = new ElasticsearchEngine('elasticsearch+http://127.0.0.1:1')
    await expect(engine.transaction(async () => 1)).rejects.toThrow(/no multi-document transactions/)
    expect(engine.summaryRefusal).toMatch(/double precision/)
    expect(engine.dialect).toBe('elasticsearch')
    expect(searchEngine(engine)).toBe(engine)
  })

  it('lets the orderbook engine refuse a search by name', () => {
    expect(() => searchEngine(new OrderbookEngine({ host: '127.0.0.1', port: 1 }))).toThrow(SearchRefused)
  })
})
