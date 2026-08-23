/**
 * Error hierarchy, organised by *when* the problem is detectable.
 *
 * A caller should be able to tell from the class whether the problem is in their declaration (found
 * when the model is built, before anything runs), in the shape of their model against a placement
 * (found when the model is planned, still before traffic), or in the world.
 *
 * `CanonicalError` deliberately lives in `canonical.ts`, which imports nothing from the rest of the
 * package: it is the module a new language port reproduces first, and a self-contained file is easier
 * to port and to audit.
 */

export class SdeError extends Error {
  override readonly name: string = 'SdeError'
}

/**
 * The declared model is not a model.
 *
 * An unmapped type, a reference to an unknown entity, an atomicity declaration naming something that
 * is not an entity. The message names the declaration, never a line inside this library, because the
 * reader has to fix their code and our stack frames do not help them.
 */
export class DeclarationError extends SdeError {
  override readonly name = 'DeclarationError'
}

/**
 * The model is valid but what is being asked of it is not possible under a placement.
 *
 * Raised when the model is planned rather than when the query runs. That distinction is the point:
 * this class of mistake is a design error and belongs in a test run, not in production at the moment
 * a customer triggers that code path.
 */
export class ModelPlanningError extends SdeError {
  override readonly name = 'ModelPlanningError'
}

/**
 * The placement map cannot be trusted or cannot be used.
 *
 * A bad signature, a map produced for a different model version, an unknown contract version. All of
 * these refuse rather than degrade: the map decides where data is written, so guessing at a
 * difference is the one thing that must never happen.
 */
export class MapError extends SdeError {
  override readonly name = 'MapError'
}

/**
 * A backend refused or failed, and the caller has to know.
 *
 * Deliberately not swallowed. Internal problems in this library are swallowed and logged, because a
 * bug of ours must not take down someone's application - but a write that did not happen is not an
 * internal problem, and reporting success for it would be the worst thing this library could do.
 */
export class EngineError extends SdeError {
  override readonly name = 'EngineError'
}
