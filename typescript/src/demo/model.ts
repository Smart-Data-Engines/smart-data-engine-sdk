/** The same fixed logical Weather model used by the Python customer starter. */
import { createHash } from 'node:crypto'
import { canonicalBytes } from '../canonical.js'
import { assemble } from '../model.js'
import { Timestamp } from '../timestamp.js'

export function weatherModel() {
  return assemble([{ name: 'WeatherReading', fields: [
    { name: 'at', type: 'timestamptz', nullable: false },
    { name: 'celsius', type: 'decimal(8,2)', nullable: false },
    { name: 'humidity', type: 'int64', nullable: false },
    { name: 'id', type: 'uuid', nullable: false },
    { name: 'station', type: 'string', nullable: false },
  ], key: ['station', 'at'], pii: [], residency: 'EU' }], [], [],
  { amount: '500.00', currency: 'EUR' })
}
export const baseTime = Timestamp.from('2026-01-01T00:00:00.000000Z')
const stationTemplate = 'weather-{run_id}-{worker}', seedTemplate = 'sde-weather-v1:{run_id}:{worker}:{sequence}'
export const celsiusBaseCents = 1525, celsiusModulus = 1000
export const humidityBase = 30, humidityModulus = 70
const uuidVersion = 4
export const generatorSpec = Object.freeze({
  kind: 'sde-weather-generator', version: 1, worker: 0,
  station_template: stationTemplate, seed_template: seedTemplate,
  base_time: baseTime.toISOString(), timestamp_step_microseconds: 1,
  celsius_base_cents: celsiusBaseCents, celsius_modulus: celsiusModulus,
  humidity_base: humidityBase, humidity_modulus: humidityModulus,
  id: 'first 16 bytes of SHA256(UTF8(seed)), RFC4122 UUID version bits', uuid_version: uuidVersion,
})
export const generatorId = 'weather-v1:' + createHash('sha256').update(canonicalBytes(generatorSpec)).digest('hex')

export function reading(runId: string, worker: number, sequence: number) {
  const bytes = createHash('sha256').update(seedTemplate.replace('{run_id}', runId).replace('{worker}', String(worker)).replace('{sequence}', String(sequence))).digest().subarray(0, 16)
  bytes[6] = (bytes[6]! & 0x0f) | (uuidVersion << 4)
  bytes[8] = (bytes[8]! & 0x3f) | 0x80
  const hex = bytes.toString('hex'), cents = celsiusBaseCents + sequence % celsiusModulus
  return { station: stationTemplate.replace('{run_id}', runId).replace('{worker}', String(worker)),
    at: Timestamp.fromEpochMicroseconds(baseTime.epochMicroseconds + BigInt(sequence)),
    celsius: `${Math.floor(cents / 100)}.${String(cents % 100).padStart(2, '0')}`,
    humidity: BigInt(humidityBase + sequence % humidityModulus),
    id: `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}` }
}
