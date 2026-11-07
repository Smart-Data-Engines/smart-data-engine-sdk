"""Where a connection may go without TLS: the one rule the engine adapters share.

A connection that does not leave the machine has nothing for TLS to protect; one that does carries
credentials and rows across a network this library knows nothing about. So plain transport to a
host other than the loopback is accepted only when the connection string says so - an explicit
decision a person wrote, never a default (specs/secure-connections). Elasticsearch never accepts it
at all, as before.

The decision is made on the text, before connecting, and names are not resolved: a name that
resolves to the loopback is not one here, because what it resolves to is decided elsewhere and
later.
"""

from __future__ import annotations

from ipaddress import ip_address


def loopback(host: str) -> bool:
    """``localhost`` in any case, an IPv4 address in 127.0.0.0/8, ``::1``, or 127.0.0.0/8 mapped
    into IPv6 (``::ffff:127.0.0.1``).

    The mapped form is decided by its IPv4 part explicitly: Python 3.13 began to answer that way and
    3.11 and 3.12 do not, and a rule that changed with the interpreter would not be one rule.
    """
    if host.lower() == "localhost":
        return True
    if "%" in host:
        # A zone (``::1%lo``) is not the loopback here. C's inet_pton refuses one, so the C++
        # library cannot accept it, and a rule the libraries answered differently is not one rule.
        return False
    try:
        address = ip_address(host.strip("[]"))
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    return bool((mapped or address).is_loopback)
