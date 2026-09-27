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
    // A hang detector, not a performance assertion: a hung test never finishes, so this number only
    // decides how long it takes to say so. vitest's default of 5 s failed four tests on
    // 27 September that take 0.4-1.7 s on an idle machine, while another session's build and tests
    // held the load average at 8 on four threads. A test that names its own budget keeps it, and a
    // bound a client depends on is asserted by its test (failure.test.ts), not by this setting.
    testTimeout: 30_000,
  },
})
