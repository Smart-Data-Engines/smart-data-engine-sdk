"""An adapter for Elasticsearch: a document per row, an index per entity, and a text index.

What this engine is for, in this library, is ``Session.search``: it supplies the candidates of a
search from an index of its own instead of every row in key order. It also takes writes, reads by
key, bounded scans and exact counts, so a group can live here whole. It has no transactions. It has
write generations - a fence in each index's final pipeline, with a write block as its drain
(``write-fences.md``) - so a group is copied onto it and off it by the local operator like a group
on any other engine with fences.

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
import re
import select
import ssl
import stat
import struct
import urllib.parse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from ipaddress import ip_address
from typing import Any, Final
from uuid import UUID

from .._usage import UsageGate, guarded
from ..canonical import CanonicalError, canonical_bytes
from ..errors import EngineError
from ..layout import index_name_refusal
from ..logging import log
from ..migration import key_columns, same_width
from ..physical import PhysicalFinding
from ..placement import BACKFILL_TABLE, PhysicalLayout
from ..query import ReadFilter, ReadPlan, read_row
from ..schema import (
    TOKENS_FIELD,
    elasticsearch_mapping,
    elasticsearch_property,
    schema_statements,
)
from ..search import tokens as text_tokens
from ..watermark import WATERMARK_TABLE
from ..write_fence import EPOCH_COLUMN, WriteFence
from ._staging import CREATION_MARKER
from ._write_fences import ElasticsearchFences

__all__ = ["KEYWORD_BYTES", "ElasticsearchEngine", "document", "document_id", "parse_dsn"]

KEYWORD_BYTES: Final = 32_766
"""The longest text a keyword holds, in UTF-8 bytes: Lucene's limit on one term."""

MAX_RESPONSE_BYTES: Final = 256 * 1024 * 1024
"""The most this adapter reads of one response. A page is at most 1001 documents."""

KEY_PAGE: Final = 1000
"""Documents per search when a migration reads in key order: a copy's chunk, a window of the
copy compared with it, or the keys counted towards a resume point."""

WATERMARK_MAPPING: Final = {
    "dynamic": "strict",
    "properties": {"map_version": {"type": "long"}, "model_version": {"type": "keyword"}},
}
"""The mapping of ``sde_map_state`` here: one document per map version applied."""

BACKFILL_MAPPING: Final = {
    "dynamic": "strict",
    "properties": {
        "entity": {"type": "keyword"},
        "materialization": {"type": "keyword"},
        "rows_copied": {"type": "long"},
    },
}
"""The mapping of ``sde_backfill_state`` here: one document per chunk copied, never updated."""

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


def _unquoted(text: str) -> str:
    """Percent-decoding as the TypeScript profile reads it: a malformed escape, or bytes that are
    not UTF-8, are refused - not kept as they stand, nor replaced by U+FFFD."""
    refusal = "the user and password of an Elasticsearch DSN are percent-encoded UTF-8"
    if re.search(r"%(?![0-9A-Fa-f]{2})", text) is not None:
        raise EngineError(refusal)
    try:
        return urllib.parse.unquote(text, errors="strict")
    except UnicodeDecodeError:
        raise EngineError(refusal) from None


def _seconds(name: str, text: str) -> float:
    # Digits and one optional fraction, as the TypeScript profile reads it: `1e1` or `inf` would
    # be seconds in one language and a refusal in the other.
    if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", text) is None:
        raise EngineError(f"{name} is a number of seconds")
    value = float(text)
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
    try:
        parts = urllib.parse.urlsplit(dsn)
    except ValueError:
        raise EngineError("an Elasticsearch DSN is a URL") from None
    if parts.scheme not in _SCHEMES:
        raise EngineError(f"an Elasticsearch DSN starts {' or '.join(sorted(_SCHEMES))}://")
    tls = _SCHEMES[parts.scheme]
    if parts.fragment or parts.path not in ("", "/"):
        raise EngineError("an Elasticsearch DSN has no path and no fragment")
    try:
        host, port = parts.hostname, parts.port
    except (ValueError, TypeError):
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
    user = _unquoted(parts.username) if parts.username is not None else None
    password = _unquoted(parts.password) if parts.password is not None else ""
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
        try:
            number = float(value)
        except OverflowError:
            raise EngineError("the value is outside float64") from None
        if not math.isfinite(number):
            raise EngineError("Elasticsearch stores no NaN or infinity")
        if kind == "float":
            # Indexed as a float32 and returned as written: rounded here, so that the value the
            # engine filters and sorts on is the value a read returns - as a PostgreSQL `real` and a
            # ClickHouse `Float32` return it.
            try:
                number = struct.unpack("<f", struct.pack("<f", number))[0]
            except OverflowError:
                raise EngineError("the value is outside float32") from None
            if not math.isfinite(number):
                raise EngineError("the value is outside float32")
        return number
    if kind == "keyword":
        if not isinstance(value, str):
            raise EngineError("a text field takes a str")
        try:
            size = len(value.encode("utf-8"))
        except UnicodeEncodeError:
            raise EngineError("a text value holds a lone surrogate, which UTF-8 cannot carry") \
                from None
        if size > KEYWORD_BYTES:
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
        coefficient, shift = _scaled(value, scale)
        if shift < 0:
            raise EngineError(f"the value has more than {scale} fractional digits")
        if coefficient and len(str(abs(coefficient))) + shift > digits:
            raise EngineError(f"the value has more than {digits} digits")
        return coefficient * int(10**shift)
    raise EngineError(f"{kind!r} is not a column type this adapter writes")


def _scaled(value: Any, scale: int) -> tuple[int, int]:
    """``value`` as ``coefficient * 10**(shift - scale)``, with the coefficient's trailing zeros
    taken off while that leaves a fraction: integer arithmetic only.

    Not ``Decimal.scaleb``, which rounds to the thread's decimal context - an application that set
    a precision of ten would have stored its decimals rounded and two keys under one ``_id``.
    """
    if isinstance(value, (bool, float)):
        raise EngineError("a decimal field takes a Decimal or an int, never a float")
    try:
        exact = value if isinstance(value, Decimal) else Decimal(str(value))
    except InvalidOperation:
        raise EngineError("a decimal field takes a decimal") from None
    if not exact.is_finite():
        raise EngineError("a decimal field takes a finite decimal")
    sign, figures, exponent = exact.as_tuple()
    assert isinstance(exponent, int)
    coefficient = int("".join(str(figure) for figure in figures) or "0")
    if coefficient == 0:
        return 0, 0
    while exponent + scale < 0 and coefficient % 10 == 0:
        coefficient //= 10
        exponent += 1
    return (-coefficient if sign else coefficient), exponent + scale


def _decimal_bound(kind: str, value: Any, operation: str) -> int | None:
    """A decimal predicate's bound in the column's scaled integers, or ``None`` for an equality no
    stored value can meet.

    A predicate is a question about the column, not a value to store: ``>= 0.005`` on a scale of
    2 is ``>= 0.01``, and ``< 0.005`` is ``< 0.01`` - what PostgreSQL and ClickHouse answer -
    rather than a refusal. A bound at or past the column's digits becomes the first value beyond
    them, where it keeps the same answer without leaving a ``long``. No power of ten is computed
    past the digits the value itself has: ``Decimal("1E-999999999")`` is one comparison.
    """
    digits, scale = _decimal_parts(kind)
    coefficient, shift = _scaled(value, scale)
    if coefficient == 0:
        return 0
    limit = int(10**digits)
    width = len(str(abs(coefficient))) + shift
    if width > digits:
        # At least 10**digits in magnitude: beyond every value the column holds.
        return None if operation == "eq" else (limit if coefficient > 0 else -limit)
    if shift >= 0:
        return coefficient * int(10**shift)
    if operation == "eq":
        return None
    if width <= 0:
        # Less than one step of the scale in magnitude: the ceiling is 1 or 0.
        return 1 if coefficient > 0 else 0
    ceiling: int = -((-coefficient) // int(10**-shift))
    return ceiling


def _same_json(left: Any, right: Any) -> bool:
    """Two JSON documents equal as values: key order aside, nothing the canonical encoding cannot
    hold - a float or a value of no canonical form is in nothing this library renders."""
    try:
        return canonical_bytes(left) == canonical_bytes(right)
    except (CanonicalError, TypeError, ValueError):
        return False


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
        integer = int(value)
        figures = tuple(int(figure) for figure in str(abs(integer)))
        return Decimal((int(integer < 0), figures, -scale))
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
            # Normalising and folding can lengthen a text: a run of U+023A folds to three bytes a
            # letter. A token is one term too, and one past the limit fails the document in the
            # engine after it was sent.
            if any(len(token.encode("utf-8")) > KEYWORD_BYTES for token in found):
                raise EngineError(
                    f"a token of {column} is longer than {KEYWORD_BYTES} UTF-8 bytes once "
                    f"normalised and folded, one term Elasticsearch cannot index"
                )
            if found:
                tokens[column] = found
    if tokens:
        source[TOKENS_FIELD] = tokens
    keyed = [source[column] for column in key]
    if any(isinstance(value, float) for value in keyed):
        raise EngineError("a key of a float type cannot address a document")
    return document_id(keyed), source


def _already_there(item: Mapping[str, Any]) -> bool:
    """A ``create`` refused because its document exists: what a copy leaves as it is."""
    error = item.get("error")
    return (
        item.get("status") == 409
        and isinstance(error, Mapping)
        and error.get("type") == "version_conflict_engine_exception"
    )


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
    entity: str = ""
    fenced: bool = False
    """The index has a write generation field: every document a session writes carries one."""

    @property
    def stored_columns(self) -> Mapping[str, str]:
        """What a document holds: the layout's columns, and the generation where it is fenced."""
        return {**self.columns, EPOCH_COLUMN: "long"} if self.fenced else self.columns

    def document(self, values: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        try:
            return document(self.stored_columns, self.key, values)
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
    def _whole(response: Mapping[str, Any], what: str) -> None:
        """Refuse an answer some shard did not contribute to: a page missing rows would move the
        cursor past them for good, and a count would not be exact."""
        shards = response.get("_shards")
        failed = shards.get("failed", 0) if isinstance(shards, Mapping) else 0
        if response.get("timed_out") is True or failed:
            raise EngineError(
                f"{what} came back partial - timed out or with {failed} failed shard(s) - and a "
                f"partial answer is not one this library returns"
            )

    @staticmethod
    def _write_error(status: int, body: Any) -> str:
        """A refused write, by its type only: the engine's reason can quote the document's value."""
        if isinstance(body, Mapping) and isinstance(body.get("error"), Mapping):
            return f"{status} {body['error'].get('type', '')}".strip()
        return str(status)

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

    # --- write generations -------------------------------------------------------------------

    def write_fence(self, table: str, *, project_id: str) -> WriteFence:
        """The write-fence protocol on this index (``write-fences.md``), for a provisioning login.

        Its pipeline and its write block need the cluster privilege ``manage_pipeline`` and
        ``manage`` on the index; reading its state at session open needs ``read_pipeline``. A
        runtime key holds neither of the first two, so it cannot lift a fence it writes under.
        """
        self._indexes.pop(table, None)
        return WriteFence(ElasticsearchFences(self._fence_request), table, project_id=project_id)

    def _fence_request(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        # A fence changes the index's mapping and settings: forget what was read of it.
        self._indexes.clear()
        return self._request(method, path, body)

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
            if dict(found.columns) != wanted or list(found.key) != key or found.entity != entity:
                raise EngineError(
                    f"index {index} exists for {found.entity} with fields "
                    f"{dict(sorted(found.columns.items()))} and key {list(found.key)}, and the "
                    f"layout needs {entity} with {dict(sorted(wanted.items()))} and key {key}. "
                    f"Elasticsearch changes no field in place."
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
        properties = mapping.get("properties", {})
        columns = {
            column: _layout_type(column, field)
            for column, field in sorted(properties.items())
            if column not in (TOKENS_FIELD, EPOCH_COLUMN)
        }
        # The write generation is the fence's field, not the layout's: present exactly as the
        # fence adds it, or absent.
        fenced = EPOCH_COLUMN in properties
        if fenced and properties[EPOCH_COLUMN] != {"type": "long"}:
            raise EngineError(f"index {name} maps {EPOCH_COLUMN} as something other than a long")
        if any(column not in columns for column in key):
            raise EngineError(f"index {name} names a key field it does not map")
        entity = meta.get("entity")
        if not isinstance(entity, str) or not entity:
            raise EngineError(f"index {name} names no entity in _meta.sde")
        # A staging's index says which staging created it, in the request that created it - as a
        # SQL staging comments its table in the transaction that creates it. Its form is fixed.
        stage = meta.get("stage")
        if stage is not None and (
            not isinstance(stage, str) or CREATION_MARKER.fullmatch(stage) is None
        ):
            raise EngineError(f"index {name} carries a staging marker that is not one")
        # The whole mapping, not only each field's type: a normalizer, a null_value, an
        # ignore_above, a token field mapped as text or a disabled _source would each change an
        # answer while every type still matched. It is what this library creates or nothing.
        rendered = elasticsearch_mapping(
            PhysicalLayout(tables={entity: name}, columns={entity: columns}),
            entity,
            {entity: [str(column) for column in key]},
        )
        if fenced:
            rendered["properties"][EPOCH_COLUMN] = {"type": "long"}
        if stage is not None:
            rendered["_meta"]["sde"]["stage"] = stage
        if not _same_json(rendered, mapping):
            raise EngineError(
                f"index {name} has a mapping this library would not create: something beyond each "
                f"field's type differs - a field parameter, the token fields or _source - and "
                f"each of those can change an answer"
            )
        index = _Index(name, columns, tuple(str(column) for column in key), entity, fenced)
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
            raise EngineError(f"insert into {table} failed: {self._write_error(status, body)}")

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
            raise EngineError(
                f"batch insert into {table} failed: {self._write_error(status, body)}"
            )
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
        kind = index.columns[name]
        if predicate.operation == "eq" and predicate.value is None:
            return {"bool": {"must_not": [{"exists": {"field": name}}]}}
        if kind.startswith("long/decimal("):
            bound = _decimal_bound(kind, predicate.value, predicate.operation)
            if bound is None:
                return {"bool": {"must_not": [{"match_all": {}}]}}
            value: Any = bound
        else:
            value = self._value(index, name, predicate.value)
        if predicate.operation == "eq":
            return {"term": {name: value}}
        return {"range": {name: {"gte" if predicate.operation == "ge" else "lt": value}}}

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
        path = self._path(table, "_search") + "?allow_partial_search_results=false"
        status, response = self._request("POST", path, body)
        if status != 200 or not isinstance(response, Mapping):
            raise EngineError(f"logical scan of {table} failed: {self._error(status, response)}")
        self._whole(response, f"a page of {table}")
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
        self._whole(response, f"the count of {table}")
        return int(response["count"])

    # --- migration ---------------------------------------------------------------------------
    #
    # `sde.migration.Migratable`, so a group can be copied onto this engine and off it. A
    # migration reads in key order, and a key is never null: the position is a filter, as a
    # page's is (§7m), so the engine's sort values stand in for no value. A copy is `create`, so a
    # document already there stays - PostgreSQL's ON CONFLICT DO NOTHING, not an overwrite: a
    # chunk copied again changes nothing, and an older row from a backfill never covers a newer
    # one the fan-out wrote.

    @staticmethod
    def _beyond(column: str, value: Any, *, above: bool) -> dict[str, Any]:
        """``column`` strictly above ``value`` (or below), the value as stored."""
        if isinstance(value, bool):
            # Two values and no order to range over: above true and below false hold nothing.
            if above == value:
                return {"bool": {"must_not": [{"match_all": {}}]}}
            return {"term": {column: not value}}
        return {"range": {column: {"gt" if above else "lt": value}}}

    def _keyset(
        self, order: Sequence[str], bound: Sequence[Any], *, above: bool, inclusive: bool
    ) -> dict[str, Any]:
        """Keys beyond ``bound`` in ``order`` - after it when ``above``, before it otherwise - and
        the bound itself when ``inclusive``: for each column, equality on the ones before it and
        beyond the bound on it, the row-value comparison of the SQL dialects."""
        alternatives: list[dict[str, Any]] = []
        for position, column in enumerate(order):
            clauses: list[dict[str, Any]] = [
                {"term": {earlier: bound[at]}} for at, earlier in enumerate(order[:position])
            ]
            clauses.append(self._beyond(column, bound[position], above=above))
            alternatives.append({"bool": {"filter": clauses}})
        if inclusive:
            alternatives.append(
                {"bool": {"filter": [{"term": {c: bound[at]}} for at, c in enumerate(order)]}}
            )
        return {"bool": {"should": alternatives, "minimum_should_match": 1}}

    def _stored_key(
        self, index: _Index, order: Sequence[str], bound: Sequence[Any], name: str
    ) -> list[Any]:
        same_width(bound, order, name)
        if any(value is None for value in bound):
            raise EngineError(f"{name} holds a null, and no key does")
        return [
            stored(index.columns[column], value)
            for column, value in zip(order, bound, strict=True)
        ]

    def _key_page(
        self,
        table: str,
        order: Sequence[str],
        *,
        after: Sequence[Any] | None,
        upto: Sequence[Any] | None,
        size: int,
        fields: Sequence[str] | None = None,
    ) -> list[Mapping[str, Any]]:
        """One search in key order: the sources of up to ``size`` documents after ``after`` and
        up to ``upto`` inclusive, both as stored."""
        clauses: list[dict[str, Any]] = []
        if after is not None:
            clauses.append(self._keyset(order, after, above=True, inclusive=False))
        if upto is not None:
            clauses.append(self._keyset(order, upto, above=False, inclusive=True))
        body: dict[str, Any] = {
            "query": {"bool": {"filter": clauses}},
            "sort": [{column: {"order": "asc"}} for column in order],
            "size": size,
            "track_total_hits": False,
        }
        if fields is not None:
            body["_source"] = list(fields)
        path = self._path(table, "_search") + "?allow_partial_search_results=false"
        status, response = self._request("POST", path, body)
        if status != 200 or not isinstance(response, Mapping):
            raise EngineError(
                f"key range select from {table} failed: {self._error(status, response)}"
            )
        self._whole(response, f"a key range of {table}")
        return [hit.get("_source") or {} for hit in response.get("hits", {}).get("hits", [])]

    def _ordered(self, table: str, order: Sequence[str]) -> tuple[_Index, tuple[str, ...]]:
        index = self._index(table)
        columns = key_columns(order, table)
        unknown = [column for column in columns if column not in index.columns]
        if unknown:
            raise EngineError(f"{table} has no field {unknown} to order by")
        return index, columns

    @guarded
    def key_range(
        self,
        table: str,
        order: Sequence[str],
        *,
        after: Sequence[Any] | None = None,
        upto: Sequence[Any] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Rows in key order, strictly after one key and up to another inclusive, with the write
        generation where the index has one - what ``SELECT *`` returns in the SQL dialects.

        In searches of at most :data:`KEY_PAGE` documents, each starting after the last one read:
        a window with no limit, which verification reads, is not one search's worth.
        """
        index, columns = self._ordered(table, order)
        position = None if after is None else self._stored_key(index, columns, after, "after")
        ceiling = None if upto is None else self._stored_key(index, columns, upto, "upto")
        if limit is not None and limit < 1:
            return []
        rows: list[dict[str, Any]] = []
        while limit is None or len(rows) < limit:
            size = KEY_PAGE if limit is None else min(KEY_PAGE, limit - len(rows))
            sources = self._key_page(table, columns, after=position, upto=ceiling, size=size)
            for source in sources:
                row = index.row(source)
                if index.fenced:
                    row[EPOCH_COLUMN] = source.get(EPOCH_COLUMN)
                rows.append(row)
            if len(sources) < size:
                break
            position = [sources[-1].get(column) for column in columns]
        return rows

    @guarded
    def nth_key(
        self, table: str, order: Sequence[str], *, position: int
    ) -> tuple[Any, ...] | None:
        """The key of the ``position``-th row in key order, one-based, or None if there is none.

        Counted through searches of keys alone: Elasticsearch has no ``OFFSET`` past its result
        window, and this is paid once per resume, not per chunk.
        """
        index, columns = self._ordered(table, order)
        if position < 1:
            raise EngineError(f"position is one-based; {position} is not a row")
        seen = 0
        after: list[Any] | None = None
        while True:
            size = min(KEY_PAGE, position - seen)
            sources = self._key_page(
                table, columns, after=after, upto=None, size=size, fields=columns
            )
            if len(sources) < size:
                return None
            seen += size
            last = sources[-1]
            if seen == position:
                return tuple(
                    _read_value(index.columns[column], last.get(column)) for column in columns
                )
            after = [last.get(column) for column in columns]

    @guarded
    def count(self, table: str) -> int:
        """Every document of the index, exactly."""
        self._index(table)
        status, response = self._request(
            "POST", self._path(table, "_count"), {"query": {"match_all": {}}}
        )
        if status != 200 or not isinstance(response, Mapping) or "count" not in response:
            raise EngineError(f"count on {table} failed: {self._error(status, response)}")
        self._whole(response, f"the count of {table}")
        return int(response["count"])

    @guarded
    def copy_in(self, table: str, rows: Sequence[Mapping[str, Any]]) -> None:
        """Copy rows in, leaving any document whose key is already there.

        One ``_bulk`` of ``create`` actions, visible to a search on return (``refresh=true``): a
        verification reads the copy by searching it. A document already there is refused with
        409 and kept, which is what makes a chunk copied twice idempotent.
        """
        if not rows:
            return
        columns = sorted(rows[0])
        for row in rows:
            if sorted(row) != columns:
                raise EngineError(
                    f"copy_in into {table} was given rows with different columns "
                    f"({columns} and {sorted(row)}). A chunk comes from one table, so this is a "
                    f"caller assembling it from two."
                )
        index = self._index(table)
        lines: list[bytes] = []
        for row in rows:
            identity, source = index.document(row)
            lines.append(json.dumps({"create": {"_index": table, "_id": identity}}).encode())
            lines.append(json.dumps(source, ensure_ascii=False, allow_nan=False).encode("utf-8"))
        payload = b"\n".join(lines) + b"\n"
        status, body = self._request("POST", "/_bulk?refresh=true", ndjson=payload)
        if status != 200 or not isinstance(body, Mapping):
            log("sde.write.failed", table=table, error=str(status))
            raise EngineError(
                f"copying {len(rows)} rows into {table} failed: {self._write_error(status, body)}"
            )
        items = [item.get("create", {}) for item in body.get("items", ())]
        present = [item for item in items if _already_there(item)]
        failed = [
            item for item in items if item.get("status", 500) >= 300 and not _already_there(item)
        ]
        if len(items) != len(rows) or failed:
            first = failed[0].get("error", {}) if failed else {}
            log("sde.write.failed", table=table, error=str(first.get("type", "unknown")))
            raise EngineError(
                f"copying {len(rows)} rows into {table}: {len(failed)} were refused "
                f"({first.get('type', 'unknown')}), {len(present)} were there already and "
                f"{len(items) - len(failed) - len(present)} were written"
            )

    @guarded
    def backfill_marker(self, *, materialization: str, entity: str) -> int:
        """How many rows of this entity have been copied into this engine. Zero if none.

        The maximum over an index that is only ever appended to, as the SQL engines' table: a
        stale document can never lower the marker.
        """
        self._bookkeeping(BACKFILL_TABLE, BACKFILL_MAPPING)
        body = {
            "size": 1,
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"materialization": str(materialization)}},
                        {"term": {"entity": str(entity)}},
                    ]
                }
            },
            "sort": [{"rows_copied": {"order": "desc"}}],
            "_source": ["rows_copied"],
            "track_total_hits": False,
        }
        path = self._path(BACKFILL_TABLE, "_search") + "?allow_partial_search_results=false"
        status, response = self._request("POST", path, body)
        if status != 200 or not isinstance(response, Mapping):
            raise EngineError(f"reading {BACKFILL_TABLE} failed: {self._error(status, response)}")
        self._whole(response, f"the marker in {BACKFILL_TABLE}")
        hits = response.get("hits", {}).get("hits", [])
        return int(hits[0]["_source"]["rows_copied"]) if hits else 0

    @guarded
    def record_backfill_marker(self, *, materialization: str, entity: str, rows: int) -> None:
        """Append the new marker, visible on return. Never update, so a trail stays readable."""
        self._bookkeeping(BACKFILL_TABLE, BACKFILL_MAPPING)
        source = {
            "materialization": str(materialization),
            "entity": str(entity),
            "rows_copied": int(rows),
        }
        path = self._path(BACKFILL_TABLE, "_doc") + "?refresh=true"
        status, body = self._request("POST", path, source)
        if status != 201:
            raise EngineError(
                f"recording backfill progress in {BACKFILL_TABLE} failed: "
                f"{self._error(status, body)}"
            )

    # --- bookkeeping ---------------------------------------------------------------------------
    #
    # Two indexes of our own, append-only, read as a maximum: the map watermark and the backfill
    # markers, as the SQL engines' tables. Each is read before anything creates it, as the SQL
    # adapters look in their catalogue before CREATE: a login that only reads and writes it needs
    # no create_index, and an index of that name with another mapping - `map_version` as a
    # keyword, whose maximum is the lexicographic one - is refused rather than trusted.

    def _bookkeeping(self, name: str, mapping: Mapping[str, Any]) -> None:
        for _ in range(2):
            status, body = self._request("GET", self._path(name, "_mapping"))
            if status == 200 and isinstance(body, Mapping):
                found = body.get(name, {}).get("mappings") if isinstance(body.get(name), Mapping) \
                    else None
                if not _same_json(found, mapping):
                    raise EngineError(
                        f"{name} exists with a mapping this library would not create; it keeps "
                        f"this library's bookkeeping, so it is refused rather than read"
                    )
                return
            if status != 404:
                raise EngineError(f"reading {name} failed: {self._error(status, body)}")
            status, body = self._request("PUT", self._path(name), {"mappings": dict(mapping)})
            if status == 200:
                return
            exists = (
                status == 400
                and isinstance(body, Mapping)
                and isinstance(body.get("error"), Mapping)
                and body["error"].get("type") == "resource_already_exists_exception"
            )
            if not exists:
                raise EngineError(f"creating {name} failed: {self._error(status, body)}")
            # Created by somebody else between the two requests: read what they created.
        raise EngineError(f"{name} could not be read or created")

    def _watermark_index(self) -> None:
        self._bookkeeping(WATERMARK_TABLE, WATERMARK_MAPPING)

    @guarded
    def map_watermark(self) -> int | None:
        self._watermark_index()
        body = {"size": 1, "sort": [{"map_version": {"order": "desc"}}], "_source": ["map_version"]}
        path = self._path(WATERMARK_TABLE, "_search") + "?allow_partial_search_results=false"
        status, response = self._request("POST", path, body)
        if status != 200 or not isinstance(response, Mapping):
            raise EngineError(f"reading {WATERMARK_TABLE} failed: {self._error(status, response)}")
        self._whole(response, f"the watermark in {WATERMARK_TABLE}")
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
