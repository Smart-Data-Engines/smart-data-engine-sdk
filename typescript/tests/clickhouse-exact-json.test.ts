/** Exact numeric values must survive JSON before a JavaScript number can round them. */
import { createServer } from 'node:http'
import type { AddressInfo } from 'node:net'
import { afterEach, describe, expect, it } from 'vitest'
import { ClickHouseEngine } from '../src/engines/clickhouse.js'

const closers: Array<() => Promise<void>> = []
afterEach(async () => { while (closers.length) await closers.pop()!() })
async function endpoint(meta: Array<{name: string, type: string}>, data: string) {
  const requests: URL[] = []
  const server = createServer((request, response) => {
    const url = new URL(request.url!, 'http://fixture.invalid'); requests.push(url)
    response.writeHead(200, { 'Content-Type': 'application/json', Connection: 'close' })
    response.end(url.searchParams.get('query')?.startsWith('SELECT version()')
      ? '{"data":[{"version":"26.8.2.7"}]}' : JSON.stringify({meta}).slice(0,-1)+',"data":['+data+']}')
  })
  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve))
  closers.push(async () => { server.closeAllConnections(); await new Promise<void>(resolve => server.close(() => resolve())) })
  const engine = new ClickHouseEngine(`http://synthetic:fixture@127.0.0.1:${(server.address() as AddressInfo).port}/db`)
  await engine.connect()
  return { engine, requests }
}

describe('exact ClickHouse JSON', () => {
  it('pins exact integer, decimal and decimal-scale settings on every JSON exchange', async () => {
    const {engine,requests} = await endpoint([{name:'amount',type:'Decimal(38, 18)'}],'{"amount":"12345678901234567890.123456789012345678"}')
    try {
      expect((await engine.get('prices',{id:1n}))?.amount).toBe('12345678901234567890.123456789012345678')
      expect(requests.length).toBe(2)
      for(const request of requests) for(const key of ['output_format_json_quote_64bit_integers','output_format_json_quote_decimals','output_format_decimal_trailing_zeros']) expect(request.searchParams.get(key)).toBe('1')
    } finally { await engine.close() }
  })
  it.each([
    ['Decimal(38, 18)','12345678901234567890.123456789012345678'],
    ['Decimal(8, 2)','12.34'],
    ['Int64','9007199254740993'],
    ['UInt64','18446744073709551615'],
    ['Int128','9007199254740993'],
    ['Nullable(Decimal(8, 2))','0'],
  ])('refuses unquoted %s rather than converting a rounded number back to text', async(type, literal) => {
    const {engine}=await endpoint([{name:'value',type}],'{"value":'+literal+'}')
    try { await expect(engine.get('values',{id:1})).rejects.toThrow() }
    finally {await engine.close()}
  })
  it('preserves exact strings and nullable values',async()=>{
    const {engine}=await endpoint([{name:'amount',type:'Decimal(38, 18)'},{name:'big',type:'Int64'},{name:'optional',type:'Nullable(Decimal(8, 2))'}],'{"amount":"-12345678901234567890.123456789012345678","big":"9007199254740993","optional":null}')
    try {expect(await engine.get('values',{id:1})).toEqual({amount:'-12345678901234567890.123456789012345678',big:9007199254740993n,optional:null})}
    finally {await engine.close()}
  })
})
