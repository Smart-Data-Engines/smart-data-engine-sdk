"""The orderbook engine's DSN, read by format contract 7n: one grammar, the same in every library.

No URL parser reads it. Python's and WHATWG's disagree exactly where a DSN is written by hand -
`0x7f.1`, `timeout=1_0`, `timeout=inf`, a zone, a stripped newline (findings 29 and 32 of the C++
port) - so every rule is written out here in the contract's order, each refusal in the contract's
words. A refusal names the part that is wrong and never repeats the DSN, which carries the secret:
it quotes a parameter's name as written, never a value.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..errors import EngineError

__all__ = ["DEFAULT_TIMEOUT", "LONGEST_TIMEOUT", "SHORTEST_TIMEOUT", "OrderbookDsn", "parse_dsn"]

DEFAULT_TIMEOUT = 10.0
SHORTEST_TIMEOUT = 0.001
"""A millisecond: TypeScript's socket waits no less, so no library promises less."""
LONGEST_TIMEOUT = 2147483.647
"""2^31 - 1 milliseconds: the longest a Node.js timer waits as asked, so no library promises
more."""

IDENTITY = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
"""The identities the server's `--auth-secret-file` takes (the engine's `valid_identity`). One
outside them can never authenticate, and it is sent on the wire, where a space would end it."""

_PREFIX = "orderbook://"
_PARAMETERS = frozenset({"ca", "timeout", "tls", "verify"})
_SWITCH = {"on": True, "off": False}
_NAME = re.compile(r"[A-Za-z0-9._-]+")
_PORT = re.compile(r"[0-9]+")
_GROUP = re.compile(r"[0-9A-Fa-f]{1,4}")
_OCTET = re.compile(r"0|[1-9][0-9]{0,2}")
_SECONDS = re.compile(r"[0-9]+(?:\.[0-9]+)?")
_HEX = frozenset("0123456789abcdefABCDEF")

PRINTABLE = (
    "an orderbook DSN is printable ASCII: a space, a control character or a character beyond "
    "ASCII is written percent-encoded"
)
SCHEME = "an orderbook DSN starts with orderbook://"
FRAGMENT = "an orderbook DSN has no fragment: a '#' in a secret is written %23"
CREDENTIALS = "an orderbook DSN's credentials are identity:secret, and neither is empty"
IDENTITY_REFUSAL = (
    "an orderbook DSN's identity is 1 to 64 characters of [A-Za-z0-9_.:-], as the server's "
    "--auth-secret-file takes them"
)
SECRET = "an orderbook DSN's secret is percent-encoded UTF-8"
HOST_AND_PORT = "an orderbook DSN names a host and a port: orderbook://host:port"
HOST = (
    "an orderbook DSN's host is a name of letters, digits, '.', '-' and '_', or an IPv6 address "
    "in brackets"
)
PORT = "an orderbook DSN's port is a number from 1 to 65535"
PATH = "an orderbook DSN has no path; local mode takes a data directory"
CA = "the orderbook DSN parameter 'ca' names a file: it is not empty and holds no NUL"
TIMEOUT = (
    "the orderbook DSN parameter 'timeout' is seconds in decimal digits, from 0.001 to "
    "2147483.647"
)
CA_WITHOUT_TLS = "the orderbook DSN's ca needs tls=on: without TLS it verifies nothing"
VERIFY_WITHOUT_TLS = (
    "the orderbook DSN's verify=off needs tls=on: without TLS there is no certificate to decline "
    "to check"
)
CA_WITHOUT_VERIFY = "the orderbook DSN's ca with verify=off is a trust anchor nothing consults"


@dataclass(frozen=True)
class OrderbookDsn:
    """What a DSN says: where to connect, and how. ``repr()`` never shows the secret."""

    host: str
    """A name in lower case, or an IPv6 address in lower case without its brackets."""
    port: int
    identity: str | None
    secret: str | None = field(repr=False)
    tls: bool
    ca: str | None
    verify: bool
    timeout: float
    """Seconds, from ``SHORTEST_TIMEOUT`` to ``LONGEST_TIMEOUT``."""

    @property
    def auth(self) -> tuple[str, str] | None:
        if self.identity is None or self.secret is None:
            return None
        return (self.identity, self.secret)


def parse_dsn(dsn: object) -> OrderbookDsn:
    """Read ``orderbook://[identity:secret@]host:port[/][?name=value[&...]]`` by contract 7n.

    Raises ``EngineError`` at the first rule the DSN breaks, in the rules' order.
    """
    if not isinstance(dsn, str):
        raise EngineError("an orderbook DSN is a string")
    # Rule 1. A comparison of code points, not str.isprintable(), which is Unicode's question.
    if not all("!" <= character <= "~" for character in dsn):
        raise EngineError(PRINTABLE)
    # Rules 2 and 3. Every character is ASCII now, so lower() is ASCII's.
    if dsn[: len(_PREFIX)].lower() != _PREFIX:
        raise EngineError(SCHEME)
    rest = dsn[len(_PREFIX) :]
    if "#" in rest:
        raise EngineError(FRAGMENT)
    # Rule 4.
    stops = [found for found in (rest.find("/"), rest.find("?")) if found != -1]
    end = min(stops, default=len(rest))
    authority, rest = rest[:end], rest[end:]
    path, _, query = rest.partition("?")
    # Rule 5: what comes before the last @ is identity:secret, split at the first :.
    identity = secret = None
    userinfo, at, hostport = authority.rpartition("@")
    if at:
        written_identity, colon, written_secret = userinfo.partition(":")
        if not colon or not written_identity or not written_secret:
            raise EngineError(CREDENTIALS)
        identity = _decoded(written_identity, form=False)
        if identity is None or IDENTITY.fullmatch(identity) is None:
            raise EngineError(IDENTITY_REFUSAL)
        secret = _decoded(written_secret, form=False)
        if secret is None:
            raise EngineError(SECRET)
    # Rules 6 and 7.
    host, written_port = _host_and_port(hostport)
    port = _port(written_port)
    # Rule 8.
    if path not in ("", "/"):
        raise EngineError(PATH)
    # Rule 9, piece by piece, left to right.
    options: dict[str, str] = {}
    for piece in query.split("&"):
        if not piece:
            continue
        written_name, _, written_value = piece.partition("=")
        name = _decoded(written_name, form=True)
        if name is None:
            raise EngineError(
                f"the orderbook DSN parameter '{written_name}' is not percent-encoded UTF-8"
            )
        if name not in _PARAMETERS:
            raise EngineError(
                f"the orderbook DSN parameter '{written_name}' is unknown; it takes ca, timeout, "
                "tls and verify"
            )
        if name in options:
            raise EngineError(f"the orderbook DSN gives '{name}' twice")
        value = _decoded(written_value, form=True)
        if value is None:
            raise EngineError(f"the orderbook DSN parameter '{name}' is not percent-encoded UTF-8")
        if name in ("tls", "verify") and value not in _SWITCH:
            raise EngineError(f"the orderbook DSN parameter '{name}' is on or off")
        if name == "ca" and (not value or "\x00" in value):
            raise EngineError(CA)
        if name == "timeout" and seconds(value) is None:
            raise EngineError(TIMEOUT)
        options[name] = value
    # Rule 10.
    tls = _SWITCH[options.get("tls", "off")]
    verify = _SWITCH[options.get("verify", "on")]
    ca = options.get("ca")
    if ca is not None and not tls:
        raise EngineError(CA_WITHOUT_TLS)
    if not verify and not tls:
        raise EngineError(VERIFY_WITHOUT_TLS)
    if ca is not None and not verify:
        raise EngineError(CA_WITHOUT_VERIFY)
    timeout = seconds(options["timeout"]) if "timeout" in options else DEFAULT_TIMEOUT
    assert timeout is not None  # checked with its piece
    return OrderbookDsn(
        host=host,
        port=port,
        identity=identity,
        secret=secret,
        tls=tls,
        ca=ca,
        verify=verify,
        timeout=timeout,
    )


def seconds(value: str) -> float | None:
    """A timeout's seconds in decimal digits, or None: its nearest double, within the range."""
    if _SECONDS.fullmatch(value) is None:
        return None
    # float() rounds correctly at any length and never raises on digits: past the double range it
    # is infinity, below it zero, and the range refuses both.
    number = float(value)
    return number if SHORTEST_TIMEOUT <= number <= LONGEST_TIMEOUT else None


def _decoded(written: str, *, form: bool) -> str | None:
    """Percent-decoded UTF-8, or None: a ``%`` without two hexadecimal digits, or bytes that are not
    UTF-8. In a form - a parameter - a ``+`` is a space first."""
    if form:
        written = written.replace("+", " ")
    out = bytearray()
    index = 0
    while index < len(written):
        if written[index] != "%":
            out += written[index].encode("ascii")  # rule 1 made every character ASCII
            index += 1
            continue
        pair = written[index + 1 : index + 3]
        if len(pair) != 2 or not (pair[0] in _HEX and pair[1] in _HEX):
            return None
        out.append(int(pair, 16))
        index += 3
    try:
        # Strict: an overlong form, an encoded surrogate and a truncated sequence are refused.
        return out.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _host_and_port(hostport: str) -> tuple[str, str]:
    """The host, in lower case, and the port as written."""
    if hostport.startswith("["):
        close = hostport.find("]")
        if close == -1 or not _ipv6(hostport[1:close]):
            raise EngineError(HOST)
        after = hostport[close + 1 :]
        if not after:
            raise EngineError(HOST_AND_PORT)
        if not after.startswith(":"):
            raise EngineError(HOST)
        return hostport[1:close].lower(), after[1:]
    name, _, written_port = hostport.partition(":")
    if not name:
        raise EngineError(HOST_AND_PORT)
    if _NAME.fullmatch(name) is None:
        raise EngineError(HOST)
    return name.lower(), written_port


def _port(written: str) -> int:
    if not written:
        raise EngineError(HOST_AND_PORT)
    if _PORT.fullmatch(written) is None:
        raise EngineError(PORT)
    # Without the zeros that lead, five digits at most: int() of thousands of digits raises, and no
    # port has more than five.
    digits = written.lstrip("0")
    if len(digits) > 5 or not 1 <= int(digits or "0") <= 65535:
        raise EngineError(PORT)
    return int(digits)


def _ipv6(text: str) -> bool:
    """RFC 3986's ``IPv6address``: groups of one to four hexadecimal digits, at most one ``::``
    standing for at least one group, and an IPv4 address allowed as the last 32 bits."""
    head, double, tail = text.partition("::")
    if double and "::" in tail:
        return False
    left = head.split(":") if head else []
    right = tail.split(":") if double and tail else []
    groups = left + right
    if not groups and not double:
        return False
    count = 0
    for index, group in enumerate(groups):
        # Only the address's last group may be an IPv4 address: not one before a closing "::".
        last = index == len(groups) - 1 and (not double or bool(right))
        if last and "." in group:
            if not _ipv4(group):
                return False
            count += 2
        elif _GROUP.fullmatch(group) is not None:
            count += 1
        else:
            return False
    return count <= 7 if double else count == 8


def _ipv4(text: str) -> bool:
    octets = text.split(".")
    return len(octets) == 4 and all(
        _OCTET.fullmatch(octet) is not None and int(octet) <= 255 for octet in octets
    )
