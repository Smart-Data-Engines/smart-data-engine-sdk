# A customer-side Weather application

The starter is shipped in the Python wheel (`sde-weather`) and npm tarball (`sde-weather-ts`).
It writes and reads synthetic weather observations through the logical SDK API. The controller
supplies signed metadata; it never runs this data application or receives its credentials.

This starter is **unreleased**. The existing PyPI development release predates it, and the npm
package has not been published. Use the reviewed wheel/npm artifacts supplied for the demo.
The [runbook](../../docs/weather-starter.md) covers setup, both clients, telemetry, local operator
handoffs, recovery and reset. [model.json](model.json) is the model to declare in the controller;
[generator.json](generator.json) pins an exact synthetic example in both languages.

Application code you can inspect and adapt:

- [Python workload](../../python/src/sde_demo/runtime.py)
- [TypeScript workload](../../typescript/src/demo/weather.ts)
- [Local provisioning and ownership](../../python/src/sde_demo/resources.py)

The example targets a new application/module. It does not import an arbitrary existing schema,
modify customer tables, or establish a production deployment's TLS, backup or support objectives.
