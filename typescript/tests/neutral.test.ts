/**
 * `neutralDeclaration`: a built model back into the form of format-contract §4a.
 *
 * The mirror of the Python test of the same name, and it is here for the reason the whole
 * conformance suite is: a helper that exists in one language and not the other is a client who can
 * hand over their model from one of their services.
 */
import { readFileSync, readdirSync } from 'node:fs'
import { join } from 'node:path'

import { describe, expect, it } from 'vitest'

import { DeclarationError } from '../src/errors.js'
import { neutralDeclaration } from '../src/model.js'
import { modelFromNeutral } from '../src/testing/loader.js'
import type { NeutralModel } from '../src/testing/loader.js'

const VECTORS = join(__dirname, '..', '..', 'conformance', 'vectors')
const read = (...parts: string[]) => JSON.parse(readFileSync(join(VECTORS, ...parts), 'utf8'))
const models = readdirSync(join(VECTORS, 'model'), { withFileTypes: true })
  .filter((e) => e.isDirectory())
  .map((e) => e.name)
  .sort()

describe('the neutral declaration form', () => {
  it('found vectors to run', () => {
    expect(models.length).toBeGreaterThan(0)
  })

  for (const vector of models) {
    it(`round-trips ${vector} without moving the version`, () => {
      const original = modelFromNeutral(read('model', vector, 'model.json'))
      const again = modelFromNeutral(neutralDeclaration(original) as NeutralModel)
      expect(again.version).toBe(original.version)
      expect(again.ir).toEqual(original.ir)
    })
  }

  it('states a key as a list of names, which is where it differs from the IR', () => {
    const model = modelFromNeutral(read('model', '002-relations-keys-atomicity', 'model.json'))
    const document = neutralDeclaration(model) as {
      entities: { name: string; key: string[] }[]
    }
    const order = document.entities.find((e) => e.name === 'Order')
    expect(order?.key).toEqual(['tenant', 'id'])
  })

  it('refuses the IR by name rather than crashing inside the encoder', () => {
    const model = modelFromNeutral(read('model', '001-single-entity', 'model.json'))
    expect(() => modelFromNeutral(model.ir as NeutralModel)).toThrow(DeclarationError)
    expect(() => modelFromNeutral(model.ir as NeutralModel)).toThrow(/the IR's key form/)
  })
})
