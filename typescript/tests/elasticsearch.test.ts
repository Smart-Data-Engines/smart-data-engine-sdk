/** The TypeScript Elasticsearch adapter without a server: its profile, its JSON and its refusals. */
import { describe, expect, it } from 'vitest'

import { EngineError, searchEngine, SearchRefused } from '../src/index.js'
import { ElasticsearchEngine, parseDsn } from '../src/engines/elasticsearch.js'
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
  ]
  for (const [dsn, match] of refused) {
    it(`refuses ${dsn}`, () => {
      expect(() => new ElasticsearchEngine(dsn)).toThrow(EngineError)
      expect(() => new ElasticsearchEngine(dsn)).toThrow(match)
    })
  }
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
