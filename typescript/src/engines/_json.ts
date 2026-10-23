/**
 * JSON with exact integers, both ways, for an engine that keeps 64-bit integers in its documents.
 *
 * `JSON.parse` turns every number into a `number`, and past 2^53 that is no longer the number: a
 * microsecond after the year 2255, an int64 identifier, a decimal times 10^scale. So an integer
 * literal outside the safe range is parsed to a `bigint`, and a `bigint` is written as its digits.
 * Everything else - strings with every escape, fractions, exponents - is the platform's own
 * parser and serializer, applied to one token at a time.
 */

const WHITESPACE = new Set([' ', '\t', '\n', '\r'])
const NUMBER = /-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?/y

class Reader {
  index = 0
  constructor(readonly text: string) {}

  fail(what: string): never {
    throw new SyntaxError(`invalid JSON at ${this.index}: ${what}`)
  }

  skip(): void {
    while (this.index < this.text.length && WHITESPACE.has(this.text[this.index] as string)) {
      this.index += 1
    }
  }

  value(depth: number): unknown {
    if (depth > 512) this.fail('nested too deeply')
    this.skip()
    const char = this.text[this.index]
    if (char === '{') return this.object(depth)
    if (char === '[') return this.array(depth)
    if (char === '"') return this.string()
    if (char === 't' || char === 'f' || char === 'n') return this.literal()
    return this.number()
  }

  object(depth: number): Record<string, unknown> {
    const out: Record<string, unknown> = Object.create(null)
    this.index += 1
    this.skip()
    if (this.text[this.index] === '}') {
      this.index += 1
      return { ...out }
    }
    for (;;) {
      this.skip()
      if (this.text[this.index] !== '"') this.fail('expected a key')
      const key = this.string()
      this.skip()
      if (this.text[this.index] !== ':') this.fail('expected a colon')
      this.index += 1
      out[key] = this.value(depth + 1)
      this.skip()
      const next = this.text[this.index]
      this.index += 1
      if (next === '}') return { ...out }
      if (next !== ',') this.fail('expected a comma or a closing brace')
    }
  }

  array(depth: number): unknown[] {
    const out: unknown[] = []
    this.index += 1
    this.skip()
    if (this.text[this.index] === ']') {
      this.index += 1
      return out
    }
    for (;;) {
      out.push(this.value(depth + 1))
      this.skip()
      const next = this.text[this.index]
      this.index += 1
      if (next === ']') return out
      if (next !== ',') this.fail('expected a comma or a closing bracket')
    }
  }

  string(): string {
    const start = this.index
    this.index += 1
    while (this.index < this.text.length) {
      const char = this.text[this.index]
      if (char === '\\') this.index += 2
      else if (char === '"') {
        this.index += 1
        return JSON.parse(this.text.slice(start, this.index)) as string
      } else this.index += 1
    }
    return this.fail('unterminated string')
  }

  literal(): boolean | null {
    for (const [word, value] of [['true', true], ['false', false], ['null', null]] as const) {
      if (this.text.startsWith(word, this.index)) {
        this.index += word.length
        return value
      }
    }
    return this.fail('unknown literal')
  }

  number(): number | bigint {
    NUMBER.lastIndex = this.index
    const match = NUMBER.exec(this.text)
    if (match === null) this.fail('expected a value')
    const literal = match[0]
    this.index += literal.length
    if (/^-?\d+$/.test(literal)) {
      const exact = BigInt(literal)
      if (exact > BigInt(Number.MAX_SAFE_INTEGER) || exact < BigInt(Number.MIN_SAFE_INTEGER)) {
        return exact
      }
    }
    return Number(literal)
  }
}

/** Parse JSON, with an integer outside the safe range as a `bigint`. */
export function parseExactJson(text: string): unknown {
  const reader = new Reader(text)
  const value = reader.value(0)
  reader.skip()
  if (reader.index !== text.length) reader.fail('trailing characters')
  return value
}

/** Write JSON, with a `bigint` as its digits. Non-finite numbers and `undefined` are refused. */
export function stringifyExactJson(value: unknown): string {
  if (value === null) return 'null'
  if (typeof value === 'bigint') return value.toString()
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) throw new TypeError('JSON has no NaN or infinity')
    // `JSON.stringify(-0)` is `0`, which would lose the sign Python keeps as `-0.0`.
    return Object.is(value, -0) ? '-0.0' : JSON.stringify(value)
  }
  if (typeof value === 'boolean' || typeof value === 'string') return JSON.stringify(value)
  if (Array.isArray(value)) return '[' + value.map(stringifyExactJson).join(',') + ']'
  if (typeof value === 'object') {
    const entries = Object.entries(value as Record<string, unknown>)
      .filter(([, item]) => item !== undefined)
      .map(([key, item]) => JSON.stringify(key) + ':' + stringifyExactJson(item))
    return '{' + entries.join(',') + '}'
  }
  throw new TypeError(`JSON has no ${typeof value}`)
}
