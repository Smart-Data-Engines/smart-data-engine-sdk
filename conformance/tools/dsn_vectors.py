#!/usr/bin/env python3
"""Write conformance/vectors/dsn/: how every library reads the orderbook engine's DSN (contract 7n).

    python3 conformance/tools/dsn_vectors.py [--check]

`--check` writes nothing and fails if a vector would change, or if a case directory is there that
this file no longer writes: CI's contract job runs it, so the vectors cannot drift from what is
written here.

One directory a case, `NNN-<name>/case.json`, holding the DSN and either what it says - host,
port, identity, secret, tls, ca, verify and timeout, `null` where it says nothing - or `refused`: a
literal substring of the refusal and, where the DSN has a secret, that secret, which the refusal
must not contain. Until 31 October 2026 each library read this DSN with its language's URL parser,
and the parsers disagree exactly where a DSN is written by hand (findings 29 and 32 of the C++
port): the cases are those edges.

The expectations are written out here by hand, not computed by any library: a vector computed by the
implementation it checks would check nothing. A case's number is its identity and is never reused.
Every character outside printable ASCII is written as an escape, here and in the vectors, so that a
reader sees which code point a case holds.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1] / "vectors" / "dsn"

# The refusals, word for word as the contract's rules give them (section 7n).
PRINTABLE = (
    "an orderbook DSN is printable ASCII: a space, a control character or a character beyond "
    "ASCII is written percent-encoded"
)
SCHEME = "an orderbook DSN starts with orderbook://"
FRAGMENT = "an orderbook DSN has no fragment: a '#' in a secret is written %23"
CREDENTIALS = "an orderbook DSN's credentials are identity:secret, and neither is empty"
IDENTITY = (
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


def undecoded(name: str) -> str:
    return f"the orderbook DSN parameter '{name}' is not percent-encoded UTF-8"


def unknown(name: str) -> str:
    return f"the orderbook DSN parameter '{name}' is unknown; it takes ca, timeout, tls and verify"


def twice(name: str) -> str:
    return f"the orderbook DSN gives '{name}' twice"


def switch(name: str) -> str:
    return f"the orderbook DSN parameter '{name}' is on or off"


def says(
    host: str,
    port: int,
    *,
    identity: str | None = None,
    secret: str | None = None,
    tls: bool = False,
    ca: str | None = None,
    verify: bool = True,
    timeout: float = 10.0,
) -> dict[str, Any]:
    return {
        "host": host,
        "port": port,
        "identity": identity,
        "secret": secret,
        "tls": tls,
        "ca": ca,
        "verify": verify,
        "timeout": timeout,
    }


def refused(match: str, absent: str | None = None) -> dict[str, Any]:
    return {"match": match} if absent is None else {"match": match, "absent": absent}


SIXTY_FOUR = "a" * 64

# (number, name, dsn, outcome, why). An outcome with "host" is what the DSN says; any other is a
# refusal.
CASES: list[tuple[int, str, str, dict[str, Any], str]] = [
    # --- what a DSN says ---
    (
        1,
        "every part",
        "orderbook://trading-app:p+ss%20w%3Ard@ob1.internal:9443/"
        "?tls=on&ca=/etc/ssl/ca.pem&verify=on&timeout=2.5",
        says("ob1.internal", 9443, identity="trading-app", secret="p+ss w:rd", tls=True,
             ca="/etc/ssl/ca.pem", timeout=2.5),
        "the secret is percent-decoded and a + in it is itself: only a parameter is read as a form",
    ),
    (
        2,
        "the defaults",
        "orderbook://127.0.0.1:9090",
        says("127.0.0.1", 9090),
        "no credentials, no TLS, verification on, no CA file and 10 seconds",
    ),
    (
        3,
        "the scheme in any case and the host in lower case",
        "ORDERBOOK://OB1.Internal:9443",
        says("ob1.internal", 9443),
        "the scheme is ASCII case-insensitive, and a name is kept in lower case",
    ),
    (
        4,
        "an ipv6 address",
        "orderbook://[::1]:9090",
        says("::1", 9090),
        "the brackets are the DSN's, not the address's",
    ),
    (
        5,
        "an ipv6 address is not compressed",
        "orderbook://[FE80:0:0:0:0:0:0:1]:9090",
        says("fe80:0:0:0:0:0:0:1", 9090),
        "lower case, and otherwise as written: no library compresses an address",
    ),
    (
        6,
        "an ipv6 address with an ipv4 tail",
        "orderbook://[::FFFF:192.0.2.1]:9090",
        says("::ffff:192.0.2.1", 9090),
        "the last 32 bits may be written as an IPv4 address",
    ),
    (
        7,
        "a name that looks like a number stays a name",
        "orderbook://0x7f.1:9090",
        says("0x7f.1", 9090),
        "WHATWG's parser made this 127.0.0.1, and a TLS check then compared the certificate with "
        "an address the DSN never gave",
    ),
    (
        8,
        "a name with an underscore",
        "orderbook://ob_primary:9090",
        says("ob_primary", 9090),
        "a container's name may hold one, and the resolver takes it",
    ),
    (
        9,
        "a port with leading zeros",
        "orderbook://ob1:00080",
        says("ob1", 80),
        "digits with a value from 1 to 65535, however many zeros lead",
    ),
    (
        10,
        "a slash and empty pieces of the query",
        "orderbook://ob1:9090/?&&tls=on&",
        says("ob1", 9090, tls=True),
        "a path of / alone is no path, and an empty piece of the query is skipped",
    ),
    (
        11,
        "a parameter percent-encoded",
        "orderbook://ob1:9090?%74ls=o%6E",
        says("ob1", 9090, tls=True),
        "a name and a value are both decoded",
    ),
    (
        12,
        "a plus in a parameter is a space",
        "orderbook://ob1:9090?tls=on&ca=/etc/my+ca.pem",
        says("ob1", 9090, tls=True, ca="/etc/my ca.pem"),
        "a parameter is read as a form is: a + in a path is written %2B",
    ),
    (
        13,
        "a colon in an identity",
        "orderbook://desk%3A1:s3cret@ob1:9090",
        says("ob1", 9090, identity="desk:1", secret="s3cret"),
        "the server takes a : in an identity, and the DSN splits the credentials at the first raw "
        "one",
    ),
    (
        14,
        "the last at sign ends the credentials",
        "orderbook://app:p@ss@ob1:9090",
        says("ob1", 9090, identity="app", secret="p@ss"),
        "the host follows the last @, so a secret may hold one",
    ),
    (
        15,
        "a secret holds colons",
        "orderbook://app:a:b:c@ob1:9090",
        says("ob1", 9090, identity="app", secret="a:b:c"),
        "the credentials are split at the first :, and the rest is the secret",
    ),
    (
        16,
        "a secret beyond ascii",
        "orderbook://app:%C5%BC%C3%B3%C5%82w@ob1:9090",
        says("ob1", 9090, identity="app", secret="\u017c\u00f3\u0142w"),
        "a secret is any text, written as percent-encoded UTF-8",
    ),
    (
        17,
        "an identity of sixty-four characters",
        f"orderbook://{SIXTY_FOUR}:s3cret@ob1:9090",
        says("ob1", 9090, identity=SIXTY_FOUR, secret="s3cret"),
        "the longest identity the server takes",
    ),
    (
        18,
        "the shortest timeout",
        "orderbook://ob1:9090?timeout=0.001",
        says("ob1", 9090, timeout=0.001),
        "a millisecond: TypeScript's socket waits no less",
    ),
    (
        19,
        "the longest timeout",
        "orderbook://ob1:9090?timeout=2147483.647",
        says("ob1", 9090, timeout=2147483.647),
        "2^31 - 1 milliseconds, the longest a Node.js timer waits as asked",
    ),
    (
        20,
        "a timeout with zeros",
        "orderbook://ob1:9090?timeout=007.50",
        says("ob1", 9090, timeout=7.5),
        "decimal digits, however many zeros lead or trail",
    ),
    (
        21,
        "a timeout whose double is the shortest",
        "orderbook://ob1:9090?timeout=0.00099999999999999999999",
        says("ob1", 9090, timeout=0.001),
        "the bound compares doubles, and this one's nearest double is 0.001's",
    ),
    (
        22,
        "verification off under tls",
        "orderbook://ob1:9090?tls=on&verify=off",
        says("ob1", 9090, tls=True, verify=False),
        "written out, and only with TLS",
    ),
    (
        23,
        "verification on restated",
        "orderbook://ob1:9090?verify=on",
        says("ob1", 9090),
        "the default restated is not a setting that would do nothing: it asks for nothing",
    ),
    (
        24,
        "an ipv6 address ending in a double colon",
        "orderbook://[1:2:3:4:5:6:7::]:9090",
        says("1:2:3:4:5:6:7::", 9090),
        "seven groups and :: for the eighth",
    ),
    (
        25,
        "an ipv6 address starting with a double colon",
        "orderbook://[::2:3:4:5:6:7:8]:9090",
        says("::2:3:4:5:6:7:8", 9090),
        ":: for the first group and seven after it",
    ),
    (
        26,
        "a byte order mark leading a secret is kept",
        "orderbook://app:%EF%BB%BFs3cret@ob1:9090",
        says("ob1", 9090, identity="app", secret="\ufeffs3cret"),
        "the bytes are UTF-8 and nothing more: a decoder that drops a leading byte order mark, as "
        "JavaScript's TextDecoder does unless told not to, answers with another secret",
    ),
    # --- rule 1: printable ASCII ---
    (
        101,
        "a trailing newline",
        "orderbook://ob1:9090\n",
        refused(PRINTABLE),
        "read from a file and not trimmed: refused, where every URL parser dropped it in silence",
    ),
    (
        102,
        "leading spaces",
        "  orderbook://ob1:9090",
        refused(PRINTABLE),
        "Python's and WHATWG's parsers strip them; a DSN that says what it means has none",
    ),
    (
        103,
        "a raw space in a parameter",
        "orderbook://ob1:9090?tls=on&ca=/etc/my ca.pem",
        refused(PRINTABLE),
        "a space is written %20, or + in a parameter",
    ),
    (
        104,
        "a raw secret beyond ascii",
        "orderbook://app:\u017c\u00f3\u0142w@ob1:9090",
        refused(PRINTABLE, "\u017c\u00f3\u0142w"),
        "a character beyond ASCII is written percent-encoded, as in any URL",
    ),
    (
        105,
        "the kelvin sign in a host",
        "orderbook://\u212a:9090",
        refused(PRINTABLE),
        "Python's parser lower-cased U+212A to the letter k and connected there",
    ),
    (
        106,
        "a tab inside a host",
        "orderbook://ob\t1:9090",
        refused(PRINTABLE),
        "both URL parsers removed a tab wherever it stood",
    ),
    (
        107,
        "a delete character",
        "orderbook://ob1:9090?timeout=1\u007f",
        refused(PRINTABLE),
        "U+007F is a control character too",
    ),
    (
        108,
        "a nul character",
        "orderbook://ob1:9090\u0000",
        refused(PRINTABLE),
        "a C string would end there, and every other library would read on",
    ),
    # --- rule 2: the scheme ---
    (
        111,
        "another scheme",
        "postgresql://app:s3cret@ob1:9090",
        refused(SCHEME, "s3cret"),
        "a DSN for another engine in the orderbook's variable",
    ),
    (
        112,
        "no slashes",
        "orderbook:ob1:9090",
        refused(SCHEME),
        "the scheme is orderbook:// whole",
    ),
    (
        113,
        "one slash",
        "orderbook:/ob1:9090",
        refused(SCHEME),
        "the scheme is orderbook:// whole",
    ),
    # --- rule 3: no fragment ---
    (
        121,
        "a hash in a secret",
        "orderbook://app:p#ss@ob1:9090",
        refused(FRAGMENT, "p#ss"),
        "a URL parser read the rest as a fragment and reported a port that is not a number",
    ),
    (
        122,
        "an empty fragment",
        "orderbook://ob1:9090#",
        refused(FRAGMENT),
        "both URL parsers accepted it, because an empty fragment is none",
    ),
    # --- rule 5: the credentials ---
    (
        131,
        "an identity without its secret",
        "orderbook://app@ob1:9090",
        refused(CREDENTIALS),
        "the server asks for both",
    ),
    (
        132,
        "a secret without its identity",
        "orderbook://:s3cret@ob1:9090",
        refused(CREDENTIALS, "s3cret"),
        "an empty identity is none",
    ),
    (
        133,
        "an empty secret",
        "orderbook://app:@ob1:9090",
        refused(CREDENTIALS),
        "an empty secret is none",
    ),
    (
        134,
        "empty credentials",
        "orderbook://@ob1:9090",
        refused(CREDENTIALS),
        "an @ says there are credentials",
    ),
    (
        135,
        "an at sign in an identity",
        "orderbook://app%40desk:s3cret@ob1:9090",
        refused(IDENTITY, "s3cret"),
        "the server's --auth-secret-file takes no @, so this identity could never authenticate",
    ),
    (
        136,
        "a space in an identity",
        "orderbook://two%20words:s3cret@ob1:9090",
        refused(IDENTITY, "s3cret"),
        "an identity is sent on the wire, and a space would end it",
    ),
    (
        137,
        "an identity of sixty-five characters",
        f"orderbook://{SIXTY_FOUR}b:s3cret@ob1:9090",
        refused(IDENTITY, "s3cret"),
        "one longer than the server takes",
    ),
    (
        138,
        "a broken escape in an identity",
        "orderbook://app%2:s3cret@ob1:9090",
        refused(IDENTITY, "s3cret"),
        "a % without two hexadecimal digits is refused, where Python kept it as written",
    ),
    (
        139,
        "an invisible character in an identity",
        "orderbook://app%E2%80%8B:s3cret@ob1:9090",
        refused(IDENTITY, "s3cret"),
        "U+200B is not whitespace to Python or to JavaScript, and it is not one of the server's "
        "characters",
    ),
    (
        140,
        "a secret that is not utf-8",
        "orderbook://app:s3%FFcret@ob1:9090",
        refused(SECRET, "s3%FFcret"),
        "Python read the byte as U+FFFD and authenticated with another secret than the one written",
    ),
    (
        141,
        "a broken escape in a secret",
        "orderbook://app:s3%zzcret@ob1:9090",
        refused(SECRET, "s3%zzcret"),
        "a % without two hexadecimal digits",
    ),
    (
        142,
        "a surrogate in a secret",
        "orderbook://app:%ED%A0%80@ob1:9090",
        refused(SECRET),
        "an encoded surrogate is not UTF-8",
    ),
    # --- rules 6 and 7: the host and the port ---
    (
        151,
        "no host",
        "orderbook://:9090",
        refused(HOST_AND_PORT),
        "a port alone",
    ),
    (
        152,
        "no port",
        "orderbook://ob1",
        refused(HOST_AND_PORT),
        "the engine has no default port worth guessing",
    ),
    (
        153,
        "an empty port",
        "orderbook://ob1:",
        refused(HOST_AND_PORT),
        "a : with nothing after it",
    ),
    (
        154,
        "an ipv6 address without a port",
        "orderbook://[::1]",
        refused(HOST_AND_PORT),
        "the brackets end the host, and no port follows",
    ),
    (
        155,
        "a tilde in a host",
        "orderbook://ob~1:9090",
        refused(HOST),
        "a name is letters, digits, ., - and _",
    ),
    (
        156,
        "a percent-encoded host",
        "orderbook://ob%2D1:9090",
        refused(HOST),
        "a host is not decoded: it is written as the resolver reads it",
    ),
    (
        157,
        "an ipv6 zone",
        "orderbook://[fe80::1%25eth0]:9090",
        refused(HOST),
        "Python accepted a zone and connected nowhere; no library takes one",
    ),
    (
        158,
        "an ipv4 address in brackets",
        "orderbook://[192.0.2.1]:9090",
        refused(HOST),
        "brackets hold an IPv6 address",
    ),
    (
        159,
        "an unclosed bracket",
        "orderbook://[::1:9090",
        refused(HOST),
        "an address without its closing bracket",
    ),
    (
        160,
        "text after the brackets",
        "orderbook://[::1]x:9090",
        refused(HOST),
        "the brackets end the host",
    ),
    (
        161,
        "nine ipv6 groups",
        "orderbook://[1:2:3:4:5:6:7:8:9]:9090",
        refused(HOST),
        "an address has eight",
    ),
    (
        162,
        "two double colons",
        "orderbook://[1::2::3]:9090",
        refused(HOST),
        "one :: at most, or the groups it stands for are not known",
    ),
    (
        163,
        "an ipv4 octet with a leading zero",
        "orderbook://[::ffff:192.0.2.01]:9090",
        refused(HOST),
        "some readers take 01 as octal: no octet has a leading zero",
    ),
    (
        164,
        "a group of five digits",
        "orderbook://[12345::1]:9090",
        refused(HOST),
        "a group is one to four hexadecimal digits",
    ),
    (
        165,
        "empty brackets",
        "orderbook://[]:9090",
        refused(HOST),
        "no address",
    ),
    (
        166,
        "a double colon and eight groups",
        "orderbook://[1:2:3:4::5:6:7:8]:9090",
        refused(HOST),
        ":: stands for at least one group, and eight leave none",
    ),
    (
        167,
        "port zero",
        "orderbook://ob1:0",
        refused(PORT),
        "Python accepted it and failed at connect",
    ),
    (
        168,
        "a port past 65535",
        "orderbook://ob1:65536",
        refused(PORT),
        "a port is sixteen bits",
    ),
    (
        169,
        "a signed port",
        "orderbook://ob1:+1",
        refused(PORT),
        "digits only",
    ),
    (
        170,
        "two ports",
        "orderbook://ob1:1:2",
        refused(PORT),
        "a name has no :, so everything after the first is the port",
    ),
    (
        171,
        "a port of letters",
        "orderbook://app:s3cret@ob1:x",
        refused(PORT, "s3cret"),
        "the refusal names the port and never repeats the DSN",
    ),
    (
        172,
        "a port past every integer",
        "orderbook://ob1:99999999999999999999999",
        refused(PORT),
        "read without overflowing, in every language",
    ),
    # --- rule 8: the path ---
    (
        181,
        "a path",
        "orderbook://app:s3cret@ob1:9090/var/data?mode=x",
        refused(PATH, "s3cret"),
        "a data directory is local mode, which takes no DSN; the path is refused before the "
        "parameters",
    ),
    (
        182,
        "two slashes",
        "orderbook://ob1:9090//",
        refused(PATH),
        "only / alone is no path",
    ),
    # --- rule 9: the parameters ---
    (
        191,
        "an unknown parameter",
        "orderbook://ob1:9090?sslmode=require",
        refused(unknown("sslmode")),
        "PostgreSQL's parameter in the orderbook's DSN",
    ),
    (
        192,
        "a name in another case",
        "orderbook://ob1:9090?Tls=on",
        refused(unknown("Tls")),
        "names are exact",
    ),
    (
        193,
        "a secret given as a parameter",
        "orderbook://ob1:9090?password=TOPSECRET",
        refused(unknown("password"), "TOPSECRET"),
        "a refusal quotes a name, never a value",
    ),
    (
        194,
        "a name that does not decode",
        "orderbook://ob1:9090?%ZZ=1",
        refused(undecoded("%ZZ")),
        "quoted as written",
    ),
    (
        195,
        "an unknown name is refused before a later duplicate",
        "orderbook://ob1:9090?x=1&tls=on&tls=on",
        refused(unknown("x")),
        "the parameters are read left to right",
    ),
    (
        196,
        "a parameter given twice",
        "orderbook://ob1:9090?tls=on&tls=off",
        refused(twice("tls")),
        "neither wins",
    ),
    (
        197,
        "a parameter given twice in two spellings",
        "orderbook://ob1:9090?tls=on&%74ls=on",
        refused(twice("tls")),
        "names are compared decoded",
    ),
    (
        198,
        "a duplicate is refused before its value",
        "orderbook://ob1:9090?tls=on&tls=yes",
        refused(twice("tls")),
        "a second tls is refused for being second",
    ),
    (
        199,
        "a value that does not decode",
        "orderbook://ob1:9090?tls=%FF",
        refused(undecoded("tls")),
        "a value is percent-encoded UTF-8 too",
    ),
    (
        200,
        "a switch that is neither",
        "orderbook://ob1:9090?tls=yes",
        refused(switch("tls")),
        "on or off",
    ),
    (
        201,
        "a switch in capitals",
        "orderbook://ob1:9090?tls=ON",
        refused(switch("tls")),
        "exactly on or off",
    ),
    (
        202,
        "a switch without a value",
        "orderbook://ob1:9090?verify",
        refused(switch("verify")),
        "a piece without = has an empty value",
    ),
    (
        203,
        "an empty ca",
        "orderbook://ob1:9090?tls=on&ca=",
        refused(CA),
        "Python accepted it and failed at connect",
    ),
    (
        204,
        "a ca with a nul",
        "orderbook://ob1:9090?tls=on&ca=/etc/ca%00.pem",
        refused(CA),
        "C++ would have opened /etc/ca, the path up to the NUL",
    ),
    (
        211,
        "an infinite timeout",
        "orderbook://ob1:9090?timeout=inf",
        refused(TIMEOUT),
        "Python's float() read it, and connect() failed on the first socket: finding 29",
    ),
    (
        212,
        "a timeout with an exponent",
        "orderbook://ob1:9090?timeout=1e3",
        refused(TIMEOUT),
        "decimal digits, with or without a fraction",
    ),
    (
        213,
        "a timeout with no integer part",
        "orderbook://ob1:9090?timeout=.5",
        refused(TIMEOUT),
        "a digit before the point",
    ),
    (
        214,
        "a timeout with no fraction after its point",
        "orderbook://ob1:9090?timeout=5.",
        refused(TIMEOUT),
        "a digit after the point",
    ),
    (
        215,
        "a timeout with an underscore",
        "orderbook://ob1:9090?timeout=1_0",
        refused(TIMEOUT),
        "Python's float() read ten seconds and JavaScript's Number() refused it: finding 32",
    ),
    (
        216,
        "a signed timeout",
        "orderbook://ob1:9090?timeout=-1",
        refused(TIMEOUT),
        "no sign",
    ),
    (
        217,
        "a hexadecimal timeout",
        "orderbook://ob1:9090?timeout=0x10",
        refused(TIMEOUT),
        "JavaScript's Number() read sixteen seconds and Python's float() refused it",
    ),
    (
        218,
        "an arabic-indic digit",
        "orderbook://ob1:9090?timeout=%D9%A3",
        refused(TIMEOUT),
        "Python's float() read U+0663 as three seconds",
    ),
    (
        219,
        "a timeout of zero",
        "orderbook://ob1:9090?timeout=0",
        refused(TIMEOUT),
        "Node.js's socket.setTimeout(0) waits forever",
    ),
    (
        220,
        "a timeout under a millisecond",
        "orderbook://ob1:9090?timeout=0.0009",
        refused(TIMEOUT),
        "TypeScript's socket would wait a millisecond rather than as asked",
    ),
    (
        221,
        "a timeout past the longest",
        "orderbook://ob1:9090?timeout=2147483.648",
        refused(TIMEOUT),
        "Node.js cuts a longer timer to 2^31 - 1 milliseconds",
    ),
    (
        222,
        "a timeout that is a word",
        "orderbook://app:s3cret@ob1:9090?timeout=soon",
        refused(TIMEOUT, "s3cret"),
        "the refusal quotes neither the value nor the secret",
    ),
    (
        223,
        "an empty timeout",
        "orderbook://ob1:9090?timeout=",
        refused(TIMEOUT),
        "no digits",
    ),
    (
        224,
        "a timeout with a space",
        "orderbook://ob1:9090?timeout=+1",
        refused(TIMEOUT),
        "a + in a parameter is a space, and Python's float() stripped it",
    ),
    (
        225,
        "a timeout with two points",
        "orderbook://ob1:9090?timeout=1.2.3",
        refused(TIMEOUT),
        "one fraction",
    ),
    (
        226,
        "a timeout past every double",
        "orderbook://ob1:9090?timeout=" + "9" * 400,
        refused(TIMEOUT),
        "past the double range, which every language reads as out of range",
    ),
    # --- rule 10: settings that would do nothing ---
    (
        231,
        "a ca without tls",
        "orderbook://ob1:9090?ca=/etc/ssl/ca.pem",
        refused(CA_WITHOUT_TLS),
        "without TLS the connection is plain text, and a CA file would read as protection",
    ),
    (
        232,
        "verification off without tls",
        "orderbook://ob1:9090?verify=off",
        refused(VERIFY_WITHOUT_TLS),
        "there is no certificate to decline to check",
    ),
    (
        233,
        "a ca with verification off",
        "orderbook://ob1:9090?tls=on&verify=off&ca=/etc/ssl/ca.pem",
        refused(CA_WITHOUT_VERIFY),
        "Python refused it at connect, in its engine client's words, and TypeScript at fromDsn: "
        "finding 32",
    ),
    (
        234,
        "a ca without tls is refused first",
        "orderbook://ob1:9090?ca=/etc/ssl/ca.pem&verify=off",
        refused(CA_WITHOUT_TLS),
        "the three rules in their order",
    ),
]


def _slug(name: str) -> str:
    return "-".join("".join(c if c.isalnum() else " " for c in name.lower()).split())


def _case(name: str, dsn: str, outcome: dict[str, Any], why: str) -> dict[str, Any]:
    key = "says" if "host" in outcome else "refused"
    return {"name": name, "dsn": dsn, key: outcome, "why": why}


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail if a vector would change")
    args = parser.parse_args()
    numbers = [number for number, *_ in CASES]
    if len(set(numbers)) != len(numbers):
        print("two cases share a number")
        return 1
    planned = {
        f"{number:03d}-{_slug(name)}": json.dumps(_case(name, dsn, outcome, why), indent=1) + "\n"
        for number, name, dsn, outcome, why in CASES
    }
    changed = []
    for name, text in planned.items():
        path = ROOT / name / "case.json"
        if path.exists() and path.read_text(encoding="utf-8") == text:
            continue
        changed.append(f"{name}/case.json")
        if not args.check:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
    stale = sorted(p for p in ROOT.iterdir() if p.name not in planned) if ROOT.exists() else []
    for folder in stale:
        changed.append(f"{folder.name}/ (no longer written)")
        if not args.check:
            for item in folder.iterdir():
                item.unlink()
            folder.rmdir()
    if args.check and changed:
        print("dsn vectors would change:", *changed, sep="\n  ")
        return 1
    written = "unchanged" if not changed else f"{len(changed)} change(s) written"
    print(f"{ROOT.relative_to(ROOT.parents[1])}: {len(CASES)} case(s), {written}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
