"""An adapter for Elasticsearch: a document per row, an index per entity, and a text index.

What this engine is for, in this library, is ``Session.search``: it supplies the candidates of a
search from an index of its own instead of every row in key order. It also takes writes, reads by
key, bounded scans and exact counts, so a group can live here whole. It has no transactions and no
write generations, so such a group is placed once and not moved (map contract 6, no
``write_epoch``), as on our orderbook engine.

**The layout.** One index per entity, its mapping strict (`dynamic: strict`), every column typed by
:data:`sde.layout.ELASTICSEARCH_TYPES`. Where Elasticsearch's own type does not say how to read a
value back, the neutral type rides in the field's ``meta``: a ``long`` holding microseconds since
the epoch is ``long/timestamptz``, and a decimal is its value times ``10^scale``. A text field
has its search tokens beside it in ``__sde_tokens`` (:data:`sde.schema.TOKENS_FIELD`), computed
by this library with the contract's rule, never by the engine's analyzer.

**The key.** A document's ``_id`` is the SHA-256 of the canonical encoding of its key values as
stored, in key order (format contract 7m). Two libraries write the same document under the same
``_id``, and a second save of one key replaces the first - ``newest_wins``, as in ClickHouse.

**Visibility.** A write returns once it is visible to searches and scans (``refresh=wait_for``),
which costs up to the index's refresh interval, a second by default. A read by key is immediate.

**The transport** is the standard library's HTTP client, one keep-alive connection, TLS verified.
No request is replayed after a lost response: whether a write happened is then unknown, and a
replay could put an older document over a newer one. The connection profile is documented in
``docs/engine-connections.md``.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import http.client
import json
import math
import os
import select
import ssl
import stat
import urllib.parse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from ipaddress import ip_address
from typing import Any, Final
from uuid import UUID

from .._usage import UsageGate, guarded
from ..errors import EngineError
from ..layout import index_name_refusal
from ..logging import log
from ..physical import PhysicalFinding
from ..placement import PhysicalLayout
from ..query import ReadFilter, ReadPlan, read_row
from ..schema import (
    TOKENS_FIELD,
    elasticsearch_mapping,
    elasticsearch_property,
    schema_statements,
)
from ..search import tokens as text_tokens
from ..watermark import WATERMARK_TABLE

__all__ = ["KEYWORD_BYTES", "ElasticsearchEngine", "document", "document_id", "parse_dsn"]

KEYWORD_BYTES: Final = 32_766
"""The longest text a keyword holds, in UTF-8 bytes: Lucene's limit on one term."""

MAX_RESPONSE_BYTES: Final = 256 * 1024 * 1024
"""The most this adapter reads of one response. A page is at most 1001 documents."""

_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
_SCHEMES: Final = {"elasticsearch+https": True, "elasticsearch+http": False}
_OPTIONS: Final = frozenset(
    {"api_key", "ca_cert", "connect_timeout", "send_receive_timeout", "verify"}
)
_CA_BYTES: Final = 1024 * 1024
_TIMEOUT_RANGE: Final = (0.001, 2_147_483.647)


@dataclass(frozen=True)
class Connection:
    """A parsed connection profile. ``authorization`` is the header value, never logged."""

    host: str
    port: int
    tls: bool
    authorization: str | None
    ca_cert: str | None
    connect_timeout: float
    send_receive_timeout: float

    def __repr__(self) -> str:
        return f"Connection(host={self.host!r}, port={self.port}, tls={self.tls})"


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def _seconds(name: str, text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise EngineError(f"{name} is a number of seconds") from None
    low, high = _TIMEOUT_RANGE
    if not math.isfinite(value) or not low <= value <= high:
        raise EngineError(f"{name} is between {low} and {high} seconds")
    return value


def parse_dsn(dsn: str) -> Connection:
    """The connection profile, refused whole on anything it does not define.

    ``elasticsearch+https://USER:PASSWORD@host:9200/?ca_cert=%2Fpath%2Fca.pem`` or
    ``elasticsearch+https://host:9200/?api_key=BASE64``. ``elasticsearch+http`` only to a loopback
    address: Elasticsearch has served TLS by default since 8.0, and a plain connection anywhere
    else would carry the credentials in the clear.
    """
    if not isinstance(dsn, str) or any(c.isspace() or c == "\\" for c in dsn):
        raise EngineError("an Elasticsearch DSN is one URL without whitespace or backslashes")
    parts = urllib.parse.urlsplit(dsn)
    if parts.scheme not in _SCHEMES:
        raise EngineError(f"an Elasticsearch DSN starts {' or '.join(sorted(_SCHEMES))}://")
    tls = _SCHEMES[parts.scheme]
    if parts.fragment or parts.path not in ("", "/"):
        raise EngineError("an Elasticsearch DSN has no path and no fragment")
    try:
        host, port = parts.hostname, parts.port
    except ValueError:
        raise EngineError("the port of an Elasticsearch DSN is a number up to 65535") from None
    if not host or "%" in host:
        raise EngineError("an Elasticsearch DSN names a host, without an IPv6 zone")
    if not tls and not _loopback(host):
        raise EngineError(
            "elasticsearch+http is accepted only to a loopback address; use elasticsearch+https"
        )
    pairs = urllib.parse.parse_qsl(parts.query, keep_blank_values=True, strict_parsing=False)
    options: dict[str, str] = {}
    for name, value in pairs:
        if name not in _OPTIONS:
            raise EngineError(f"unknown Elasticsearch DSN option {name!r}")
        if name in options:
            raise EngineError(f"the Elasticsearch DSN option {name!r} is given twice")
        options[name] = value
    if options.get("verify", "true") != "true":
        raise EngineError("verify can only be true: disabling TLS verification is refused")
    if not tls and ("ca_cert" in options or "verify" in options):
        raise EngineError("ca_cert and verify belong to elasticsearch+https")
    ca = options.get("ca_cert")
    if ca is not None:
        if not os.path.isabs(ca):
            raise EngineError("ca_cert is an absolute path")
        try:
            info = os.stat(ca)
        except OSError:
            raise EngineError("ca_cert names no readable file") from None
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= _CA_BYTES:
            raise EngineError("ca_cert is a nonempty regular file of at most 1 MiB")
    user = urllib.parse.unquote(parts.username) if parts.username is not None else None
    password = urllib.parse.unquote(parts.password) if parts.password is not None else ""
    if user is not None and "api_key" in options:
        raise EngineError("an Elasticsearch DSN gives a user or an api_key, not both")
    authorization = None
    if user is not None:
        if not user or ":" in user:
            raise EngineError("an Elasticsearch user is nonempty and has no colon")
        token = base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
        authorization = f"Basic {token}"
    elif "api_key" in options:
        key = options["api_key"]
        try:
            base64.b64decode(key, validate=True)
        except (binascii.Error, ValueError):
            raise EngineError("api_key is the encoded key Elasticsearch returns") from None
        if not key:
            raise EngineError("api_key is the encoded key Elasticsearch returns")
        authorization = f"ApiKey {key}"
    return Connection(
        host=host,
        port=port if port is not None else 9200,
        tls=tls,
        authorization=authorization,
        ca_cert=ca,
        connect_timeout=_seconds("connect_timeout", options.get("connect_timeout", "10")),
        send_receive_timeout=_seconds(
            "send_receive_timeout", options.get("send_receive_timeout", "30")
        ),
    )


# --- values ----------------------------------------------------------------------------------


def _decimal_parts(kind: str) -> tuple[int, int]:
    digits, scale = kind[len("long/decimal(") : -1].split(",")
    return int(digits), int(scale)


def _micros(value: Any) -> int:
    if not isinstance(value, datetime):
        raise EngineError("a timestamp is a datetime")
    instant = value.replace(tzinfo=UTC) if value.utcoffset() is None else value.astimezone(UTC)
    delta = instant - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def stored(kind: str, value: Any) -> Any:
    """The JSON value a column of ``kind`` keeps for ``value``, exactly, or a refusal."""
    if value is None:
        return None
    if kind == "boolean":
        if not isinstance(value, bool):
            raise EngineError("a boolean field takes a bool")
        return value
    if kind in ("integer", "long"):
        if isinstance(value, bool) or not isinstance(value, int):
            raise EngineError("an integer field takes an int")
        bits = 32 if kind == "integer" else 64
        if not -(2 ** (bits - 1)) <= value < 2 ** (bits - 1):
            raise EngineError(f"the value is outside int{bits}")
        return value
    if kind in ("float", "double"):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise EngineError("a float field takes a number")
        number = float(value)
        if not math.isfinite(number):
            raise EngineError("Elasticsearch stores no NaN or infinity")
        return number
    if kind == "keyword":
        if not isinstance(value, str):
            raise EngineError("a text field takes a str")
        if len(value.encode("utf-8")) > KEYWORD_BYTES:
            raise EngineError(
                f"a text value longer than {KEYWORD_BYTES} UTF-8 bytes is one term Elasticsearch "
                f"cannot index; refused before it is sent"
            )
        return value
    if kind == "keyword/uuid":
        try:
            return str(value if isinstance(value, UUID) else UUID(str(value)))
        except ValueError:
            raise EngineError("a uuid field takes a UUID or its text") from None
    if kind == "date":
        if isinstance(value, datetime) or not isinstance(value, date):
            raise EngineError("a date field takes a date")
        return value.isoformat()
    if kind in ("long/timestamp", "long/timestamptz"):
        return _micros(value)
    if kind.startswith("long/decimal("):
        digits, scale = _decimal_parts(kind)
        if isinstance(value, (bool, float)):
            raise EngineError("a decimal field takes a Decimal or an int, never a float")
        try:
            exact = value if isinstance(value, Decimal) else Decimal(str(value))
        except InvalidOperation:
            raise EngineError("a decimal field takes a decimal") from None
        if not exact.is_finite():
            raise EngineError("a decimal field takes a finite decimal")
        scaled = exact.scaleb(scale)
        if scaled != scaled.to_integral_value():
            raise EngineError(f"the value has more than {scale} fractional digits")
        integer = int(scaled)
        if abs(integer) >= 10**digits:
            raise EngineError(f"the value has more than {digits} digits")
        return integer
    raise EngineError(f"{kind!r} is not a column type this adapter writes")


def _read_value(kind: str, value: Any) -> Any:
    if value is None:
        return None
    if kind in ("boolean", "integer", "long", "keyword"):
        return value
    if kind in ("float", "double"):
        return float(value)
    if kind == "keyword/uuid":
        return UUID(value)
    if kind == "date":
        return date.fromisoformat(value)
    if kind in ("long/timestamp", "long/timestamptz"):
        instant = _EPOCH + timedelta(microseconds=int(value))
        return instant.replace(tzinfo=None) if kind == "long/timestamp" else instant
    if kind.startswith("long/decimal("):
        _, scale = _decimal_parts(kind)
        return Decimal(int(value)).scaleb(-scale)
    raise EngineError(f"{kind!r} is not a column type this adapter reads")


def document_id(key_values: Sequence[Any]) -> str:
    """The ``_id`` of a document, from its key values as stored (format contract 7m).

    Each value is a tag, its length as four big-endian bytes, and its bytes: ``s`` and the UTF-8
    of a text exactly as written, ``i`` and the decimal digits of an integer, ``b`` and ``true`` or
    ``false``. The SHA-256 of them all, in key order, in lowercase hexadecimal.

    Not the canonical encoding, which normalises text to NFC: two keys that differ only in how an
    accent is composed are two rows in every other engine, compared byte by byte, and would be one
    document here. Every value is framed by its length, so no two keys share an encoding.
    """
    digest = hashlib.sha256()
    for value in key_values:
        if isinstance(value, bool):
            tag, payload = b"b", (b"true" if value else b"false")
        elif isinstance(value, int):
            tag, payload = b"i", str(value).encode("ascii")
        elif isinstance(value, str):
            tag, payload = b"s", value.encode("utf-8")
        else:
            raise EngineError(
                f"a key value of type {type(value).__name__} cannot address a document"
            )
        digest.update(tag + len(payload).to_bytes(4, "big") + payload)
    return digest.hexdigest()


def document(
    columns: Mapping[str, str], key: Sequence[str], values: Mapping[str, Any]
) -> tuple[str, dict[str, Any]]:
    """A row as an Elasticsearch document: its ``_id`` and its ``_source`` (format contract 7m).

    Every column of the index is in the source, null where the row has no value, so that a read
    returns the same fields however the row was written. Each text field that holds a value has its
    tokens - distinct, in code point order - under :data:`~sde.schema.TOKENS_FIELD`.
    """
    unknown = sorted(set(values) - set(columns))
    if unknown:
        raise EngineError(f"the index has no field {unknown}")
    missing = [column for column in key if values.get(column) is None]
    if missing:
        raise EngineError(f"a document needs its key {missing}")
    source: dict[str, Any] = {}
    tokens: dict[str, list[str]] = {}
    for column, kind in sorted(columns.items()):
        value = stored(kind, values.get(column))
        source[column] = value
        if kind == "keyword" and value is not None:
            found = search_tokens(value)
            if found:
                tokens[column] = found
    if tokens:
        source[TOKENS_FIELD] = tokens
    keyed = [source[column] for column in key]
    if any(isinstance(value, float) for value in keyed):
        raise EngineError("a key of a float type cannot address a document")
    return document_id(keyed), source


def search_tokens(text: Any) -> list[str]:
    """What a text field contributes to its token field: distinct tokens, in code point order."""
    return sorted(set(text_tokens(text))) if isinstance(text, str) else []


# --- the index, as this adapter knows it -----------------------------------------------------


@dataclass(frozen=True)
class _Index:
    name: str
    columns: Mapping[str, str]
    """Column -> layout type (``long/timestamptz``), read back from the mapping."""
    key: tuple[str, ...]

    def document(self, values: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        try:
            return document(self.columns, self.key, values)
        except EngineError as exc:
            raise EngineError(f"{self.name}: {exc}") from None

    def row(self, source: Mapping[str, Any]) -> dict[str, Any]:
        return {
            column: _read_value(kind, source.get(column)) for column, kind in self.columns.items()
        }


def _layout_type(name: str, field: Mapping[str, Any]) -> str:
    native = field.get("type")
    meta = field.get("meta") or {}
    neutral = meta.get("sde_type") if isinstance(meta, Mapping) else None
    kind = f"{native}/{neutral}" if neutral else str(native)
    elasticsearch_property(kind)  # refuses a type this library would not have created
    if native == "date" and field.get("format") != "strict_date":
        raise EngineError(f"{name} is a date field without the strict_date format")
    return kind


# --- the adapter -----------------------------------------------------------------------------


class ElasticsearchEngine:
    """A thin adapter over Elasticsearch's REST API. Executes decisions, makes none."""

    dialect = "elasticsearch"

    summary_refusal = (
        "this engine summarizes nothing exactly: its aggregations add in double precision, and a "
        "summary of this library is exact. Refused rather than rounded."
    )
    """Why ``Session.summarize`` is refused for a group on this engine."""

    def __init__(self, dsn: str) -> None:
        self._connection = parse_dsn(dsn)
        self._http: http.client.HTTPConnection | None = None
        self._indexes: dict[str, _Index] = {}
        self._usage = UsageGate()

    # --- connection ------------------------------------------------------------------------

    @guarded
    def connect(self) -> None:
        """Open the connection and authenticate, with the profile's timeouts.

        ``_security/_authenticate`` rather than the root endpoint: it answers any login that
        exists, where ``GET /`` needs the cluster's ``monitor`` privilege - which a key scoped to
        the layout's indexes, the least a client should give, does not have.
        """
        if self._http is None:
            self._open()
            status, body = self._request("GET", "/_security/_authenticate")
            if status != 200 or not isinstance(body, Mapping):
                self._drop()
                raise EngineError(
                    f"could not connect to Elasticsearch: {self._error(status, body)}"
                )

    @guarded
    def close(self) -> None:
        self._drop()

    def __enter__(self) -> ElasticsearchEngine:
        self.connect()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _open(self) -> None:
        c = self._connection
        try:
            if c.tls:
                context = ssl.create_default_context(cafile=c.ca_cert)
                context.minimum_version = ssl.TLSVersion.TLSv1_2
                connection: http.client.HTTPConnection = http.client.HTTPSConnection(
                    c.host, c.port, timeout=c.connect_timeout, context=context
                )
            else:
                connection = http.client.HTTPConnection(c.host, c.port, timeout=c.connect_timeout)
            connection.connect()
            assert connection.sock is not None
            connection.sock.settimeout(c.send_receive_timeout)
        except (OSError, ssl.SSLError) as exc:
            raise EngineError(
                f"could not connect to Elasticsearch at {c.host}:{c.port}: {type(exc).__name__}"
            ) from None
        self._http = connection

    def _drop(self) -> None:
        if self._http is not None:
            self._http.close()
            self._http = None

    def _stale(self) -> bool:
        """Whether the idle connection was closed by the server: readable while idle means an
        end of stream or bytes nobody asked for, and either way it is not reused."""
        sock = None if self._http is None else self._http.sock
        if sock is None:
            return True
        try:
            readable, _, _ = select.select([sock], [], [], 0)
        except (OSError, ValueError):
            return True
        return bool(readable)

    def _request(
        self, method: str, path: str, body: Any = None, *, ndjson: bytes | None = None
    ) -> tuple[int, Any]:
        """One request, one response, no redirect followed and nothing replayed."""
        if self._http is None:
            raise EngineError("not connected; call connect() first")
        if self._stale():
            # Nothing was sent on it, so opening another is not a replay.
            self._drop()
            self._open()
        assert self._http is not None
        headers = {"Accept": "application/json"}
        if self._connection.authorization is not None:
            headers["Authorization"] = self._connection.authorization
        payload: bytes | None = None
        if ndjson is not None:
            payload, headers["Content-Type"] = ndjson, "application/x-ndjson"
        elif body is not None:
            payload = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        try:
            self._http.request(method, path, body=payload, headers=headers)
            response = self._http.getresponse()
            data = response.read(MAX_RESPONSE_BYTES + 1)
        except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
            self._drop()
            raise EngineError(
                f"Elasticsearch {method} {path.split('?')[0]} lost its response "
                f"({type(exc).__name__}); whether it took effect is unknown, and it is not replayed"
            ) from None
        if len(data) > MAX_RESPONSE_BYTES:
            self._drop()
            raise EngineError(f"a response to {method} was over {MAX_RESPONSE_BYTES} bytes")
        if 300 <= response.status < 400:
            raise EngineError(f"Elasticsearch answered {response.status}; no redirect is followed")
        if not data:
            return response.status, None
        try:
            return response.status, json.loads(data)
        except ValueError:
            raise EngineError(
                f"Elasticsearch answered {response.status} with something that is not JSON"
            ) from None

    @staticmethod
    def _error(status: int, body: Any) -> str:
        if isinstance(body, Mapping) and isinstance(body.get("error"), Mapping):
            error = body["error"]
            return f"{status} {error.get('type', '')}: {error.get('reason', '')}".strip()
        return str(status)

    def _path(self, index: str, *rest: str) -> str:
        refusal = index_name_refusal(index)
        if refusal is not None:
            raise EngineError(f"{index!r} is not an Elasticsearch index name: {refusal}")
        return "/" + "/".join([urllib.parse.quote(index, safe=""), *rest])

    # --- schema ----------------------------------------------------------------------------

    @guarded
    def ensure_schema(
        self, layout: PhysicalLayout, *, keys: Mapping[str, Sequence[str]]
    ) -> tuple[PhysicalFinding, ...]:
        """Create each index that is missing, change none that exists, and check them all.

        An index that exists must have exactly the mapping this layout renders: Elasticsearch
        never changes a field's type in place, so a difference is a migration, not a repair.
        """
        statements = schema_statements(layout, keys=keys, dialect=self.dialect)
        for statement in statements:
            document = json.loads(statement)
            path = self._path(document["index"])
            status, body = self._request("PUT", path, {"mappings": document["mappings"]})
            exists = (
                status == 400
                and isinstance(body, Mapping)
                and isinstance(body.get("error"), Mapping)
                and body["error"].get("type") == "resource_already_exists_exception"
            )
            if status != 200 and not exists:
                raise EngineError(
                    f"creating index {document['index']} failed: {self._error(status, body)}"
                )
        log("sde.schema.applied", engine=self.dialect, statements=len(statements))
        self._verify_schema(layout, keys)
        return ()

    @guarded
    def validate_schema(
        self, layout: PhysicalLayout, *, keys: Mapping[str, Sequence[str]] | None = None
    ) -> tuple[PhysicalFinding, ...]:
        """Check the existing indexes without creating anything."""
        if keys is None:
            keys = {
                entity: self._index(index).key for entity, index in sorted(layout.tables.items())
            }
        self._verify_schema(layout, keys)
        return ()

    def _verify_schema(self, layout: PhysicalLayout, keys: Mapping[str, Sequence[str]]) -> None:
        for entity, index in sorted(layout.tables.items()):
            self._indexes.pop(index, None)
            found = self._index(index)
            expected = elasticsearch_mapping(layout, entity, keys)
            wanted = dict(layout.columns.get(entity, {}))
            key = list(expected["_meta"]["sde"]["key"])
            if dict(found.columns) != wanted or list(found.key) != key:
                raise EngineError(
                    f"index {index} exists with fields {dict(sorted(found.columns.items()))} and "
                    f"key {list(found.key)}, and the layout needs {dict(sorted(wanted.items()))} "
                    f"and key {key}. Elasticsearch changes no field in place."
                )

    def _index(self, name: str) -> _Index:
        """The index as its own mapping describes it, read once per connection."""
        known = self._indexes.get(name)
        if known is not None:
            return known
        status, body = self._request("GET", self._path(name, "_mapping"))
        if status == 404:
            raise EngineError(f"index {name} does not exist; prepare the schema first")
        if status != 200 or not isinstance(body, Mapping) or name not in body:
            raise EngineError(
                f"the mapping of {name} could not be read: {self._error(status, body)}"
            )
        mapping = body[name].get("mappings", {})
        meta = (mapping.get("_meta") or {}).get("sde") or {}
        key = meta.get("key")
        if mapping.get("dynamic") != "strict" or not isinstance(key, list) or not key:
            raise EngineError(
                f"index {name} was not created by this library: its mapping is not strict or "
                f"names no key in _meta.sde"
            )
        columns = {
            column: _layout_type(column, field)
            for column, field in sorted(mapping.get("properties", {}).items())
            if column != TOKENS_FIELD
        }
        if any(column not in columns for column in key):
            raise EngineError(f"index {name} names a key field it does not map")
        index = _Index(name, columns, tuple(str(column) for column in key))
        self._indexes[name] = index
        return index

    # --- writes ----------------------------------------------------------------------------

    @guarded
    def insert(self, table: str, values: Mapping[str, Any]) -> None:
        """One document under its key's ``_id``; a second save of the key replaces the first."""
        if not values:
            raise EngineError("nothing to insert")
        index = self._index(table)
        identity, source = index.document(values)
        status, body = self._request(
            "PUT", self._path(table, "_doc", identity) + "?refresh=wait_for", source
        )
        if status not in (200, 201):
            log("sde.write.failed", table=table, error=str(status))
            raise EngineError(f"insert into {table} failed: {self._error(status, body)}")

    @guarded
    def insert_many(self, table: str, rows: Sequence[Mapping[str, Any]]) -> None:
        """One ``_bulk`` request. Not atomic: on a refusal some documents may have been written,
        and the error says how many, never which values."""
        if not rows:
            return
        index = self._index(table)
        lines: list[bytes] = []
        for row in rows:
            identity, source = index.document(row)
            lines.append(json.dumps({"index": {"_index": table, "_id": identity}}).encode())
            lines.append(json.dumps(source, ensure_ascii=False, allow_nan=False).encode("utf-8"))
        payload = b"\n".join(lines) + b"\n"
        status, body = self._request("POST", "/_bulk?refresh=wait_for", ndjson=payload)
        if status != 200 or not isinstance(body, Mapping):
            raise EngineError(f"batch insert into {table} failed: {self._error(status, body)}")
        if body.get("errors"):
            items = [item.get("index", {}) for item in body.get("items", ())]
            failed = [item for item in items if item.get("status", 500) >= 300]
            first = failed[0].get("error", {}) if failed else {}
            raise EngineError(
                f"batch insert into {table}: {len(failed)} of {len(rows)} documents were refused "
                f"({first.get('type', 'unknown')}) and {len(items) - len(failed)} were written"
            )

    # --- reads -----------------------------------------------------------------------------

    @guarded
    def get(self, table: str, key: Mapping[str, Any]) -> dict[str, Any] | None:
        """One document by its key's ``_id``, immediate, and checked to be that key."""
        index = self._index(table)
        if sorted(key) != sorted(index.key):
            raise EngineError(f"a read of {table} by key names exactly {list(index.key)}")
        values = [stored(index.columns[column], key[column]) for column in index.key]
        status, body = self._request("GET", self._path(table, "_doc", document_id(values)))
        if status == 404:
            return None
        if status != 200 or not isinstance(body, Mapping):
            raise EngineError(f"select from {table} failed: {self._error(status, body)}")
        source = body.get("_source") or {}
        if [source.get(column) for column in index.key] != values:
            # A SHA-256 collision, or a document written under this _id by something else: either
            # way it is not the row asked for, and answering with it would be a wrong row.
            return None
        return index.row(source)

    def _value(self, index: _Index, name: str, value: Any) -> Any:
        return stored(index.columns[name], value)

    def _filter(self, index: _Index, predicate: ReadFilter) -> dict[str, Any]:
        name = predicate.column.name
        if predicate.operation == "eq":
            if predicate.value is None:
                return {"bool": {"must_not": [{"exists": {"field": name}}]}}
            return {"term": {name: self._value(index, name, predicate.value)}}
        bound = "gte" if predicate.operation == "ge" else "lt"
        return {"range": {name: {bound: self._value(index, name, predicate.value)}}}

    def _after(self, index: _Index, plan: ReadPlan) -> dict[str, Any]:
        """Rows past ``plan.after`` in the plan's order, nulls last: the keyset rule of the SQL
        dialects, as a query, so the engine's own sort values never stand in for a value."""
        assert plan.after is not None
        alternatives: list[dict[str, Any]] = []
        later = "lt" if plan.descending else "gt"
        for position, (column, value) in enumerate(zip(plan.order, plan.after, strict=True)):
            if value is None:
                continue
            clauses: list[dict[str, Any]] = []
            for previous, earlier in zip(plan.order[:position], plan.after[:position], strict=True):
                clauses.append(self._filter(index, ReadFilter(previous, "eq", earlier)))
            beyond = {"range": {column.name: {later: self._value(index, column.name, value)}}}
            missing = {"bool": {"must_not": [{"exists": {"field": column.name}}]}}
            clauses.append({"bool": {"should": [beyond, missing], "minimum_should_match": 1}})
            alternatives.append({"bool": {"filter": clauses}})
        if not alternatives:
            return {"bool": {"must_not": [{"match_all": {}}]}}
        return {"bool": {"should": alternatives, "minimum_should_match": 1}}

    def _page(
        self, table: str, plan: ReadPlan, extra: Sequence[dict[str, Any]] = ()
    ) -> list[dict[str, Any]]:
        index = self._index(table)
        clauses = [self._filter(index, predicate) for predicate in plan.filters]
        if plan.after is not None:
            clauses.append(self._after(index, plan))
        clauses.extend(extra)
        order = "desc" if plan.descending else "asc"
        body = {
            "query": {"bool": {"filter": clauses}},
            "sort": [{column.name: {"order": order, "missing": "_last"}} for column in plan.order],
            "size": plan.limit + 1,
            "track_total_hits": False,
        }
        status, response = self._request("POST", self._path(table, "_search"), body)
        if status != 200 or not isinstance(response, Mapping):
            raise EngineError(f"logical scan of {table} failed: {self._error(status, response)}")
        hits = response.get("hits", {}).get("hits", [])
        return [read_row(plan.columns, index.row(hit.get("_source") or {})) for hit in hits]

    @guarded
    def select_rows(self, table: str, plan: ReadPlan) -> list[dict[str, Any]]:
        return self._page(table, plan)

    @guarded
    def search_rows(
        self, table: str, plan: ReadPlan, query: Sequence[str], fields: Sequence[str]
    ) -> list[dict[str, Any]]:
        """A page of rows whose token fields hold every token of the query, in the plan's order."""
        index = self._index(table)
        bad = [field for field in fields if index.columns.get(field) != "keyword"]
        if bad:
            raise EngineError(f"{table} has no text field {bad} to search")
        extra = [
            {
                "bool": {
                    "should": [{"term": {f"{TOKENS_FIELD}.{field}": token}} for field in fields],
                    "minimum_should_match": 1,
                }
            }
            for token in query
        ]
        return self._page(table, plan, extra)

    @guarded
    def count_rows(self, table: str, plan: ReadPlan) -> int:
        index = self._index(table)
        clauses = [self._filter(index, predicate) for predicate in plan.filters]
        body = {"query": {"bool": {"filter": clauses}}}
        status, response = self._request("POST", self._path(table, "_count"), body)
        if status != 200 or not isinstance(response, Mapping) or "count" not in response:
            raise EngineError(f"count of {table} failed: {self._error(status, response)}")
        return int(response["count"])

    # --- rollback protection ------------------------------------------------------------------
    #
    # One document per map version applied, in an index of our own. `max` over them is the
    # watermark, as in the SQL engines' append-only table.

    def _watermark_index(self) -> None:
        mapping = {
            "dynamic": "strict",
            "properties": {
                "map_version": {"type": "long"},
                "model_version": {"type": "keyword"},
            },
        }
        status, body = self._request("PUT", self._path(WATERMARK_TABLE), {"mappings": mapping})
        exists = (
            status == 400
            and isinstance(body, Mapping)
            and isinstance(body.get("error"), Mapping)
            and body["error"].get("type") == "resource_already_exists_exception"
        )
        if status != 200 and not exists:
            raise EngineError(f"creating {WATERMARK_TABLE} failed: {self._error(status, body)}")

    @guarded
    def map_watermark(self) -> int | None:
        self._watermark_index()
        body = {"size": 1, "sort": [{"map_version": {"order": "desc"}}], "_source": ["map_version"]}
        status, response = self._request("POST", self._path(WATERMARK_TABLE, "_search"), body)
        if status != 200 or not isinstance(response, Mapping):
            raise EngineError(f"reading {WATERMARK_TABLE} failed: {self._error(status, response)}")
        hits = response.get("hits", {}).get("hits", [])
        return int(hits[0]["_source"]["map_version"]) if hits else None

    @guarded
    def record_map_version(self, version: int, *, model_version: str) -> None:
        self._watermark_index()
        source = {"map_version": int(version), "model_version": str(model_version)}
        path = self._path(WATERMARK_TABLE, "_doc", str(int(version))) + "?refresh=wait_for"
        status, body = self._request("PUT", path, source)
        if status not in (200, 201):
            raise EngineError(f"recording map version failed: {self._error(status, body)}")

    # --- transactions ----------------------------------------------------------------------

    def transaction(self) -> Any:
        """Refuses. Elasticsearch writes one document at a time, and so does this adapter."""
        raise EngineError(
            "Elasticsearch has no multi-document transactions, so this adapter will not pretend "
            "to start one. If these writes have to commit together, declare it: `atomic_with` on "
            "the entities makes them one colocation group, and the planner is then not permitted "
            "to put them here."
        )
