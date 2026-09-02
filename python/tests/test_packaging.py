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
from types import ModuleType

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


def test_every_name_in_all_is_actually_importable() -> None:
    """`__all__` is a promise about `from sde import *`, and nothing else here checked it.

    Found by trying: `ALSO_WRITE_SINCE` was added to `__all__` when the map contract gained the key
    and never imported, so `sde.ALSO_WRITE_SINCE` raised and `from sde import *` would have too.
    Neither ruff nor mypy --strict reported it, which is the whole reason this test exists rather
    than a note saying to be careful.
    """
    missing = sorted(name for name in sde.__all__ if not hasattr(sde, name))
    assert not missing, f"named in __all__ and not importable: {missing}"


def test_nothing_public_is_left_out_of_all() -> None:
    """The other direction: a public name reachable by attribute and absent from `__all__`.

    Submodules are excluded by *type* rather than by a list of their names, and the first version
    of this test did it by list - which passed alone and failed in the full run, because importing
    `sde.engines` anywhere binds it as an attribute of `sde`. An order-dependent test is worse than
    no test: it reads as flaky and gets a re-run.
    """
    public = {
        name
        for name, value in vars(sde).items()
        if not name.startswith("_") and not isinstance(value, ModuleType)
    }
    # `annotations` is the __future__ feature object, not a name anybody imports from us.
    public.discard("annotations")
    assert public - set(sde.__all__) == set(), sorted(public - set(sde.__all__))
