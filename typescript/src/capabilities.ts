/**
 * Asking an engine adapter whether it will answer a call, which is not the same as its type.
 *
 * Two optional capabilities decide whether an engine takes part in something: keeping the
 * forward-only bookkeeping, and taking part in a migration. Both are asked for by **name of the
 * members required**, with ordinary property access, and the reason is worth carrying across from
 * the reference implementation even though this runtime cannot reproduce its defect.
 *
 * There, the obvious spelling was `isinstance(engine, Protocol)` - and a `runtime_checkable`
 * protocol resolves its members with `hasattr` up to Python 3.11 and with `inspect.getattr_static`
 * from 3.12, the second of which ignores `__getattr__`. An object forwarding to a wrapped adapter
 * therefore passed on one interpreter and failed on the next: the same client, the same wrapper, a
 * different answer. The consequence was two wrong diagnoses shipped as helpful messages, both
 * naming the client's engine for a property of their own wrapper, and one of them refusing a
 * migration outright.
 *
 * TypeScript has no such split - property access is property access, and it goes through the
 * prototype chain and through a `Proxy` trap alike - so wrapping an adapter works here without
 * anything special. That is exactly why the reason is written down rather than the mechanism
 * assumed: the *question* is the durable part. Will this object respond to these calls?
 */

/**
 * Whether every member named here can be reached on this object.
 *
 * **Presence, not callability, and that is the reference implementation's decision rather than
 * mine.** A member that exists and is not callable fails at the call with a message naming it,
 * which is a better failure than a capability check that quietly answers "no" and sends the reader
 * to look at their engine. The first version of this file asked `typeof === 'function'`, which is
 * the idiomatic spelling and disagreed with the reference on exactly that input - an adapter with a
 * null method would have been "cannot take part" here and "takes part, then crashes with the name
 * of the method" there. One contract, two behaviours, no compilation error.
 *
 * Read through the property rather than with `in`, because `in` consults a `Proxy`'s `has` trap and
 * a forwarding wrapper usually only implements `get`. That is the same wrapper the reference's
 * module docstring is about, and the reason this question is asked by name at all.
 */
export function satisfies(engine: unknown, members: readonly string[]): boolean {
  if (typeof engine !== 'object' || engine === null) return false
  const target = engine as Record<string, unknown>
  return members.every((name) => target[name] !== undefined)
}

/**
 * The members an engine needs for the forward-only map check.
 *
 * A list rather than an interface used with a type guard, because an interface is erased before the
 * code runs and this question is asked at runtime about an object a client constructed. The list and
 * the interface are kept in agreement by the compiler - see `WATERMARK_STORE_IS_TOTAL` in
 * `watermark.ts`.
 */
export const WATERMARK_MEMBERS = ['mapWatermark', 'recordMapVersion'] as const

/** The members an engine needs to take part in a migration. */
export const MIGRATABLE_MEMBERS = [
  'keyRange',
  'nthKey',
  'copyIn',
  'count',
  'get',
  'backfillMarker',
  'recordBackfillMarker',
] as const
