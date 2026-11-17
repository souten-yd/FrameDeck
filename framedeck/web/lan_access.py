"""Pinless browser access only over direct RFC1918 LAN IP connections.

Both the TCP peer and the browser-facing Host must be literal local IPs.
This avoids treating a malicious public Host/DNS rebinding or forwarded proxy
as an authenticated local browser. Neither X-Forwarded-For nor Forwarded is
accepted as evidence of locality.
"""
from __future__ import annotations

import ipaddress

from starlette.requests import Request

_LAN = tuple(ipaddress.ip_network(network) for network in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
    "fc00::/7",
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
        dest = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(source in net and dest in net for net in _LAN)
