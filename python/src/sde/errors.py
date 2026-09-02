"""Error hierarchy, organised by *when* the problem is detectable.

That grouping is deliberate. A client should be able to tell from the exception type whether the
problem is in their declaration (found at import time, before anything runs), in the shape of their
model against a placement (found when the model is planned, still before traffic), or in the world
(found at runtime, and therefore something their code has to handle).

:class:`~sde.canonical.CanonicalError` deliberately does not live here. ``canonical.py`` imports
nothing from the rest of the package, because it is the module every other language port has to
reproduce first and a self-contained file is easier to port and to audit.
"""

from __future__ import annotations

__all__ = [
    "DeclarationError",
    "EngineError",
    "MapError",
    "MapRolledBack",
    "ModelPlanningError",
    "SdeError",
]


class SdeError(Exception):
    """Base for everything this library raises on purpose."""


class DeclarationError(SdeError):
    """The declared model is not a model.

    Raised while the declaration is being read, so at import time in practice: an unmapped Python
    type, a reference to an unknown entity, an atomicity declaration naming something that is not an
    entity. The message names the declaration, never a line inside this library, because the reader
    has to fix their code and our stack frames do not help them.
    """


class ModelPlanningError(SdeError):
    """The model is valid but what is being asked of it is not possible under a placement.

    A query that would join across engines, or a transaction spanning two colocation groups. Raised
    when the model is planned rather than when the query runs, which is the whole point: this class
    of mistake is a design error and should surface in a test run, not in production at the moment a
    customer triggers that code path. The message says which entities would have to share a group.
    """


class MapError(SdeError):
    """The placement map cannot be trusted or cannot be used.

    A bad signature, a map produced for a different model version, an unknown contract version. All
    of these refuse rather than degrade: the map decides where data is written, so guessing at a
    difference is the one thing that must never happen.
    """


class MapRolledBack(MapError):
    """This map is older than one already applied against these engines.

    A subclass rather than a plain :class:`MapError`, because it is the one map refusal a client may
    reasonably want to handle: it says the document is authentic and out of date, not that it is
    wrong. Everything else in this hierarchy says the map cannot be trusted; this one says it can
    be, and that trusting it would undo something.
    """


class EngineError(SdeError):
    """A backend refused or failed, and the client's code has to know.

    Deliberately not swallowed. Internal problems in this library are swallowed and logged, because
    a profiling or routing bug must not take down someone's application - but a write that did not
    happen is not an internal problem, and reporting success for it would be the worst thing this
    library could do.
    """
