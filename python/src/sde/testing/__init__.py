"""Support for running the shared conformance vectors, and for testing an adapter.

Two modules, both public and both narrow. :mod:`sde.testing.loader` turns a vector's neutral
declaration into a model, which every implementation needs because a vector cannot contain one
language's classes. :mod:`sde.testing.memory` is an in-memory engine, which every implementation
needs because the ``migration/`` vectors pin behaviour that only happens against one.
"""

from __future__ import annotations

from .loader import model_from_neutral
from .memory import MemoryEngine, Recorded, engines_from

__all__ = ["MemoryEngine", "Recorded", "engines_from", "model_from_neutral"]
