"""Authenticated LAN discovery for an intermittently powered FrameDeck worker.

Only IPv4 local broadcasts are used. A beacon cannot enroll a worker unless
the NAS already holds the same explicit 256-bit pairing secret. LAN membership
alone is NOT sufficient to trust a machine.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import secrets
import socket
import threading
import time

from .cluster_worker import (
    ReplayGuard, signed_headers, validate_secret, validate_worker_url,
    worker_secret,
)

logger = logging.getLogger("framedeck")

DISCOVERY_PORT = 31964
DISCOVERY_PATH = "/api/cluster/discovery/beacon"
_DISCOVERY_PERIOD = 8
_MAX_FRAME_BYTES = 512
_ALLOWED_NETS = tuple(ipaddress.ip_network(net) for net in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
))


def _worker_port() -> int | None:
    try:
        value = os.environ.get("FRAMEDECK_CLUSTER_WORKER_PORT", "9000")
        if not value.isascii() or not value.isdecimal():
            return None
        port = int(value)
        if not 1 <= port <= 65535:
            return None
        return port
    except (ValueError, OverflowError):
        return None


def _body(port: int) -> bytes:
    return json.dumps(
        {"v": 1, "port": port}, separators=(",", ":"), sort_keys=True,
    ).encode("ascii")


def make_beacon(secret: str, port: int, *, now: int | None = None,
                nonce: str | None = None) -> bytes:
    """Build one signed, bounded UDP discovery advertisement."""
    validate_secret(secret)
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("Invalid worker port")
    headers = signed_headers(
        secret, "POST", DISCOVERY_PATH, _body(port), now=now, nonce=nonce,
    )
    return json.dumps({
        "v": 1, "port": port,
        "ts": headers["X-FrameDeck-Timestamp"],
        "nonce": headers["X-FrameDeck-Nonce"],
        "sig": headers["X-FrameDeck-Signature"],
    }, separators=(",", ":"), sort_keys=True).encode("ascii")


class LanDiscovery:
    """Accept only pre-authorized matching beacons and avoid repeated NAS writes."""

    def __init__(self, controller) -> None:
        self.controller = controller
        self._stop = threading.Event()
        self._guard = ReplayGuard()
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        # Each job idles without network work if its corresponding mode is off.
        for target, name in (
            (self._listen, "framedeck-lan-discovery"),
            (self._announce, "framedeck-worker-beacon"),
        ):
            thread = threading.Thread(target=target, name=name, daemon=True)
            self._threads.append(thread)
            thread.start()

    def shutdown(self) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=2.5)

    def accept(self, packet: bytes, source_ip: str, *, now: int | None = None) -> bool:
        """Check source locality, signature, freshness and replay before pairing.

        Called from the bounded UDP listener; contains no disk writes for
        repeated beacons from the same registered address.
        """
        secret = self.controller.discovery_secret()
        if secret is None or not isinstance(packet, bytes) or len(packet) > _MAX_FRAME_BYTES:
            return False
        try:
            ip = ipaddress.IPv4Address(source_ip)
            if not any(ip in network for network in _ALLOWED_NETS):
                return False
            message = json.loads(packet)
            if not isinstance(message, dict) or set(message) != {
                "v", "port", "ts", "nonce", "sig",
            } or message["v"] != 1:
                return False
            port = message["port"]
            if type(port) is not int or not 1 <= port <= 65535:
                return False
            headers = {
                "x-framedeck-timestamp": message["ts"],
                "x-framedeck-nonce": message["nonce"],
                "x-framedeck-signature": message["sig"],
            }
            if not self._guard.verify(
                secret, "POST", DISCOVERY_PATH, headers, _body(port), now=now,
            ):
                return False
            url = validate_worker_url(f"http://{source_ip}:{port}")
            return self.controller.accept_discovered_worker(url, secret)
        except (ValueError, KeyError, TypeError, OverflowError):
            return False

    def _listen(self) -> None:
        while not self._stop.is_set():
            if self.controller.discovery_secret() is None:
                self._stop.wait(2)
                continue
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                    sock.bind(("0.0.0.0", DISCOVERY_PORT))
                    sock.settimeout(1.5)
                    while not self._stop.is_set() and self.controller.discovery_secret():
                        try:
                            packet, addr = sock.recvfrom(_MAX_FRAME_BYTES + 1)
                        except socket.timeout:
                            continue
                        if len(packet) <= _MAX_FRAME_BYTES:
                            self.accept(packet, addr[0])
            except OSError as exc:
                # UDP availability cannot affect FrameDeck playback.
                logger.warning("Cluster LAN discovery listener unavailable: %s",
                               type(exc).__name__)
                self._stop.wait(10)

    def _announce(self) -> None:
        while not self._stop.is_set():
            secret = worker_secret()
            port = _worker_port()
            if secret is not None and port is not None:
                try:
                    packet = make_beacon(secret, port)
                    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                        # Limited LAN broadcast; no internet multicast/cloud API.
                        sock.sendto(packet, ("255.255.255.255", DISCOVERY_PORT))
                except OSError:
                    # A powered-down or isolated NAS must not affect B.
                    pass
            self._stop.wait(_DISCOVERY_PERIOD)
