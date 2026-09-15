"""Native TLS qualification worker, also runnable from an installed SDK wheel."""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlencode

from sde.engines.clickhouse import ClickHouseEngine
from sde.engines.postgres import PostgresEngine
from sde.errors import EngineError
from sde.placement import PhysicalLayout


def main() -> None:
    config = json.loads(Path(os.environ["SDE_TLS_TEST"]).read_text())
    material = config["material"]
    table = "tls_python_" + uuid.uuid4().hex
    row = {
        "id": "synthetic",
        "big": 9_007_199_254_740_993,
        "at": datetime(2026, 9, 15, 10, 0, 0, 123456, tzinfo=UTC),
        "label": "Zażółć gęślą jaźń",
        "money": Decimal("12.34"),
    }
    for dialect, scheme, secure_option in (
        ("postgres", "postgresql", False),
        ("clickhouse", "https", False),
        ("clickhouse", "clickhouse", True),
    ):

        def dsn(
            ca: str,
            dialect: str = dialect,
            scheme: str = scheme,
            secure_option: bool = secure_option,
        ) -> str:
            options = (
                {"sslmode": "verify-full", "sslrootcert": ca}
                if dialect == "postgres"
                else {"ca_cert": ca}
            )
            if secure_option:
                options["secure"] = "true"
            user = "postgres" if dialect == "postgres" else "default"
            return (
                f"{scheme}://{user}:{config['password']}@127.0.0.1:"
                f"{config[dialect + '_port']}/sde?{urlencode(options)}"
            )

        adapter = PostgresEngine if dialect == "postgres" else ClickHouseEngine
        columns = (
            {
                "id": "text",
                "big": "bigint",
                "at": "timestamptz",
                "label": "text",
                "money": "numeric(12,2)",
            }
            if dialect == "postgres"
            else {
                "id": "String",
                "big": "Int64",
                "at": "DateTime64(6, 'UTC')",
                "label": "String",
                "money": "Decimal(12, 2)",
            }
        )
        current_table = table + ("_secure" if secure_option else "")
        with adapter(dsn(material["ca"])) as engine:
            engine.ensure_schema(
                PhysicalLayout(tables={"A": current_table}, columns={"A": columns}),
                keys={"A": ["id"]},
            )
            engine.insert(current_table, row)
            engine.insert_many(current_table, [{**row, "id": "two"}, {**row, "id": "three"}])
            assert engine.get(current_table, {"id": "synthetic"}) == row
            assert engine.count(current_table) == 3
            if dialect == "postgres":
                assert engine._cx.execute(
                    "SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()"
                ).fetchone() == (True,)
            print(
                json.dumps(
                    {
                        "language": "python",
                        "dialect": dialect,
                        "scheme": scheme,
                        "secure_option": secure_option,
                        "rows": 3,
                        "exact_values": True,
                    }
                )
            )
        bad = adapter(dsn(material["other_ca"]))
        try:
            try:
                bad.connect()
            except EngineError:
                print(
                    json.dumps(
                        {
                            "language": "python",
                            "dialect": dialect,
                            "scheme": scheme,
                            "wrong_ca_refused": True,
                        }
                    )
                )
            else:
                raise AssertionError("An unrelated CA was accepted")
        finally:
            bad.close()


if __name__ == "__main__":
    main()
