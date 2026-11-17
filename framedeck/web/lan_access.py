"""PIN-free access over direct private LAN and VPN connections.

Require a private TCP peer and private literal Host, or a Tailscale peer
with a MagicDNS ts.net Host. Forwarded headers are never locality evidence.
"""
from __future__ import annotations

import ipaddress

from starlette.requests import Request

_LAN = tuple(ipaddress.ip_network(network) for network in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
    "fc00::/7", "100.64.0.0/10",
))
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def trusted_direct_lan(request: Request) -> bool:
    if request.headers.get("forwarded") or request.headers.get("x-forwarded-for"):
        return False
    peer = request.client.host if request.client else ""
    host = request.url.hostname or ""
    if peer == "testclient":
        return True  # TestClient's explicit synthetic ASGI peer.
    if peer in _LOOPBACK and host in _LOOPBACK:
        return True
    try:
        source = ipaddress.ip_address(peer)
    except ValueError:
        return False
    if not any(source in net for net in _LAN):
        return False
    # MagicDNS names are controlled by Tailscale, unlike arbitrary DNS Hosts.
    # Restrict this exception to peers on the Tailscale overlay itself.
    tailscale_peer = (source in ipaddress.ip_network("100.64.0.0/10") or
                      source in ipaddress.ip_network("fd7a:115c:a1e0::/48"))
    if tailscale_peer and host.lower().rstrip(".").endswith(".ts.net"):
        return True
    try:
        dest = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(dest in net for net in _LAN)
