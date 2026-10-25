import assert from 'node:assert/strict'
import { registerHooks } from 'node:module'
import { pathToFileURL } from 'node:url'
import { join } from 'node:path'

const reference = process.argv[2]
registerHooks({ resolve(specifier, context, nextResolve) {
  if (specifier === '@deepseek-ai/cosmokit') return { shortCircuit: true, url: pathToFileURL(join(reference, 'vendor/cosmokit/src/index.ts')).href }
  return nextResolve(specifier, context)
}})
const { Context, FiberState, Service } = await import(pathToFileURL(join(reference, 'vendor/cordis/src/index.ts')).href)
const log = (scenario_id, event, data = {}) => process.stdout.write(JSON.stringify({ scenario_id, event, ...data }) + '\n')
const gate = () => { let resolve; const promise = new Promise(r => resolve = r); return { promise, resolve } }
const tests = []
function test(id, coverage, deviations, body) { tests.push({ id, coverage, deviations, body }) }
const root = () => { const ctx = new Context(); ctx.logger.error = () => {}; return ctx }
const plugin = (ctx, name, apply, inject = []) => ctx.plugin({ name, apply, inject })
const settle = async (...fibers) => { for (const fiber of fibers) await fiber.await() }

// Each case states its actual fragment; C/S IDs remain acceptance requirements,
// not a claim that a TS observation implements all Stage-only assertions.
test('C1-01', 'setup failure cleans previously registered effects', ['D-04'], async () => {
  const ctx = root(), events = []
  const fiber = plugin(ctx, 'broken', p => { p.effect(() => () => events.push('A')); p.effect(() => () => events.push('B')); throw Error('setup') })
  await assert.rejects(fiber.await(), /setup/)
  await fiber.dispose()
  assert.deepEqual([...events].sort(), ['A', 'B'])
  return { cleaned: events }
})
test('C1-02', 'unload waits for in-flight async setup and cleans its late disposer', [], async () => {
  const ctx = root(), entered = gate(), release = gate(), events = []
  const fiber = plugin(ctx, 'late', async () => { entered.resolve(); await release.promise; return () => events.push('cleanup') })
  await entered.promise
  const closing = fiber.dispose(); release.resolve(); await closing
  assert.deepEqual(events, ['cleanup']); assert.equal(fiber.state, FiberState.DISPOSED)
  return { cleaned: true }
})
test('C1-03', 'effect inverse executes once across manual and owner close', ['D-03'], async () => {
  const ctx = root(); let cleaned = 0, close
  const fiber = plugin(ctx, 'single', p => { close = p.effect(() => () => cleaned++) })
  await fiber.await(); await close(); await close(); await fiber.dispose()
  assert.equal(cleaned, 1); return { cleaned }
})
test('C1-04', 'safe independent cleanup continues after another cleanup throws', ['D-02', 'D-04'], async () => {
  const ctx = root(), events = []
  const fiber = plugin(ctx, 'cleanup-error', p => { p.effect(() => () => { events.push('bad'); throw Error('cleanup') }); p.effect(() => () => events.push('good')) })
  await fiber.await(); await fiber.dispose(); assert.deepEqual(events.sort(), ['bad', 'good'])
  return { errors_propagate: false, other_cleanup_runs: true }
})
test('C1-05', 'disposed owner rejects a new effect', [], async () => {
  const ctx = root(); let own
  const fiber = plugin(ctx, 'inactive', p => { own = p }); await fiber.await(); await fiber.dispose()
  assert.throws(() => own.effect(() => {}), /inactive/); return { rejected: true }
})
test('C2-01', 'consumer waits for provider then automatically activates', [], async () => {
  const ctx = root(); let runs = 0
  const consumer = plugin(ctx, 'consumer', () => { runs++ }, ['db'])
  await consumer.await(); assert.equal(consumer.state, FiberState.PENDING)
  const provider = plugin(ctx, 'db', p => p.provide('db', 1)); await settle(provider, consumer)
  assert.equal(runs, 1); await consumer.dispose(); await provider.dispose(); return { runs }
})
test('C2-02', 'dependency removal and restoration unloads and reloads consumer', [], async () => {
  const ctx = root(); let runs = 0, cleaned = 0
  const consumer = plugin(ctx, 'consumer', () => { runs++; return () => cleaned++ }, ['db'])
  let provider = plugin(ctx, 'db', p => p.provide('db', 1)); await settle(provider, consumer)
  await provider.dispose(); assert.equal(consumer.state, FiberState.PENDING)
  provider = plugin(ctx, 'db2', p => p.provide('db', 2)); await settle(provider, consumer)
  assert.equal(runs, 2); assert.equal(cleaned, 1); await consumer.dispose(); await provider.dispose(); return { runs, cleaned_before_final_close: 1 }
})
test('C2-03', 'explicit check notification changes dependency availability', [], async () => {
  const ctx = root(); let available = false, runs = 0
  const provider = plugin(ctx, 'db', p => p.provide('db', {}, () => available)); await provider.await()
  const consumer = plugin(ctx, 'consumer', () => { runs++ }, ['db']); await consumer.await(); assert.equal(runs, 0)
  available = true; ctx.reflect.notify(['db']); await consumer.await(); assert.equal(runs, 1)
  available = false; ctx.reflect.notify(['db']); await consumer.await(); assert.equal(consumer.state, FiberState.PENDING)
  await consumer.dispose(); await provider.dispose(); return { runs, inactive_after_false: true }
})
test('C2-04', 'duplicate provider rejected; reference missing-service wait is pending', ['D-09'], async () => {
  const ctx = root(); ctx.provide('db', 1); assert.throws(() => ctx.provide('db', 2), /registered/)
  const waiting = plugin(ctx, 'pending', () => {}, ['missing']); await waiting.await(); assert.equal(waiting.state, FiberState.PENDING)
  await waiting.dispose(); return { duplicate_rejected: true, pending_is_not_ready: true, cycle_diagnostics: 'Stage-only' }
})
test('C3-01', 'equal provider values still rebind when provider identity changes', ['D-05'], async () => {
  const ctx = root(), value = {}; let runs = 0
  const consumer = plugin(ctx, 'consumer', () => { runs++ }, ['db'])
  const p1 = plugin(ctx, 'p1', p => p.provide('db', value)); await settle(p1, consumer); await p1.dispose()
  const p2 = plugin(ctx, 'p2', p => p.provide('db', value)); await settle(p2, consumer); assert.equal(runs, 2)
  await consumer.dispose(); await p2.dispose(); return { runs }
})
test('C3-04', 'unrelated service does not reload consumer', [], async () => {
  const ctx = root(); ctx.provide('db', 1); let runs = 0
  const consumer = plugin(ctx, 'consumer', () => { runs++ }, ['db']); await consumer.await()
  const unrelated = plugin(ctx, 'other', p => p.provide('other', 2)); await unrelated.await(); await unrelated.dispose()
  assert.equal(runs, 1); await consumer.dispose(); return { runs }
})
test('C4-01', 'consumer cleanup retains old committed reference after public revocation', [], async () => {
  const ctx = root(), entered = gate(), release = gate(); let cleanupValue, publicValue
  const p1 = plugin(ctx, 'p1', p => p.provide('db', 'old')); await p1.await()
  const consumer = plugin(ctx, 'consumer', p => () => { cleanupValue = p.db; publicValue = ctx.get('db'); entered.resolve(); return release.promise }, ['db']); await consumer.await()
  const closing = p1.dispose(); await entered.promise; assert.equal(cleanupValue, 'old'); assert.equal(publicValue, undefined)
  release.resolve(); await closing; await consumer.dispose(); return { cleanup_value: cleanupValue, public_absent: publicValue === undefined }
})
test('C4-02', 'independent replacement can publish while old consumer cleanup retains old binding', [], async () => {
  const ctx = root(), entered = gate(), release = gate(); let cleanupValue, seen = []
  const p1 = plugin(ctx, 'p1', p => p.provide('db', 'old')); await p1.await()
  const consumer = plugin(ctx, 'consumer', p => { seen.push(p.db); return async () => { cleanupValue = p.db; entered.resolve(); await release.promise; assert.equal(p.db, cleanupValue) } }, ['db']); await consumer.await()
  const closing = p1.dispose(); await entered.promise
  const p2 = plugin(ctx, 'p2', p => p.provide('db', 'new')); await p2.await(); assert.equal(ctx.get('db'), 'new'); assert.equal(cleanupValue, 'old')
  release.resolve(); await closing; await consumer.await(); assert.deepEqual(seen, ['old', 'new'])
  await consumer.dispose(); await p2.dispose(); return { seen, cleanup_value: cleanupValue }
})
test('C4-03', 'undeclared property access rejected while public get observes service', [], async () => {
  const ctx = root(); const provider = plugin(ctx, 'provider', p => p.provide('db', 1)); await provider.await(); let undeclared = false, discovered
  const consumer = plugin(ctx, 'consumer', p => { discovered = p.get('db'); assert.throws(() => p.db, /inject|dependency|access/); undeclared = true }); await consumer.await()
  assert.equal(discovered, 1); assert.equal(undeclared, true); await consumer.dispose(); await provider.dispose(); return { discovered, undeclared_rejected: true }
})
test('C4-04', 'committed service view released after owner disposal', [], async () => {
  const ctx = root(); ctx.provide('db', 1); let own
  const consumer = plugin(ctx, 'consumer', p => { own = p; assert.equal(p.db, 1) }, ['db']); await consumer.await(); await consumer.dispose()
  assert.equal(consumer.store, undefined); return { committed_view_released: true }
})
test('C5-01', 'reference allows pool cleanup before dependent cleanup finishes', ['D-01', 'D-02'], async () => {
  const ctx = root(), entered = gate(), release = gate(), events = []
  const provider = plugin(ctx, 'provider', p => { p.provide('db', {}); p.effect(() => () => events.push('pool.close')) }); await provider.await()
  const consumer = plugin(ctx, 'consumer', p => async () => { assert.ok(p.db); entered.resolve(); await release.promise; events.push('consumer.cleanup.end') }, ['db']); await consumer.await()
  const closing = provider.dispose(); await entered.promise
  assert.deepEqual(events, ['pool.close']); release.resolve(); await closing; await consumer.dispose()
  return { events, stage_requires_reverse_order: true }
})
test('C6-01', 'per-key realm isolates db and inherits unrelated logger', [], async () => {
  const ctx = root(); ctx.provide('db', 'parent'); ctx.provide('shared', 'same')
  const isolated = ctx.isolate('db'); isolated.provide('db', 'child')
  assert.equal(ctx.get('db'), 'parent'); assert.equal(isolated.get('db'), 'child'); assert.equal(isolated.get('shared'), 'same')
  return { parent: ctx.get('db'), child: isolated.get('db'), inherited: isolated.get('shared') }
})
test('C6-02', 'same explicit realm label shares binding, different label remains independent', [], async () => {
  const ctx = root(), label = Symbol('shared'), a = ctx.isolate('db', label), b = ctx.isolate('db', label), c = ctx.isolate('db')
  a.provide('db', 'shared'); c.provide('db', 'private')
  assert.equal(b.get('db'), 'shared'); assert.equal(c.get('db'), 'private'); return { sibling_shared: true, different_independent: true }
})
test('C6-04', 'isolated provider and consumer disposal removes binding and listeners', [], async () => {
  const ctx = root(), isolated = ctx.isolate('db'); let calls = 0
  const provider = plugin(isolated, 'provider', p => p.provide('db', 1)); await provider.await()
  const consumer = plugin(isolated, 'consumer', p => { p.on('probe', () => calls++) }, ['db']); await consumer.await()
  ctx.emit('probe'); assert.equal(calls, 1); await consumer.dispose(); await provider.dispose(); ctx.emit('probe')
  assert.equal(calls, 1); assert.equal(isolated.get('db'), undefined); return { listener_removed: true, binding_removed: true, authz: 'Stage-only' }
})
test('C7-01', 'caller-owned registrations isolated while common provider remains alive', ['D-06'], async () => {
  const ctx = root(); let a = 0, b = 0
  const first = plugin(ctx, 'a', p => { p.on('tick', () => a++) }); const second = plugin(ctx, 'b', p => { p.on('tick', () => b++) }); await settle(first, second)
  ctx.emit('tick'); await first.dispose(); ctx.emit('tick'); assert.equal(a, 1); assert.equal(b, 2); await second.dispose()
  return { first: a, second: b }
})
test('C7-03', 'parent disposal owns nested child effects and removes listeners', [], async () => {
  const ctx = root(); let calls = 0, cleaned = 0
  const parent = plugin(ctx, 'parent', p => { p.plugin({ name: 'child', apply: c => { c.on('probe', () => calls++); c.effect(() => () => cleaned++) } }) }); await parent.await()
  for (const runtime of ctx.registry.values()) for (const fiber of runtime.fibers) await fiber.await()
  ctx.emit('probe'); await parent.dispose(); ctx.emit('probe'); assert.equal(calls, 1); assert.equal(cleaned, 1)
  return { calls, cleaned }
})
test('C7-04', 'stale caller context rejects new registration', [], async () => {
  const ctx = root(); let own
  const consumer = plugin(ctx, 'consumer', p => { own = p }); await consumer.await(); await consumer.dispose()
  assert.throws(() => own.on('probe', () => {}), /inactive/); return { rejected: true }
})
test('C8-01', 'listener removal does not change an existing callback snapshot', ['D-08'], async () => {
  const ctx = root(), events = []; let second
  ctx.on('probe', () => { events.push('first'); second() }); second = ctx.on('probe', () => events.push('second'))
  ctx.emit('probe'); ctx.emit('probe'); assert.deepEqual(events, ['first', 'second', 'first']); return { events }
})
test('C8-02', 'around waterfall wraps downstream and can veto it', [], async () => {
  const ctx = root(), events = []
  ctx.on('around', async next => { events.push('before'); const value = await next(); events.push('after'); return value + 1 })
  const value = await ctx.waterfall('around', async () => { events.push('inner'); return 2 }); assert.equal(value, 3); assert.deepEqual(events, ['before', 'inner', 'after'])
  ctx.on('veto', () => 5); assert.equal(ctx.waterfall('veto', () => assert.fail('vetoed downstream')), 5); return { value, events, veto: 5 }
})
test('C8-03', 'parallel waits all callbacks before aggregate failure; serial short circuits', [], async () => {
  const ctx = root(), entered = gate(), release = gate(); let settled = false
  ctx.on('parallel', async () => { throw Error('broken') }); ctx.on('parallel', async () => { entered.resolve(); await release.promise; settled = true })
  const dispatch = ctx.parallel('parallel'); await entered.promise; assert.equal(settled, false); release.resolve(); await assert.rejects(dispatch, AggregateError); assert.equal(settled, true)
  const events = []; ctx.on('serial', () => { events.push('first'); return 0 }); ctx.on('serial', () => events.push('second')); assert.equal(await ctx.serial('serial'), 0); assert.deepEqual(events, ['first'])
  return { all_settled: settled, serial_stopped_at_zero: true }
})
test('C8-04', 'null undefined false continue while zero and empty string bail', ['D-07'], async () => {
  const observed = []
  for (const value of [null, undefined, false, 0, '']) { const ctx = root(); let next = false; ctx.on('bail', () => value); ctx.on('bail', () => { next = true }); ctx.bail('bail'); observed.push(next) }
  assert.deepEqual(observed, [true, true, true, false, false]); return { continued: observed }
})
test('C8-05', 'reference await can settle PENDING; Stage strict readiness is separate', ['D-03', 'D-04', 'D-09'], async () => {
  const ctx = root(); const pending = plugin(ctx, 'pending', () => assert.fail('must not run'), ['absent']); await pending.await(); assert.equal(pending.state, FiberState.PENDING); await pending.dispose()
  return { pending_returned_without_ready: true }
})

test('C3-02', 'late load result is cleaned; consumer ultimately binds latest independent provider', ['D-05'], async () => {
  const ctx = root(), entered = gate(), release = gate(), firstRevoked = gate(), secondRevoked = gate(), starts = [], cleaned = []
  let revocations = 0
  ctx.on('internal/service', (name, value) => { if (name === 'db' && value === undefined) { revocations++; (revocations === 1 ? firstRevoked : secondRevoked).resolve() } })
  const p1 = plugin(ctx, 'p1', p => p.provide('db', 1)); await p1.await()
  const consumer = plugin(ctx, 'consumer', async p => { const value = p.db; starts.push(value); if (value === 1) { entered.resolve(); await release.promise }; return () => cleaned.push(value) }, ['db'])
  await entered.promise; const close1 = p1.dispose(); await firstRevoked.promise
  const p2 = plugin(ctx, 'p2', p => p.provide('db', 2)); await p2.await(); const close2 = p2.dispose(); await secondRevoked.promise
  const p3 = plugin(ctx, 'p3', p => p.provide('db', 3)); await p3.await(); release.resolve(); await Promise.all([close1, close2]); await consumer.await()
  assert.deepEqual(starts, [1, 3]); assert.deepEqual(cleaned, [1]); await consumer.dispose(); await p3.dispose(); return { starts, stale_cleaned: true }
})
test('C3-03', 'same provider Fiber restart reruns its dependent', ['D-05'], async () => {
  const ctx = root(); let providerRuns = 0, consumerRuns = 0
  const provider = plugin(ctx, 'provider', p => { providerRuns++; return p.provide('db', 1) }); await provider.await()
  const consumer = plugin(ctx, 'consumer', () => { consumerRuns++ }, ['db']); await consumer.await(); const uid = provider.uid
  await provider.restart(); await consumer.await(); assert.equal(provider.uid, uid); assert.equal(providerRuns, 2); assert.equal(consumerRuns, 2)
  await consumer.dispose(); await provider.dispose(); return { providerRuns, consumerRuns, fiber_uid_unchanged: true }
})
test('C5-02', 'multiple provider resource inverses run before dependent cleanup ends', ['D-01', 'D-02'], async () => {
  const ctx = root(), entered = gate(), release = gate(), events = []
  const provider = plugin(ctx, 'provider', p => { p.provide('db', {}); p.effect(() => () => events.push('timer.close')); p.effect(() => () => events.push('pool.close')) }); await provider.await()
  const consumer = plugin(ctx, 'consumer', () => async () => { entered.resolve(); await release.promise; events.push('consumer.end') }, ['db']); await consumer.await()
  const closing = provider.dispose(); await entered.promise; assert.deepEqual([...events].sort(), ['pool.close', 'timer.close']); release.resolve(); await closing; await consumer.dispose()
  return { events, stage_requires_resources_after_consumer: true }
})
test('C5-03', 'reference does not pin arbitrary in-flight service calls during provider disposal', ['D-01'], async () => {
  const ctx = root(), entered = gate(), release = gate(), events = []
  const provider = plugin(ctx, 'provider', p => { p.provide('db', { async query() { entered.resolve(); await release.promise; events.push('query.end') } }); p.effect(() => () => events.push('pool.close')) }); await provider.await()
  const consumer = plugin(ctx, 'consumer', () => {}, ['db']); await consumer.await(); const running = consumer.ctx.db.query(); await entered.promise
  await provider.dispose(); assert.deepEqual(events, ['pool.close']); release.resolve(); await running; await consumer.dispose()
  return { events, request_leases: 'Stage-only' }
})
test('C5-04', 'reference owner disposal remains pending while consumer inverse is pending', ['D-01', 'D-04'], async () => {
  const ctx = root(), entered = gate(), release = gate(); let closed = false
  const provider = plugin(ctx, 'provider', p => p.provide('db', {})); await provider.await()
  const consumer = plugin(ctx, 'consumer', () => async () => { entered.resolve(); await release.promise }, ['db']); await consumer.await()
  const closing = Promise.resolve(provider.dispose()).then(() => { closed = true }); await entered.promise; assert.equal(closed, false)
  release.resolve(); await closing; await consumer.dispose(); return { pending_until_release: true, deadline_report: 'Stage-only' }
})
test('C6-03', 'service intercept merges base ancestors inject and head in order', [], async () => {
  const ctx = root(); class ConfigService extends Service { constructor(p) { super(p, 'config') } }
  const provider = plugin(ctx, 'provider', p => { new ConfigService(p) }); await provider.await(); let resolved
  const scope = ctx.intercept('config', { outer: true, common: 'outer' }).intercept('config', { inner: true, common: 'inner' })
  const consumer = plugin(scope, 'consumer', p => { resolved = p.config[Service.resolveConfig]({ base: true, common: 'base' }, { head: true, common: 'head' }) }, { config: { inject: true, common: 'inject' } }); await consumer.await()
  assert.deepEqual(resolved, { base: true, outer: true, inner: true, inject: true, head: true, common: 'head' })
  await consumer.dispose(); await provider.dispose(); return { resolved }
})
test('C7-02', 'shared service effects are attributed to asynchronous caller contexts', ['D-06'], async () => {
  const ctx = root(), events = []
  class Timer extends Service { constructor(p) { super(p, 'timer') } register(name) { return this.ctx.effect(() => () => events.push(name)) } }
  const provider = plugin(ctx, 'timer', p => { new Timer(p) }); await provider.await()
  const first = plugin(ctx, 'first', async p => { await Promise.resolve(); p.timer.register('first') }, ['timer'])
  const second = plugin(ctx, 'second', async p => { p.timer.register('second'); await Promise.resolve() }, ['timer']); await settle(first, second)
  await first.dispose(); assert.deepEqual(events, ['first']); assert.equal(provider.state, FiberState.ACTIVE); await second.dispose(); assert.deepEqual(events, ['first', 'second']); await provider.dispose()
  return { caller_cleanup: events }
})

let failed = 0
for (const entry of tests) {
  log(entry.id, 'begin', { coverage: entry.coverage, deviations: entry.deviations })
  try { const data = await entry.body(); log(entry.id, 'result', { outcome: 'observed', coverage: entry.coverage, deviations: entry.deviations, data }) }
  catch (error) { failed++; log(entry.id, 'result', { outcome: 'failed', coverage: entry.coverage, deviations: entry.deviations, error: String(error), stack: error.stack }) }
}
process.exitCode = failed ? 1 : 0
