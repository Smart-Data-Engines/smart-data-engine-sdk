import { defineConfig } from 'vitest/config'

export default defineConfig({
  test: {
    // This repository runs Node tests. It does not expose a test API or browser-mocking server.
    api: false,
    browser: { enabled: false },
    include: ['tests/**/*.test.ts'],
    // The conformance runner reads files from ../conformance, which is outside this package. That is
    // deliberate: the vectors are shared, so they cannot live inside any one language's tree.
    root: '.',
  },
})
