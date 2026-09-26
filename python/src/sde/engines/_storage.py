"""The ``system.parts`` columns a ClickHouse storage measurement reads - in a module of its own.

Shared by the adapter that reads them, the operator's runtime qualification that admits exactly
this grant, and the starter that gives it; kept apart from the adapter so that importing the
qualification does not import a driver.
"""

from __future__ import annotations

STORAGE_COLUMNS = (
    "database",
    "table",
    "active",
    "bytes_on_disk",
    "secondary_indices_compressed_bytes",
    "secondary_indices_marks_bytes",
)
"""The columns of ``system.parts`` a storage measurement reads, and all a runtime login is granted.

ClickHouse 24.8 refuses ``system.parts`` to a login with only table grants; a column grant on these
names lets it read the parts of the tables it may use and no others (measured). The operator's
runtime qualification admits exactly this grant, and the starter gives it."""
