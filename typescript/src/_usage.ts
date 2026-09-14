/** Local operation/transaction ownership across async execution chains. */
import { AsyncLocalStorage } from 'node:async_hooks'
import { ModelPlanningError, ResourceBusy, ResourceClosed } from './errors.js'

interface TransactionScope {
  readonly gate: UsageGate
  readonly parent: TransactionScope | undefined
  readonly session: object | undefined
  active: boolean
}
const sessionOwner = new AsyncLocalStorage<object>()
const operation = new AsyncLocalStorage<object>()
const transaction = new AsyncLocalStorage<TransactionScope>()

export function checkScope(): void {
  let scope = transaction.getStore()
  while (scope !== undefined) {
    if (!scope.active) throw new ResourceClosed('the inherited transaction scope has already ended')
    scope = scope.parent
  }
}

export function withSession<T>(owner: object, body: () => T): T {
  checkScope()
  const logical = sessionScope.getStore()
  if (logical !== undefined && logical.active && !logical.usage.isOwner(owner)) {
    throw new ResourceBusy('another session owns the current transaction scope')
  }
  const scope = transaction.getStore()
  if (scope !== undefined && scope.session !== owner) {
    throw new ResourceBusy('another session owns the current transaction scope')
  }
  return sessionOwner.run(owner, body)
}

export class UsageGate {
  private operationOwner: object | undefined
  private operationDepth = 0
  private transactionOwner: TransactionScope | undefined

  private checkTransaction(): void {
    checkScope()
    if (this.transactionOwner === undefined) return
    let scope = transaction.getStore()
    while (scope !== undefined && scope.gate !== this) scope = scope.parent
    if (scope !== this.transactionOwner || scope.session !== sessionOwner.getStore()) {
      throw new ResourceBusy('the connection is owned by another active transaction scope')
    }
  }

  async operation<T>(body: () => Promise<T>): Promise<T> {
    this.checkTransaction()
    const owner = operation.getStore() ?? {}
    if (this.operationOwner !== undefined && this.operationOwner !== owner) {
      throw new ResourceBusy('the connection already has an operation in progress')
    }
    this.operationOwner = owner
    this.operationDepth++
    return operation.run(owner, async () => {
      let failed = false
      try {
        return await body()
      } catch (error) {
        failed = true
        throw error
      } finally {
        this.operationDepth--
        if (this.operationDepth === 0) this.operationOwner = undefined
        if (!failed) checkScope()
      }
    })
  }

  async transaction<T>(body: () => Promise<T>): Promise<T> {
    this.checkTransaction()
    if (this.operationOwner !== undefined) {
      throw new ResourceBusy('a transaction cannot begin while an operation is in progress')
    }
    const previous = this.transactionOwner
    const scope: TransactionScope = {
      gate: this, parent: transaction.getStore(), session: sessionOwner.getStore(), active: true,
    }
    this.transactionOwner = scope
    return transaction.run(scope, async () => {
      try { return await body() }
      finally { scope.active = false; this.transactionOwner = previous }
    })
  }

  seal(): void { if (this.transactionOwner !== undefined) this.transactionOwner.active = false }

  idle(): void {
    if (this.operationOwner !== undefined) {
      if (this.transactionOwner !== undefined) this.transactionOwner.active = false
      throw new ResourceBusy('finish all operations before leaving the transaction')
    }
  }
}

interface SessionScope { readonly usage: SessionUsage; readonly group: string; active: boolean }
const sessionScope = new AsyncLocalStorage<SessionScope>()

export class SessionUsage {
  private closed = false
  private busy = false
  private transactionScope: SessionScope | undefined
  constructor(private readonly owner: object) {}
  isOwner(owner: object): boolean { return this.owner === owner }

  check(): void {
    checkScope()
    const scope = sessionScope.getStore()
    if (this.closed || (scope !== undefined && !scope.active)) {
      throw new ResourceClosed('the session or inherited transaction scope has ended')
    }
    if (this.transactionScope !== undefined && scope !== this.transactionScope) {
      throw new ResourceBusy('the session belongs to another active transaction context')
    }
  }

  group(name: string): void {
    this.check()
    if (this.transactionScope !== undefined && this.transactionScope.group !== name) {
      throw new ModelPlanningError("the operation is outside the active transaction's colocation group")
    }
  }

  async operation<T>(body: () => Promise<T>): Promise<T> {
    this.check()
    if (this.busy) throw new ResourceBusy('the session already has an operation in progress')
    this.busy = true
    let failed = false
    try {
      return await withSession(this.owner, async () => {
        try { return await body() }
        catch (error) { failed = true; throw error }
        finally { if (!failed) this.check() }
      })
    } finally { this.busy = false }
  }

  async transaction<T>(group: string, body: () => Promise<T>): Promise<T> {
    this.group(group)
    if (this.busy) throw new ResourceBusy('finish the session operation before beginning a transaction')
    const previous = this.transactionScope
    const scope: SessionScope = { usage: this, group, active: true }
    this.transactionScope = scope
    try { return await sessionScope.run(scope, () => withSession(this.owner, body)) }
    finally { scope.active = false; this.transactionScope = previous }
  }

  idle(): void {
    if (this.busy) {
      if (this.transactionScope !== undefined) this.transactionScope.active = false
      throw new ResourceBusy('finish all session operations before leaving the transaction')
    }
  }

  async maintenance<T>(body: () => Promise<T>): Promise<T> {
    this.check()
    if (this.transactionScope !== undefined) throw new ResourceBusy('run migration helpers outside application transactions')
    return this.operation(body)
  }

  seal(): void { if (this.transactionScope !== undefined) this.transactionScope.active = false }

  close(): void {
    if (this.busy || this.transactionScope !== undefined) {
      throw new ResourceBusy('cannot close a session while its operation or transaction is active')
    }
    this.closed = true
  }
}

const sessions = new WeakMap<object, SessionUsage>()
export function registerSession(owner: object, usage: SessionUsage): void { sessions.set(owner, usage) }
export async function withReader<T>(reader: object, body: () => Promise<T>): Promise<T> {
  const usage = sessions.get(reader)
  if (usage === undefined) return body()
  return usage.maintenance(body)
}
