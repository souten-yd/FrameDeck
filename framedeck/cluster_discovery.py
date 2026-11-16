"""Authenticated LAN discovery for an intermittently powered FrameDeck worker.

Only IPv4 local broadcasts are used. A beacon cannot enroll a worker unless
the NAS already holds the same explicit 256-bit pairing secret. LAN membership
alone is NOT sufficient to trust a machine.
"""
from __future__ import annotations

import ipaddress
import json
import hmac
import logging
import os
import secrets
import socket
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request
import threading
import time

from .cluster_worker import (
    CAPABILITY_PATH, ClusterConfigError, ReplayGuard, _secure_opener,
    response_signature, signed_headers, validate_secret, validate_worker_url,
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

    def __init__(self, controller, config_dir: Path) -> None:
        self.controller = controller
        self._file = config_dir / "cluster-discovery.json"
        self._lock = threading.RLock()
        self._secret: str | None = None
        self._load()
        self._stop = threading.Event()
        self._guard = ReplayGuard()
        self._threads: list[threading.Thread] = []
        self.pairing = None  # injected by Services before discovery threads start

    def _load(self) -> None:
        if not self._file.exists():
            return
        try:
            if self._file.is_symlink() or self._file.stat().st_mode & 0o077:
                raise ClusterConfigError("insecure discovery configuration")
            if self._file.stat().st_size > 1024:
                raise ClusterConfigError("discovery config too large")
            value = json.loads(self._file.read_text(encoding="utf-8"))
            self._secret = validate_secret(value["secret"])
        except (ValueError, TypeError, KeyError, OSError):
            logger.warning("Invalid LAN discovery config; automatic pairing disabled")

    def discovery_secret(self) -> str | None:
        with self._lock:
            return self._secret

    def enabled(self) -> bool:
        return self.discovery_secret() is not None

    def enable(self) -> str:
        """Provision a key once. Return it only in this admin-only mutation."""
        with self._lock:
            if self._secret:
                return self._secret
            with self.controller._lock:
                secret = (self.controller._peer or {}).get("secret") or secrets.token_hex(32)
            self._file.parent.mkdir(parents=True, exist_ok=True)
            temp = self._file.with_name(self._file.name + "." + secrets.token_hex(8) + ".tmp")
            try:
                fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as handle:
                    handle.write(json.dumps({"secret": secret}).encode("ascii"))
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp, self._file)
                self.controller._sync_config_dir()
            finally:
                temp.unlink(missing_ok=True)
            self._secret = secret
            return secret

    def install_secret(self, secret: str) -> None:
        """Persist a key derived by TWO approved hosts, never supplied by UI."""
        secret = validate_secret(secret)
        with self._lock:
            existing = self.discovery_secret()
            if existing and existing != secret and self.controller.status()["configured"]:
                raise ClusterConfigError("Refusing to replace an existing trusted peer")
            self._file.parent.mkdir(parents=True, exist_ok=True)
            if self._file.is_symlink():
                raise ClusterConfigError("Unsafe discovery configuration")
            temp = self._file.with_name(
                self._file.name + "." + secrets.token_hex(8) + ".tmp"
            )
            try:
                fd = os.open(str(temp), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, "wb") as handle:
                    handle.write(json.dumps({"secret": secret}).encode("ascii"))
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp, self._file)
                self.controller._sync_config_dir()
            finally:
                temp.unlink(missing_ok=True)
            self._secret = secret

    def disable(self) -> None:
        with self._lock:
            self._file.unlink(missing_ok=True)
            self._secret = None
            self.controller._sync_config_dir()

    def _verify_candidate(self, url: str, secret: str) -> bool:
        """Confirm the announced address actually serves the keyed worker.

        A spoofed/captured UDP beacon alone must never change QNAP's saved
        target. The candidate must prove possession of the secret over TCP.
        """
        headers = signed_headers(secret, "GET", CAPABILITY_PATH)
        try:
            with _secure_opener().open(
                Request(url + CAPABILITY_PATH, headers=headers, method="GET"),
                timeout=1.5,
            ) as response:
                if response.status != 200:
                    return False
                payload = response.read(8193)
                if len(payload) > 8192:
                    return False
                proof = response.headers.get("X-FrameDeck-Response-Signature", "")
                expected = response_signature(secret, headers["X-FrameDeck-Nonce"], payload)
                if not hmac.compare_digest(proof, expected):
                    return False
                data = json.loads(payload)
                return (
                    isinstance(data, dict) and data.get("worker") is True and
                    data.get("protocol_version") == 1 and
                    isinstance(data.get("transcode_available"), bool)
                )
        except (HTTPError, URLError, ValueError, OSError, TypeError):
            return False

    def accept_discovered_worker(self, url: str, secret: str) -> bool:
        try:
            url = validate_worker_url(url)
            secret = validate_secret(secret)
        except ClusterConfigError:
            return False
        if worker_secret() is not None:
            return False  # worker B must never register A as its worker
        with self._lock:
            if self._secret != secret:
                return False
            with self.controller._lock:
                peer = self.controller._peer
                if peer is not None and peer["secret"] != secret:
                    return False
                if peer is not None and peer["url"] == url:
                    return True  # recurring beacons never write to disk
            if not self._verify_candidate(url, secret):
                return False
            with self.controller._lock:
                # Repeat permission check after the network round trip.
                peer = self.controller._peer
                if self._secret != secret or (peer and peer["secret"] != secret):
                    return False
                try:
                    self.controller.configure(url, secret)
                except (ValueError, OSError) as exc:
                    logger.warning("Verified LAN discovery was not stored: %s",
                                   type(exc).__name__)
                    return False
        return True

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
        # Unsigned offers are never registered; they only appear temporarily
        # in the admin UI during the controller's pairing window.
        pairing = self.pairing
        if pairing is not None and pairing.accept_offer(packet, source_ip):
            return True
        secret = self.discovery_secret()
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
            return self.accept_discovered_worker(url, secret)
        except (ValueError, KeyError, TypeError, OverflowError):
            return False

    def _listen(self) -> None:
        while not self._stop.is_set():
            waiting = bool(self.pairing and self.pairing.active() and
                           not self.pairing.linux)
            if (self.discovery_secret() is None and not waiting) or (
                    worker_secret() is not None):
                self._stop.wait(2)
                continue
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                    sock.bind(("0.0.0.0", DISCOVERY_PORT))
                    sock.settimeout(1.5)
                    while not self._stop.is_set() and (
                            self.discovery_secret() or (
                                self.pairing and self.pairing.active()
                                and not self.pairing.linux
                            )):
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
            pair_offer = self.pairing.offer_packet(port) if (
                self.pairing is not None and port is not None
            ) else None
            if (secret is not None or pair_offer is not None) and port is not None:
                try:
                    packet = make_beacon(secret, port) if secret is not None else pair_offer
                    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                        # Limited LAN broadcast; no internet multicast/cloud API.
                        sock.sendto(packet, ("255.255.255.255", DISCOVERY_PORT))
                except OSError:
                    # A powered-down or isolated NAS must not affect B.
                    pass
            self._stop.wait(_DISCOVERY_PERIOD)
