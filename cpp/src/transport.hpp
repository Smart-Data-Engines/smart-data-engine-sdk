#pragma once

// Where a connection may go without TLS: the one rule the adapters share (specs/secure-connections),
// the same as the reference's sde/engines/_transport.py. Decided on the text, before connecting;
// names are not resolved.

#include <string>

namespace sde::detail {

// `localhost` in any case, an IPv4 address in 127.0.0.0/8, ::1 in any spelling, or 127.0.0.0/8
// mapped into IPv6 (::ffff:127.0.0.1). Brackets around an IPv6 address are allowed.
bool loopback(const std::string& host);

}  // namespace sde::detail
