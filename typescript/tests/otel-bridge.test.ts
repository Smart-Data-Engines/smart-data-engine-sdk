/**
 * The OpenTelemetry bridge of docs/observability.md, run: a hook that makes spans and a histogram -
 * the counterpart of python/tests/test_otel_bridge.py.
 *
 * The library depends on no tracing package, so the bridge is the application's code. The block
 * between the markers is the one the document shows, and the last test holds the two equal. The
 * tracer and the histogram are stand-ins with @opentelemetry/api's shape - `startSpan(name,
 * { startTime, attributes })`, `setStatus`, `end(endTime)`, `record(value, attributes)` - so nothing
 * is installed for it.
 */
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { expect, it } from 'vitest'

import type { OperationHook, OperationRecord } from '../src/index.js'

interface Span {
  name: string
  startTime: number
  attributes: Record<string, unknown>
  status?: { code: number; message?: string }
  endTime?: number
  setStatus(status: { code: number; message?: string }): void
  end(endTime: number): void
}
interface Tracer { startSpan(name: string, options: { startTime: number; attributes: Record<string, unknown> }): Span }
interface Histogram { record(value: number, attributes: Record<string, unknown>): void }
const SpanStatusCode = { UNSET: 0, OK: 1, ERROR: 2 } as const

function stand_ins() {
  const spans: Span[] = []
  const values: [number, Record<string, unknown>][] = []
  const tracer: Tracer = {
    startSpan(name, options) {
      const span: Span = { name, startTime: options.startTime, attributes: options.attributes,
        setStatus(status) { this.status = status }, end(endTime) { this.endTime = endTime } }
      spans.push(span)
      return span
    },
  }
  const histogram: Histogram = { record(value, attributes) { values.push([value, attributes]) } }
  return { tracer, histogram, spans, values }
}

// bridge:begin
/** A span and a histogram value for each operation the session completes. */
function opentelemetryHook(tracer: Tracer, histogram: Histogram): OperationHook {
  return (record) => {
    const end = performance.timeOrigin + performance.now()
    const span = tracer.startSpan(`sde.${record.operation}`, {
      startTime: end - record.durationNs / 1e6,
      attributes: {
        'db.system': record.dialect,
        'sde.entity': record.entity,
        'sde.group': record.group,
        'sde.shape': record.shape,
        'sde.engine': record.engine,
        'sde.materialization': record.materialization,
        'sde.rows': record.rows,
      },
    })
    if (record.outcome !== 'ok') span.setStatus({ code: SpanStatusCode.ERROR, message: record.outcome })
    span.end(end)
    histogram.record(record.durationNs / 1e9, {
      'sde.operation': record.operation,
      'db.system': record.dialect,
      'sde.outcome': record.outcome,
    })
  }
}
// bridge:end

function record(changes: Partial<OperationRecord> = {}): OperationRecord {
  return { operation: 'get', entity: 'Note', group: 'Note', shape: 'a1b2c3', engine: 'pg-main',
    dialect: 'postgres', materialization: 'notes@pg', durationNs: 1_500_000, rows: 1, outcome: 'ok',
    ...changes }
}

it('makes a record a span that ends now and began its duration before', () => {
  const { tracer, histogram, spans, values } = stand_ins()
  opentelemetryHook(tracer, histogram)(record())
  expect(spans).toHaveLength(1)
  const [span] = spans
  expect(span!.name).toBe('sde.get')
  expect(span!.endTime! - span!.startTime).toBeCloseTo(1.5, 6)
  expect(span!.attributes['db.system']).toBe('postgres')
  expect(span!.attributes['sde.materialization']).toBe('notes@pg')
  expect(span!.status).toBeUndefined()
  expect(values).toEqual([[0.0015, { 'sde.operation': 'get', 'db.system': 'postgres', 'sde.outcome': 'ok' }]])
})

it('makes an error the span status, with its class', () => {
  const { tracer, histogram, spans, values } = stand_ins()
  opentelemetryHook(tracer, histogram)(record({ outcome: 'EngineError', rows: 0 }))
  expect(spans[0]!.status).toEqual({ code: SpanStatusCode.ERROR, message: 'EngineError' })
  expect(values[0]![1]['sde.outcome']).toBe('EngineError')
})

it('is the code the document shows', () => {
  const source = readFileSync(new URL(import.meta.url), 'utf8')
  const ours = source.split('// bridge:begin\n')[1]!.split('// bridge:end')[0]!
  const document = readFileSync(join(import.meta.dirname, '..', '..', 'docs', 'observability.md'), 'utf8')
  const shown = /<!-- bridge:typescript -->\n```typescript\n([\s\S]*?)```/.exec(document)
  expect(shown, 'docs/observability.md shows no TypeScript bridge').not.toBeNull()
  const code = (text: string) => text.split('\n').filter((line) => !line.startsWith('import ')).join('\n').trim()
  expect(code(shown![1]!)).toBe(code(ours))
})
