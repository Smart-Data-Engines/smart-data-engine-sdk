/** The same fixed logical Weather model used by the Python customer starter. */
import { createHash } from 'node:crypto'
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
export function reading(runId: string, worker: number, sequence: number) {
  const bytes = createHash('sha256').update(`sde-weather-v1:${runId}:${worker}:${sequence}`).digest().subarray(0, 16)
  bytes[6] = (bytes[6]! & 0x0f) | 0x40
  bytes[8] = (bytes[8]! & 0x3f) | 0x80
  const hex = bytes.toString('hex'), cents = 1525 + sequence % 1000
  return { station: `weather-${runId}-${worker}`,
    at: Timestamp.fromEpochMicroseconds(baseTime.epochMicroseconds + BigInt(sequence)),
    celsius: `${Math.floor(cents / 100)}.${String(cents % 100).padStart(2, '0')}`,
    humidity: BigInt(30 + sequence % 70),
    id: `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}` }
}
