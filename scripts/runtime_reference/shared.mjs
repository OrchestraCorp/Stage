import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { registerHooks } from 'node:module'
import { pathToFileURL } from 'node:url'
import { join } from 'node:path'
const reference = process.argv[2]
registerHooks({ resolve(specifier, context, nextResolve) {
  if (specifier === '@deepseek-ai/cosmokit') return { shortCircuit: true, url: pathToFileURL(join(reference, 'vendor/cosmokit/src/index.ts')).href }
  return nextResolve(specifier, context)
}})
const { Context, FiberState, Service } = await import(pathToFileURL(join(reference, 'vendor/cordis/src/index.ts')).href)
const cases = JSON.parse(readFileSync(new URL('shared-cases.json', import.meta.url)))

class Scenario {
  constructor(description) {
    this.description = description
    this.ctx = new Context(); this.ctx.logger.error = () => {}
    this.scopes = {root: this.ctx}; this.fibers = {}; this.contexts = {}; this.stats = {}; this.availability = {}
    this.effects = {}; this.gates = {}; this.tasks = {}; this.log = []; this.observations = []; this.labels = {}; this.listenerHandles = {}
    this.taskDone = {}
  }
  gate(name) {
    if (!this.gates[name]) { let resolve; const promise = new Promise(r => resolve = r); this.gates[name] = {promise, resolve} }
    return this.gates[name]
  }
  inverse(descriptor, ctx) {
    const before = () => {
      if (descriptor.read) this.log.push({phase: 'before', value: ctx[descriptor.read], public: this.ctx.get(descriptor.read) ?? null})
      else if (descriptor.release) this.log.push(descriptor.label + '.enter')
      else this.log.push(descriptor.label)
      if (descriptor.entered) this.gate(descriptor.entered).resolve()
      if (descriptor.error) throw Error('fixture cleanup failure')
    }
    return descriptor.release ? async () => {
      before(); await this.gate(descriptor.release).promise
      if (descriptor.read) this.log.push({phase: 'after', value: ctx[descriptor.read]})
      else this.log.push(descriptor.label + '.end')
    } : before
  }
  listener(descriptor) {
    const behavior = descriptor.behavior ?? 'log'
    const begin = () => {
      this.log.push(descriptor.label ?? behavior)
      if (descriptor.entered) this.gate(descriptor.entered).resolve()
      if (behavior === 'error') throw Error('fixture listener failure')
    }
    if (behavior === 'around') return async next => {
      this.log.push(descriptor.label + '.before')
      if (descriptor.veto) return descriptor.value
      const value = await next(); this.log.push(descriptor.label + '.after'); return value + (descriptor.add ?? 0)
    }
    if (behavior === 'wait') return async () => { begin(); await this.gate(descriptor.release).promise; this.log.push(descriptor.label + '.end') }
    return () => { begin(); if (behavior === 'remove') this.listenerHandles[descriptor.target](); return descriptor.value }
  }
  load(action, parent) {
    const name = action.id, scenario = this
    this.stats[name] = {runs: 0, cleaned: 0, calls: 0}; this.availability[name] = action.available ?? true
    const inject = action.inject ? Object.fromEntries((action.requires ?? []).map(key => [key, action.inject[key] ?? null])) : action.requires ?? []
    this.fibers[name] = (parent ?? this.scopes[action.scope ?? 'root']).plugin({name, inject, apply: async p => {
      this.contexts[name] = p; const data = this.stats[name]; data.runs++
      if (action.provide) {
        if (action.service_type === 'caller') {
          class CallerService extends Service { constructor(ctx) { super(ctx, action.provide) } register(label) { this.ctx.effect(() => () => scenario.log.push(label)) } }
          new CallerService(p)
        } else if (action.service_type === 'query') {
          p.provide(action.provide, {async query() { scenario.gate('query.entered').resolve(); await scenario.gate('query.release').promise; scenario.log.push('query.end') }})
        } else if (action.service_type === 'config') {
          class ConfigService extends Service { constructor(ctx) { super(ctx, action.provide); if (action.merge) this.Config = {merge: (...layers) => ({sources: layers.map(layer => layer.common)})} } }
          new ConfigService(p)
        } else p.provide(action.provide, action.value, () => this.availability[name])
      }
      if (action.listen) p.on('probe', () => data.calls++)
      for (const descriptor of action.listeners ?? []) { const handle = p.on(descriptor.event ?? 'probe', this.listener(descriptor)); if (descriptor.id) this.listenerHandles[descriptor.id] = handle }
      if (action.read) { data.discovered = p.get(action.read) ?? null; try { p[action.read]; data.denied = false } catch { data.denied = true } }
      if (action.capture_required) (data.values ??= []).push(p[action.capture_required])
      if (action.register_call) { await Promise.resolve(); p[action.register_call.service].register(action.register_call.label) }
      if (action.config_for) { const d = action.config_for; data.config = p[d.key][Service.resolveConfig](d.base, d.head) }
      for (const descriptor of action.effects ?? []) this.effects[name + ':' + descriptor.label] = p.effect(() => this.inverse(descriptor, p))
      if (action.generator) this.effects[name + ':generator'] = p.effect(function* () { for (const descriptor of action.generator) yield scenario.inverse(descriptor, p) })
      for (const child of action.children ?? []) this.load(child, p)
      if (action.setup_entered) this.gate(action.setup_entered).resolve()
      if (action.setup_release) await this.gate(action.setup_release).promise
      if (action.fail_setup) throw Error('fixture setup failure')
      const inverse = action.cleanup ? this.inverse(action.cleanup, p) : null
      return async () => { data.cleaned++; if (inverse) await inverse() }
    }})
  }
  async dispatch(action) {
    const name = action.event ?? 'probe', mode = action.mode
    let result
    if (mode === 'waterfall') result = await this.ctx.waterfall(name, async () => { this.log.push('terminal'); return action.terminal })
    else result = await this.ctx[mode](name)
    return result ?? null
  }
  async execute() {
    for (const action of this.description.actions) {
      const op = action.op
      if (op === 'scope') this.scopes[action.id] = this.scopes[action.base ?? 'root'].isolate(action.key, this.labels[action.label] ??= Symbol(action.label))
      else if (op === 'intercept') this.scopes[action.id] = this.scopes[action.base ?? 'root'].intercept(action.key, action.value)
      else if (op === 'load') this.load(action)
      else if (op === 'ready') for (const id of action.ids) { await this.fibers[id].await(); assert.equal(this.fibers[id].state, FiberState.ACTIVE) }
      else if (op === 'ready_failed') await assert.rejects(this.fibers[action.id].await(), /fixture setup failure/)
      else if (op === 'duplicate_failed') { await assert.rejects(this.fibers[action.id].await(), /registered/); this.observations.push({duplicate_rejected: true}) }
      else if (op === 'pending') { await this.fibers[action.id].await(); assert.equal(this.fibers[action.id].state, FiberState.PENDING); this.observations.push({pending: action.id}) }
      else if (op === 'wait_absent') { while (this.ctx.get(action.key) !== undefined) await Promise.resolve() }
      else if (op === 'retire' || op === 'retire_begin') {
        const closing = Promise.resolve(this.fibers[action.id].dispose())
        if (op === 'retire_begin') { const task = action.task ?? 'closing'; this.tasks[task] = closing.finally(() => this.taskDone[task] = true) }
        else { await closing; if (action.observe_failure) this.observations.push({cleanup_failed: false}) }
      }
      else if (op === 'restart') { const before = this.fibers[action.id].uid; await this.fibers[action.id].restart(); if (action.observe) this.observations.push({activation_identity_changed: this.fibers[action.id].uid !== before}) }
      else if (op === 'effect_close' || op === 'effect_close_begin') {
        const closing = this.effects[action.effect]()
        if (action.observe_shape) this.observations.push({returns_awaitable: !!closing?.then})
        if (op === 'effect_close_begin') this.tasks[action.task ?? 'effect'] = Promise.resolve(closing)
        else await closing
      }
      else if (op === 'wait_gate') await this.gate(action.id).promise
      else if (op === 'release') this.gate(action.id).resolve()
      else if (op === 'join') {
        if (action.expect_error) {
          try { await this.tasks[action.task]; assert.fail('expected dispatch failure') }
          catch (error) { if (!(error instanceof AggregateError)) throw error; this.observations.push({raised: true, errors: error.errors.length}) }
        } else await this.tasks[action.task]
      }
      else if (op === 'observe_task') this.observations.push({pending: !this.taskDone[action.task]})
      else if (op === 'await_task_timeout') { const outcome = await Promise.race([this.tasks[action.task].then(() => 'done'), new Promise(resolve => setTimeout(() => resolve('timeout'), 10))]); this.observations.push({task_outcome: outcome}) }
      else if (op === 'call_begin') this.tasks[action.task] = this.ctx.get(action.key).query()
      else if (op === 'availability') { this.availability[action.id] = action.value; this.ctx.reflect.notify(['db']) }
      else if (op === 'emit') this.ctx.emit('probe')
      else if (op === 'dispatch' || op === 'dispatch_begin') {
        const dispatch = this.dispatch(action)
        if (op === 'dispatch_begin') this.tasks[action.task] = dispatch
        else this.observations.push({value: await dispatch})
      }
      else if (op === 'dispatch_shape') { const value = this.ctx.bail(action.event); this.observations.push({returns_explicit_decision: typeof value === 'object' && value !== null}) }
      else if (op === 'get') this.observations.push({value: this.scopes[action.scope ?? 'root'].get(action.key) ?? null})
      else if (op === 'observe') this.observations.push({id: action.id, ...this.stats[action.id]})
      else if (op === 'observe_log') this.observations.push({log: action.sort ? [...this.log].sort() : [...this.log]})
      else if (op === 'stale_effect') { let rejected = false; try { this.contexts[action.id].effect(() => {}) } catch { rejected = true }; this.observations.push({rejected}) }
      else if (op === 'stale_require' || op === 'stale_listener') { let rejected = false; try { if (op === 'stale_require') this.contexts[action.id][action.key]; else this.contexts[action.id].on('probe', () => {}) } catch { rejected = true }; this.observations.push({rejected}) }
      else if (op === 'await_without_provider') { await this.fibers[action.id].await(); this.observations.push({outcome: this.fibers[action.id].state === FiberState.PENDING ? 'pending' : 'active'}) }
      else throw Error('unsupported shared operation: ' + op)
    }
    for (const fiber of Object.values(this.fibers).reverse()) await fiber.dispose()
    return this.observations
  }
}
let failed = 0
for (const description of cases) {
  try { const observations = await new Scenario(description).execute(); process.stdout.write(JSON.stringify({scenario_id: description.id, scope: description.scope, outcome: 'observed', observations}) + '\n') }
  catch (error) { failed++; process.stdout.write(JSON.stringify({scenario_id: description.id, outcome: 'failed', error: String(error), stack: error.stack}) + '\n') }
}
process.exitCode = failed ? 1 : 0
