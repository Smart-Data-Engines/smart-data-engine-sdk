"""The operator's chunk: optional in its configuration, bounded, and handed to the cutover.

``chunk_rows`` is how many rows a cutover's repair and frozen comparison move at once
(docs/local-cutover.md). A value outside 1 to 20 000 is refused before anything connects.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from sde_operator.__main__ import main


def _config(tmp_path: Path, **extra: Any) -> Path:
    config = {
        "protocol": 1,
        "project_id": "1" * 32,
        "model": {"entities": []},
        "public_keys": {"primary": "AAAA"},
        "engines": {},
        **extra,
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    return path


@pytest.mark.parametrize("value", [0, -1, 20001, 1.5, True, "1000", None])
def test_a_chunk_outside_its_bounds_is_refused_before_anything_connects(
    value: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    capsys.readouterr()
    path = _config(tmp_path, chunk_rows=value)
    code = main(["--project-dir", str(tmp_path), "--config", str(path), "resume"])
    assert code == 2
    assert "chunk_rows is a whole number of rows from 1 to 20000" in capsys.readouterr().err


def test_an_unknown_key_beside_it_is_still_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    capsys.readouterr()
    path = _config(tmp_path, chunk_rows=5000, chunk_bytes=1)
    assert main(["--project-dir", str(tmp_path), "--config", str(path), "resume"]) == 2
    assert "unsupported or incomplete" in capsys.readouterr().err


def test_a_chunk_in_bounds_reaches_the_cutover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Up to the engines: the configured chunk is what the cutover is built with."""
    import sde_operator.__main__ as cli

    seen: dict[str, Any] = {}

    class Recorded(Exception):
        pass

    def built(*_args: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        raise Recorded

    monkeypatch.setattr(cli, "LocalCutover", built)
    monkeypatch.setattr(cli, "model_from_neutral", lambda _raw: object())
    monkeypatch.setattr(cli, "adapter", lambda _dialect: _NoEngine)
    monkeypatch.setenv("SDE_CHUNK_TEST_OP", "unused")
    monkeypatch.setenv("SDE_CHUNK_TEST_APP", "unused")
    engines = {"pg": {"dialect": "postgres", "operator_dsn_env": "SDE_CHUNK_TEST_OP",
                      "runtime_dsn_envs": ["SDE_CHUNK_TEST_APP"]}}
    path = _config(tmp_path, chunk_rows=5000, engines=engines)
    with pytest.raises(Recorded):
        main(["--project-dir", str(tmp_path), "--config", str(path), "resume"])
    assert seen["chunk_rows"] == 5000


class _NoEngine:
    def __init__(self, _dsn: str) -> None:
        pass

    def connect(self) -> None:
        pass

    def close(self) -> None:
        pass
