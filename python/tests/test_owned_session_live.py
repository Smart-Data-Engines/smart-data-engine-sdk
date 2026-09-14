"""The owned factory uses native runtime credentials and closes only its own connection."""

from __future__ import annotations

from typing import Any

from test_runtime_privileges_live import roles as roles

import sde
from sde.testing.loader import model_from_neutral


def test_owned_native_session_connects_writes_and_releases_its_adapter(roles: Any) -> None:
    model = model_from_neutral(
        {
            "entities": [
                {"name": "Event", "fields": [{"name": "id", "type": "int64"}], "key": ["id"]}
            ]
        }
    )
    dialect = roles.operator.dialect
    placement = sde.load_map(
        {
            "contract": 4,
            "project_id": "1" * 32,
            "model_version": model.version,
            "map_version": 1,
            "groups": {
                "Event": {
                    "write_epoch": 1,
                    "source": {
                        "id": "source",
                        "engine": "db",
                        "layout": {
                            "tables": {"Event": "events"},
                            "columns": {
                                "Event": {"id": "bigint" if dialect == "postgres" else "Int64"}
                            },
                        },
                    },
                }
            },
        },
        model=model,
    )
    sde.prepare_schema(model, placement, {"db": roles.operator}, project_id="1" * 32)
    roles.grant("events")
    made = []

    def factory() -> Any:
        engine = type(roles.runtime)(roles.runtime._dsn)
        made.append(engine)
        return engine

    with sde.Session.connect(model, placement, {"db": factory}, project_id="1" * 32) as session:
        session.save("Event", {"id": 1})
        assert session.get("Event", {"id": 1}) == {"id": 1}
    assert len(made) == 1
    assert (made[0]._conn if dialect == "postgres" else made[0]._client) is None
    assert roles.runtime.get("events", {"id": 1}) is not None
