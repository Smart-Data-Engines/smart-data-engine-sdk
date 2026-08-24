"""What the distribution has to contain, checked here rather than discovered by a consumer.

This file exists because of a gap that was invisible from inside the repository. This package is
annotated throughout and its own CI runs ``mypy --strict``, so from here the types looked fine. From
*outside* they did not exist: without a PEP 561 marker a consumer's type checker reports ``module
is installed, but missing library stubs or py.typed marker`` and silently treats everything as
``Any``.

That is worse than shipping no annotations. It is shipping annotations only we benefit from, in a
library whose entire argument is that a client can verify our privacy invariants by reading it - and
the first person to hit it was our own control plane, on the day it was created.
"""

from __future__ import annotations

from pathlib import Path

import sde

PACKAGE = Path(sde.__file__).resolve().parent


def test_the_pep_561_marker_is_next_to_the_package() -> None:
    marker = PACKAGE / "py.typed"
    assert marker.exists(), (
        "src/sde/py.typed is missing. Without it every consumer's type checker treats this library "
        "as untyped, which is invisible from inside this repository because our own mypy run reads "
        "the source directly."
    )
    assert marker.stat().st_size == 0, (
        "the marker is a marker; PEP 561 gives its contents no meaning"
    )
