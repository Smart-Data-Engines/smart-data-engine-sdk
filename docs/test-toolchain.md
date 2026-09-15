# TypeScript test tools

The SDK supports Node 18, 20 and 22. The test toolchain uses Vitest 3.2.7 and an explicit
Vite 6.4.3 dependency range so that a new installation does not silently choose Vite 7,
which has a different Node requirement. Both selected packages declare Node 18 support.
This changes development tools only; neither package is an SDK runtime dependency.

Run tests with `npm test` or `npx vitest run`. The repository config disables the test API and
browser mode explicitly. It does not configure the public mockerPlugin/interceptorPlugin,
a Vitest UI or a browser-test dev server. Do not expose these test tools as a service.

## Audit record, 15 September 2026

The previous Vitest 1.6 toolchain produced four npm audit findings: one critical, one high
and two moderate. Upgrading Vitest and Vite removes the critical/high findings, including
[the Vitest UI advisory](https://github.com/vitest-dev/vitest/security/advisories/GHSA-5xrq-8626-4rwp)
and the affected Vite/esbuild versions. The full native suite passed on Node 18.19.1:
644 tests across 42 files, using PostgreSQL, ClickHouse and the Python interoperability peers.
The build, strict typecheck and the required Node matrix also gate this change.

The full development audit still reports **two moderate entries for one advisory**,
[GHSA-82fw-gwwq-j7x9](https://github.com/vitest-dev/vitest/security/advisories/GHSA-82fw-gwwq-j7x9),
against vitest and @vitest/mocker. This is not reported as fixed. The maintainer describes an
exposed development-server mock-registration path; the unauthenticated path requires the public
mocker/interceptor plugins, which this repository does not use. Our supported test invocation
runs Node tests with API/browser mode disabled. This scope assessment is not a claim that an
operator can safely expose a differently configured test server.

The advisory is fixed in Vitest 4.1.11 and newer maintained lines, while the maintainer does not
plan a 3.x backport. Those tool lines require a newer Node baseline. Moving the complete test suite
to a maintained major therefore needs an explicit compatibility solution for the SDK's existing
Node 18 claim; silently dropping that matrix entry or overriding incompatible internal Vitest
packages would weaken the evidence. Keep this development-only exception visible and reassess it
when changing the supported Node versions, test mode or runner. Do not copy it to another project.

`npm audit --omit=dev` reports no runtime findings for this lockfile. npm installs a dependency's
runtime and peer requirements, not its development tools; a customer SDK installation does not
install Vitest or Vite. Review published artifacts and their production dependency tree separately
from this development audit.
