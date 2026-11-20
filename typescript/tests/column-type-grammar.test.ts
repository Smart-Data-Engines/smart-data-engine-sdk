/**
 * The column type grammar at its bound (format contract §7a). Not a vector: no type of the two
 * vocabularies is 128 characters long without spaces its server would not keep, and a vector's
 * statement is one a server runs. The vectors hold every other edge, in all three libraries.
 */
import { describe, expect, it } from 'vitest'

import { columnTypeRefusal } from '../src/schema.js'

describe('the column type grammar', () => {
  it('takes a type of 128 characters and refuses one of 129', () => {
    const longest = 'character' + ' '.repeat(107) + 'varying(255)'
    expect(longest.length).toBe(128)
    expect(columnTypeRefusal(longest)).toBeNull()
    expect(columnTypeRefusal(longest + ' ')).toMatch(/is not a column type this library renders/)
  })
})
