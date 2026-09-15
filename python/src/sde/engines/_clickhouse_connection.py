"""Pure normalization of the portable ClickHouse DSN profile.

No sockets, CA reads or driver factories run here. The connection owner qualifies local trust
material before creating a client. Never include a raw DSN or its credentials in parser errors.
"""

from __future__ import annotations

import ipaddress
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qsl, unquote_to_bytes, urlsplit

from ..errors import EngineError

CONNECT_TIMEOUT_SECONDS = 10
HANDSHAKE_TIMEOUT_SECONDS = 15
MIN_TIMEOUT_SECONDS = 0.001
MAX_TIMEOUT_SECONDS = 2147483.647
_QUERY_KEYS = frozenset({"secure", "verify", "ca_cert", "connect_timeout", "send_receive_timeout"})
_SCHEMES = frozenset({"clickhouse", "clickhouses", "http", "https"})
_PERCENT = re.compile(r"%(?![0-9a-fA-F]{2})")
_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", re.ASCII)
_NUMERIC_HOST = re.compile(r"(?:[0-9]+|0x[0-9a-f]+)(?:\.(?:[0-9]+|0x[0-9a-f]+))*\.?", re.ASCII)
_SECONDS = re.compile(r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", re.ASCII)


@dataclass(frozen=True)
class ConnectionParameters:
    host: str = field(repr=False)
    port: int
    username: str = field(repr=False)
    password: str = field(repr=False)
    database: str = field(repr=False)
    secure: bool
    ca_cert: str | None = field(default=None, repr=False)
    connect_timeout: float = CONNECT_TIMEOUT_SECONDS
    send_receive_timeout: float = HANDSHAKE_TIMEOUT_SECONDS

    @property
    def interface(self) -> Literal["http", "https"]:
        return "https" if self.secure else "http"


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise EngineError(message)


def _no_controls(value: str) -> None:
    _require(
        all(ord(character) >= 32 and ord(character) != 127 for character in value),
        "ClickHouse connection fields cannot contain control characters",
    )


def _decode(value: str) -> str:
    result = unquote_to_bytes(value).decode("utf-8", errors="strict")
    _no_controls(result)
    return result


def _host(value: str) -> str:
    _require(
        value.isascii() and "%" not in value, "ClickHouse host must be ASCII DNS or an IP address"
    )
    value = value.lower()
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        _require(
            _NUMERIC_HOST.fullmatch(value) is None, "Legacy numeric IPv4 forms are not supported"
        )
        labels = value[:-1].split(".") if value.endswith(".") else value.split(".")
        _require(
            len(value) <= 253 and all(_DNS_LABEL.fullmatch(label) is not None for label in labels),
            "ClickHouse host is not a valid DNS name or IP address",
        )
        return value
    return str(address)


def _seconds(value: str | None, default: float) -> float:
    if value is None:
        return default
    _require(
        _SECONDS.fullmatch(value) is not None, "ClickHouse timeout must be positive finite seconds"
    )
    result = float(value)
    _require(
        math.isfinite(result) and MIN_TIMEOUT_SECONDS <= result <= MAX_TIMEOUT_SECONDS,
        "ClickHouse timeout must be positive finite seconds",
    )
    return result


def parse_dsn(dsn: str) -> ConnectionParameters:
    """Parse the shared URI profile once; errors name rules, never connection values."""
    try:
        _require(isinstance(dsn, str) and bool(dsn), "Provide a ClickHouse connection URI")
        dsn.encode("utf-8", errors="strict")
        _require(
            not any(character.isspace() for character in dsn),
            "Encode whitespace in connection URIs",
        )
        _no_controls(dsn)
        _require(
            "#" not in dsn and "\\" not in dsn and _PERCENT.search(dsn) is None,
            "ClickHouse URI fragments, raw backslashes and malformed percent escapes are refused",
        )
        parsed = urlsplit(dsn)
        scheme = parsed.scheme.lower()
        _require(
            scheme in _SCHEMES and bool(parsed.netloc),
            "Unsupported ClickHouse URI scheme or authority",
        )
        _require(
            parsed.hostname is not None and parsed.netloc.count("@") <= 1,
            "ClickHouse URI needs one unambiguous host",
        )
        authority = parsed.netloc.rsplit("@", 1)[-1]
        _require("\\" not in authority, "ClickHouse authority cannot contain a backslash")
        if authority.startswith("["):
            closing = authority.find("]")
            _require(
                closing > 0 and (len(authority) == closing + 1 or authority[closing + 1] == ":"),
                "ClickHouse IPv6 authority is malformed",
            )
            declared_port = authority[closing + 2 :] if len(authority) > closing + 1 else None
        else:
            _require(authority.count(":") <= 1, "Enclose IPv6 hosts in URI brackets")
            declared_port = authority.rsplit(":", 1)[1] if ":" in authority else None
        port = None
        if declared_port is not None:
            _require(
                re.fullmatch(r"[0-9]+", declared_port, flags=re.ASCII) is not None,
                "ClickHouse port must be an explicit positive integer",
            )
            port = int(declared_port)
            _require(0 < port <= 65535, "ClickHouse port is outside the valid range")
        host = _host(parsed.hostname or "")
        username = _decode(parsed.username) if parsed.username is not None else "default"
        password = _decode(parsed.password) if parsed.password is not None else ""
        _require(
            bool(username) and ":" not in username,
            "ClickHouse username must be nonempty and contain no colon",
        )
        _require(
            parsed.path.startswith("/") and parsed.path.count("/") == 1 and "\\" not in parsed.path,
            "ClickHouse URI needs one explicit database path segment",
        )
        database = _decode(parsed.path[1:])
        _require(
            bool(database) and "/" not in database and database not in (".", ".."),
            "ClickHouse database must be a nonempty single segment",
        )
        options: dict[str, str] = {}
        for name, value in parse_qsl(
            parsed.query,
            keep_blank_values=True,
            strict_parsing=True,
            encoding="utf-8",
            errors="strict",
        ):
            _no_controls(name)
            _no_controls(value)
            _require(
                name in _QUERY_KEYS and name not in options,
                "ClickHouse query parameters must be known and appear once",
            )
            options[name] = value
        requested = options.get("secure")
        _require(
            requested is None or requested in ("true", "false"),
            "ClickHouse secure must be literal true or false",
        )
        secure = scheme in ("https", "clickhouses")
        if scheme == "clickhouse":
            _require(
                requested is not None or port not in (443, 8443),
                "Select TLS or explicit plain transport for an ambiguous ClickHouse port",
            )
            secure = requested == "true"
        elif requested is not None:
            _require(
                (requested == "true") == secure, "ClickHouse scheme and secure setting disagree"
            )
        _require(
            "verify" not in options or options["verify"] == "true",
            "ClickHouse verification must remain enabled; use literal true or omit verify",
        )
        _require(
            secure or not {"verify", "ca_cert"} & options.keys(),
            "ClickHouse TLS options cannot apply to plain HTTP",
        )
        ca_cert = options.get("ca_cert")
        _require(
            ca_cert is None or (bool(ca_cert) and Path(ca_cert).is_absolute()),
            "ClickHouse CA must be an absolute local file path",
        )
        if port is None:
            port = {"http": 80, "https": 443}.get(scheme, 8443 if secure else 8123)
        return ConnectionParameters(
            host=host,
            port=port,
            username=username,
            password=password,
            database=database,
            secure=secure,
            ca_cert=ca_cert,
            connect_timeout=_seconds(options.get("connect_timeout"), CONNECT_TIMEOUT_SECONDS),
            send_receive_timeout=_seconds(
                options.get("send_receive_timeout"), HANDSHAKE_TIMEOUT_SECONDS
            ),
        )
    except EngineError:
        raise
    except (ValueError, TypeError, UnicodeError, AttributeError):
        raise EngineError(
            "Invalid ClickHouse URI encoding, authority or connection setting"
        ) from None
