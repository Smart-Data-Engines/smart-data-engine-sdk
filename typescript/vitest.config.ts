import { defineConfig } from 'vitest/config'

export default defineConfig({
  test: {
    include: ['tests/**/*.test.ts'],
    // The conformance runner reads files from ../conformance, which is outside this package. That is
    // deliberate: the vectors are shared, so they cannot live inside any one language's tree.
    root: '.',
  },
})
