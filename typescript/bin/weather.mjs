#!/usr/bin/env node
/** Runtime entry point; bootstrap and the local operator use the Python sde-weather command. */
import { resolve } from 'node:path'
import { parseArgs } from 'node:util'
import { DemoRefused } from '../dist/demo/project.js'
import { runWeather } from '../dist/demo/weather.js'

try {
  const { values, positionals } = parseArgs({ allowPositionals: true, options: {
    directory: { type: 'string' }, iterations: { type: 'string', default: '10' },
    'batch-size': { type: 'string', default: '10' }, 'interval-ms': { type: 'string', default: '100' },
    'recovery-ms': { type: 'string', default: '10000' }, workload: { type: 'string', default: 'mixed' },
    help: { type: 'boolean' },
  } })
  if (values.help) {
    console.log('sde-weather-ts --directory DIR run [--iterations 10] [--batch-size 10] [--interval-ms 100] [--recovery-ms 10000] [--workload mixed|point|analytics]')
  } else {
    if (positionals.length !== 1 || positionals[0] !== 'run' || !values.directory ||
        !['mixed', 'point', 'analytics'].includes(values.workload ?? 'mixed')) throw new DemoRefused('Use --help for the local runtime command.')
    const result = await runWeather(resolve(values.directory), {
      iterations: Number(values.iterations), batchSize: Number(values['batch-size']),
      intervalMs: Number(values['interval-ms']), recoveryMs: Number(values['recovery-ms']),
      workload: values.workload === 'point' ? 'point' : values.workload === 'analytics' ? 'analytics' : 'mixed',
    })
    console.log(JSON.stringify(result))
  }
} catch (error) {
  console.error(JSON.stringify({ error: error instanceof Error ? error.name : 'UnknownError', message:
    error instanceof DemoRefused ? error.message : 'The local operation did not complete. Inspect the run report before retrying.' }))
  process.exitCode = error instanceof DemoRefused ? 2 : 3
}
