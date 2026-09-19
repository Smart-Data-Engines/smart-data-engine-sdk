"""Concurrency trials preserve the original four-worker profile and separate identities."""

from collections import Counter

import pytest
from qualification.run import workers


def test_default_profile_keeps_both_original_languages_and_refresh_policies() -> None:
    assert workers(2) == (
        (1, "python", 500),
        (2, "python", 0),
        (3, "typescript", 500),
        (4, "typescript", 0),
    )


@pytest.mark.parametrize("count", [1, 4, 16])
def test_scaled_profiles_keep_equal_languages_and_do_not_collide_with_seed(count: int) -> None:
    layout = workers(count)
    assert Counter(language for _, language, _ in layout) == {"python": count, "typescript": count}
    assert len({identity for identity, _, _ in layout}) == 2 * count
    assert all(0 < identity < 99 and delay in (0, 500) for identity, _, delay in layout)
    assert {language for _, language, delay in layout if delay == 500} == {"python", "typescript"}


@pytest.mark.parametrize("count", [0, -1, 17, True, 1.5])
def test_unbounded_or_coerced_worker_counts_are_refused(count: int) -> None:
    with pytest.raises(ValueError):
        workers(count)
