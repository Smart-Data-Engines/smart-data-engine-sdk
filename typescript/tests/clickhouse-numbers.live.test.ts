/** Real server JSON settings must not round wide keys or decimal values. */
import { randomUUID } from 'node:crypto'
import { describe, expect, it } from 'vitest'
import { ClickHouseEngine } from '../src/engines/clickhouse.js'

const dsn = process.env['SDE_CLICKHOUSE_DSN']
const root = dsn ? new URL(dsn) : undefined
async function native(statement: string): Promise<string> {
  const response = await fetch(`http://${root!.host}/?database=${encodeURIComponent(root!.pathname.slice(1))}&wait_end_of_query=1`, {
    method:'POST',body:statement,headers:{Authorization:`Basic ${Buffer.from(`${decodeURIComponent(root!.username)}:${decodeURIComponent(root!.password)}`).toString('base64')}`},
  })
  const text = await response.text()
  if (!response.ok) throw new Error(text)
  return text
}

describe.skipIf(!dsn)('native exact JSON numbers',()=>{
  it('keeps independently written numbers and scale in point, range and migration-key reads',async()=>{
    const name='sde_exact_'+randomUUID().replaceAll('-','')
    const engine=new ClickHouseEngine(dsn!)
    try {
      await native(`CREATE TABLE ${name} (id Int64, amount Decimal(38,18), rounded Decimal(8,2), optional Nullable(Decimal(38,18))) ENGINE=ReplacingMergeTree ORDER BY id`)
      await native(`INSERT INTO ${name} SELECT toInt64('9007199254740993'),toDecimal128('12345678901234567890.123456789012345678',18),toDecimal64('23.60',2),NULL UNION ALL SELECT toInt64('9007199254740995'),toDecimal128('-12345678901234567890.123456789012345678',18),toDecimal64('-0.10',2),toDecimal128('0.000000000000000001',18)`)
      const exact = await native(`SELECT toString(id),toString(amount) FROM ${name} ORDER BY id FORMAT TabSeparated`)
      expect(exact).toContain('9007199254740993\t12345678901234567890.123456789012345678')
      await engine.connect()
      const expected=[{id:9007199254740993n,amount:'12345678901234567890.123456789012345678',rounded:'23.60',optional:null},
        {id:9007199254740995n,amount:'-12345678901234567890.123456789012345678',rounded:'-0.10',optional:'0.000000000000000001'}]
      expect(await engine.get(name,{id:expected[0]!.id})).toEqual(expected[0])
      expect(await engine.keyRange(name,['id'])).toEqual(expected)
      expect(await engine.nthKey(name,['id'],{position:1})).toEqual([expected[0]!.id])
      expect(await engine.keyRange(name,['id'],{after:[expected[0]!.id],limit:1})).toEqual([expected[1]])
    } finally { await engine.close(); await native(`DROP TABLE IF EXISTS ${name} SYNC`) }
  })
  it.each([{readonly:1,quoted:true},{readonly:1,quoted:false},{readonly:2,quoted:false}])('preserves exact data or refuses incompatible locked defaults ($readonly/$quoted)',async({readonly,quoted})=>{
    const suffix=randomUUID().replaceAll('-',''),table='sde_exact_'+suffix,user='sde_exact_'+suffix+'_app'
    const password=randomUUID(),runtime=new URL(dsn!)
    runtime.username=user;runtime.password=password
    const engine=new ClickHouseEngine(runtime.href)
    try {
      await native(`CREATE TABLE ${table} (id Int64, amount Decimal(38,18)) ENGINE=ReplacingMergeTree ORDER BY id`)
      await native(`INSERT INTO ${table} SELECT toInt64('9007199254740993'),toDecimal128('12345678901234567890.123456789012345678',18)`)
      await native(`CREATE USER ${user} IDENTIFIED WITH sha256_password BY '${password}' SETTINGS output_format_json_quote_64bit_integers=${Number(quoted)},output_format_json_quote_decimals=${Number(quoted)},output_format_decimal_trailing_zeros=${Number(quoted)},readonly=${readonly}`)
      await native(`GRANT SELECT ON ${root!.pathname.slice(1)}.${table} TO ${user}`)
      if (readonly === 1 && !quoted) {
        // This profile forbids correcting a lossy wire format. Refuse before returning data.
        await expect(engine.connect()).rejects.toThrow(/Cannot modify.*readonly/)
      } else {
        await engine.connect()
        expect(await engine.get(table,{id:9007199254740993n})).toEqual({id:9007199254740993n,amount:'12345678901234567890.123456789012345678'})
      }
    } finally {await engine.close();await native(`DROP USER IF EXISTS ${user}`);await native(`DROP TABLE IF EXISTS ${table} SYNC`)}
  })
})
