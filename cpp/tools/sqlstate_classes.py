"""Writes the class name psycopg gives each SQLSTATE, for the C++ adapter's `sde.write.failed`.

The reference logs a failed write with `error=type(exc).__name__`, the name psycopg derives from the
SQLSTATE, so an alert that keys on it works for both libraries. libpq reports only the code, so the
names are generated from psycopg's own table rather than written by hand:

    python cpp/tools/sqlstate_classes.py > cpp/src/engines/postgres/sqlstate.inc
"""

import psycopg
import psycopg.errors as errors

exact = sorted((code, cls.__name__) for code, cls in errors._sqlcodes.items() if len(code) == 5 and code[:2].isalnum() and code == code.upper() and any(c.isdigit() for c in code))
base = sorted((prefix, cls.__name__) for prefix, cls in errors._base_exc_map.items())
print(f"// Generated from psycopg {psycopg.__version__} by cpp/tools/sqlstate_classes.py; not edited.")
print("// The class name psycopg raises for each SQLSTATE, then for each prefix of an unknown one.")
print("inline constexpr std::pair<std::string_view, std::string_view> kExact[] = {")
for code, name in exact:
    print(f'    {{"{code}", "{name}"}},')
print("};")
print("inline constexpr std::pair<std::string_view, std::string_view> kPrefix[] = {")
for prefix, name in base:
    print(f'    {{"{prefix}", "{name}"}},')
print("};")
