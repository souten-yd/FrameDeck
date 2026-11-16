"""Mutually approved, keyless LAN pairing.

An ephemeral X25519 exchange derives the FrameDeck HMAC secret. The user
compares a short authentication string on BOTH local Web UIs; no secret is
typed, displayed or transferred. A mismatched code MUST be rejected. The
resulting shared secret is persisted only after approvals on both hosts.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import secrets
import threading
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request

from .cluster_worker import (
    ClusterConfigError, _secure_opener, set_runtime_worker_secret,
    validate_secret, validate_worker_url, worker_secret,
)

try:
    from cryptography.hazmat.primitives.asymmetric.x25519 import (
        X25519PrivateKey, X25519PublicKey,
    )
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    from cryptography.hazmat.primitives import hashes, serialization
except ImportError:  # QPKG bundles cryptography; older Ubuntu envs may not.
    X25519PrivateKey = X25519PublicKey = HKDF = hashes = serialization = None

logger = logging.getLogger("framedeck")
PAIRING_ANNOUNCE_PATH = "/api/cluster/pairing/hello"
PAIRING_COMMIT_PATH = "/api/cluster/pairing/commit"
PAIRING_TTL = 120
_HEX32 = "0123456789abcdef"
_ALLOWED_NETS = tuple(ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
))


class PairingError(ValueError):
    pass


def _valid_hex(value, length: int) -> bool:
    return isinstance(value, str) and len(value) == length and all(
        c in _HEX32 for c in value
    )


def _private_source(value: str) -> bool:
    if value == "testclient":  # FastAPI in-process integration tests
        return True
    try:
        addr = ipaddress.IPv4Address(value)
        return any(addr in net for net in _ALLOWED_NETS)
    except ipaddress.AddressValueError:
        return False


def _public(private) -> str:
    return private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    ).hex()


def _derive(private, their_public: str, token: str, worker_pub: str,
            nas_pub: str) -> tuple[str, str, bytes]:
    if not _valid_hex(their_public, 64) or not _valid_hex(token, 32):
        raise PairingError("Invalid pairing public key or token")
    try:
        shared = private.exchange(X25519PublicKey.from_public_bytes(
            bytes.fromhex(their_public)
        ))
    except (ValueError, TypeError) as exc:
        raise PairingError("Invalid remote ephemeral key") from exc
    # Bound the same transcript/roles and exact public keys on both hosts.
    transcript = b"framedeck-pair-v1|" + bytes.fromhex(token) + (
        bytes.fromhex(worker_pub) + bytes.fromhex(nas_pub)
    )
    digest = hashlib.sha256(transcript).digest()
    key = HKDF(
        algorithm=hashes.SHA256(), length=32,
        salt=digest, info=b"FrameDeck secure LAN pairing HMAC v1",
    ).derive(shared)
    code_bytes = hmac.new(key, b"visual-confirm-v1|" + transcript,
                          hashlib.sha256).digest()
    code = f"{int.from_bytes(code_bytes[:4], 'big') % 1000000:06d}"
    return key.hex(), code, digest


def _proof(secret: str, role: str, digest: bytes) -> str:
    return hmac.new(bytes.fromhex(secret),
                    b"pair-complete-v1|" + role.encode("ascii") + digest,
                    hashlib.sha256).hexdigest()


def _read_json_response(response) -> dict:
    with response:
        if response.status != 200:
            raise PairingError("Worker rejected the pairing exchange")
        raw = response.read(1025)
    if len(raw) > 1024:
        raise PairingError("Oversize pairing response")
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise PairingError("Invalid worker pairing response") from exc
    if not isinstance(value, dict):
        raise PairingError("Invalid worker pairing object")
    return value


class PairingManager:
    """Two-sided consent, bounded volatile handshakes, no privileged commands."""

    def __init__(self, config_dir: Path, cluster, discovery, *, linux: bool) -> None:
        self._lock = threading.RLock()
        self._config = config_dir
        self._file = config_dir / "cluster-worker-secret.json"
        self.cluster = cluster
        self.discovery = discovery
        self.linux = linux
        self._window = 0.0
        self._offers: dict[str, dict] = {}
        self._worker: dict | None = None
        self._nas: dict | None = None
        self._load_secret()

    @property
    def available(self) -> bool:
        return X25519PrivateKey is not None

    def _load_secret(self) -> None:
        if not self.linux or not self._file.exists():
            return
        try:
            if self._file.is_symlink() or self._file.stat().st_mode & 0o077:
                raise PairingError("Unsafe worker key file")
            if self._file.stat().st_size > 512:
                raise PairingError("Oversize worker key file")
            saved = json.loads(self._file.read_text("utf-8"))
            secret = validate_secret(saved["secret"])
            set_runtime_worker_secret(secret)
        except (OSError, ValueError, KeyError, TypeError):
            logger.warning("Invalid saved worker secret; no worker key loaded")

    def _persist_secret(self, secret: str) -> None:
        secret = validate_secret(secret)
        self._config.mkdir(parents=True, exist_ok=True)
        if self._file.is_symlink():
            raise PairingError("Unsafe worker key symlink")
        tmp = self._file.with_name(
            self._file.name + "." + secrets.token_hex(8) + ".tmp"
        )
        try:
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(str(tmp), flags, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write((json.dumps({"secret": secret}) + "\n").encode())
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self._file)
            self.cluster._sync_config_dir()
        finally:
            tmp.unlink(missing_ok=True)
        set_runtime_worker_secret(secret)

    def arm(self) -> dict:
        """Explicit local admin action; does not trust any discovered host."""
        if not self.available:
            raise PairingError("Pairing crypto component is not installed")
        with self._lock:
            if self.linux:
                if worker_secret() is not None:
                    return {"already_paired": True}
                private = X25519PrivateKey.generate()
                self._worker = {
                    "id": secrets.token_hex(16), "private": private,
                    "pub": _public(private), "until": time.monotonic() + PAIRING_TTL,
                    "pending": None, "attempts": 0,
                }
            else:
                if self.cluster.status()["configured"]:
                    return {"already_paired": True}
                self._nas = None
                self._offers.clear()
            self._window = time.monotonic() + PAIRING_TTL
        return {"armed": True, "expires_in": PAIRING_TTL}

    def active(self) -> bool:
        with self._lock:
            return time.monotonic() < self._window

    def offer_packet(self, port: int) -> bytes | None:
        if not self.linux or not self.active():
            return None
        with self._lock:
            worker = self._worker
            if worker is None:
                return None
            return json.dumps({
                "kind": "pair_offer", "version": 1, "id": worker["id"],
                "pub": worker["pub"], "port": port,
            }, separators=(",", ":")).encode("ascii")

    def accept_offer(self, packet: bytes, source_ip: str) -> bool:
        """Untrusted LAN offers are only candidates, NEVER automatically paired."""
        if self.linux or not self.active() or not _private_source(source_ip):
            return False
        if not isinstance(packet, bytes) or len(packet) > 300:
            return False
        try:
            data = json.loads(packet)
            if (not isinstance(data, dict) or set(data) !=
                    {"kind", "version", "id", "pub", "port"} or
                    data["kind"] != "pair_offer" or data["version"] != 1 or
                    not _valid_hex(data["id"], 32) or
                    not _valid_hex(data["pub"], 64) or
                    type(data["port"]) is not int or
                    not 1 <= data["port"] <= 65535):
                return False
            url = validate_worker_url(
                f"http://{source_ip}:{data['port']}"
            )
        except (ValueError, TypeError, KeyError):
            return False
        with self._lock:
            if len(self._offers) >= 8 and data["id"] not in self._offers:
                return False
            self._offers[data["id"]] = {
                "url": url, "pub": data["pub"],
                "seen": time.monotonic(),
            }
        return True

    def hello(self, pairing_id: str, nas_public: str, source_ip: str) -> dict:
        """Linux receives QNAP's ephemeral public key, not a shared secret."""
        if not self.linux or not _private_source(source_ip):
            raise PairingError("Pairing is limited to the local network")
        with self._lock:
            w = self._worker
            if (not self.active() or w is None or pairing_id != w["id"]
                    or not _valid_hex(nas_public, 64)):
                raise PairingError("Worker pairing window is not active")
            if w["attempts"] >= 4:
                raise PairingError("Too many pairing requests")
            if w["pending"] is not None:
                if w["pending"]["nas_pub"] != nas_public or w["pending"]["ip"] != source_ip:
                    raise PairingError("Another QNAP pairing request is pending")
                return {"id": w["id"], "worker_pub": w["pub"]}
            w["attempts"] += 1
            secret, code, digest = _derive(
                w["private"], nas_public, pairing_id,
                w["pub"], nas_public,
            )
            w["pending"] = {
                "nas_pub": nas_public, "ip": source_ip,
                "secret": secret, "code": code, "digest": digest,
                "approved": False, "committed": False,
            }
            return {"id": w["id"], "worker_pub": w["pub"]}

    def join(self, pairing_id: str) -> dict:
        """QNAP initiates TCP challenge ONLY after choosing a discovered offer."""
        if self.linux or not self.active():
            raise PairingError("QNAP pairing window is closed")
        with self._lock:
            offer = self._offers.get(pairing_id)
            if (offer is None or time.monotonic()-offer["seen"] > 25 or
                    self._nas is not None):
                raise PairingError("No fresh worker candidate")
            private = X25519PrivateKey.generate()
            nas_pub = _public(private)
            url = offer["url"]
            worker_pub = offer["pub"]
        body = json.dumps(
            {"id": pairing_id, "nas_pub": nas_pub},
            separators=(",", ":"),
        ).encode()
        try:
            response = _secure_opener().open(
                Request(url + PAIRING_ANNOUNCE_PATH, data=body,
                        headers={"Content-Type": "application/json"},
                        method="POST"),
                timeout=3.0,
            )
            result = _read_json_response(response)
        except (HTTPError, URLError, OSError, TimeoutError, ValueError) as exc:
            raise PairingError("Could not reach the offered Linux worker") from exc
        if (result.get("id") != pairing_id or
                result.get("worker_pub") != worker_pub):
            raise PairingError("Worker public key differs from LAN offer")
        secret, code, digest = _derive(
            private, worker_pub, pairing_id, worker_pub, nas_pub,
        )
        with self._lock:
            if not self.active() or self._nas is not None:
                raise PairingError("QNAP pairing window has expired")
            self._nas = {
                "id": pairing_id, "url": url, "secret": secret,
                "code": code, "digest": digest,
            }
        return {"code": code}

    def approve_worker(self) -> dict:
        if not self.linux:
            raise PairingError("Not a Linux worker")
        with self._lock:
            w = self._worker
            if not self.active() or not w or not w["pending"]:
                raise PairingError("No active QNAP request")
            w["pending"]["approved"] = True
        return {"approved": True}

    def commit(self, pairing_id: str, proof: str, source_ip: str) -> dict:
        """Complete only after Linux UI approval and matching ECDH MAC."""
        if not self.linux or not _private_source(source_ip):
            raise PairingError("Pairing origin is not trusted")
        with self._lock:
            w = self._worker
            if not self.active() or not w or w["id"] != pairing_id:
                raise PairingError("Pairing window expired")
            p = w["pending"]
            if not p or not p["approved"] or p["ip"] != source_ip:
                raise PairingError("Linux operator approval is still required")
            if not isinstance(proof, str) or not hmac.compare_digest(
                proof, _proof(p["secret"], "qnap", p["digest"])
            ):
                raise PairingError("Pairing authentication failed")
            if not p["committed"]:
                self._persist_secret(p["secret"])
                p["committed"] = True
            return {"ack": _proof(p["secret"], "linux", p["digest"])}

    def finish(self) -> dict:
        """QNAP's local admin approves the matching visual verification code."""
        if self.linux:
            raise PairingError("Not a QNAP controller")
        with self._lock:
            n = self._nas
            if not self.active() or not n:
                raise PairingError("No active Linux pairing session")
            url, pairing_id = n["url"], n["id"]
            secret, digest = n["secret"], n["digest"]
        body = json.dumps({
            "id": pairing_id,
            "proof": _proof(secret, "qnap", digest),
        }, separators=(",", ":")).encode()
        try:
            response = _secure_opener().open(
                Request(url + PAIRING_COMMIT_PATH, data=body,
                        headers={"Content-Type": "application/json"},
                        method="POST"),
                timeout=3.0,
            )
            result = _read_json_response(response)
        except HTTPError as exc:
            raise PairingError(
                "Linux side has not yet approved this confirmation code"
            ) from exc
        except (URLError, OSError, TimeoutError, ValueError) as exc:
            raise PairingError("Linux pairing completion failed") from exc
        if not isinstance(result.get("ack"), str) or not hmac.compare_digest(
            result["ack"], _proof(secret, "linux", digest)
        ):
            raise PairingError("Linux acknowledgment authentication failed")
        # No QNAP automatic worker registration without both explicit
        # approvals. Persist the shared derived secret, never publish it.
        self.cluster.configure(url, secret)
        self.discovery.install_secret(secret)
        with self._lock:
            self._nas = None
            self._window = 0
            self._offers.clear()
        self.cluster.probe_once()
        return {"paired": True}

    def status(self) -> dict:
        with self._lock:
            remaining = max(0, round(self._window - time.monotonic()))
            result = {
                "role": "linux" if self.linux else "qnap",
                "available": self.available,
                "open": remaining > 0,
                "expires_in": remaining,
                "worker_key_configured": bool(worker_secret()) if self.linux else None,
            }
            if self.linux:
                w = self._worker
                p = w.get("pending") if w and remaining else None
                result["confirmation_code"] = p["code"] if p else None
                result["request_from"] = p["ip"] if p else None
                result["approved"] = bool(p and p["approved"])
            else:
                result["candidates"] = [
                    {"id": id_, "url": x["url"]}
                    for id_, x in self._offers.items()
                    if remaining and time.monotonic()-x["seen"] <= 25
                ]
                result["confirmation_code"] = (
                    self._nas["code"] if self._nas and remaining else None
                )
                result["selected_url"] = (
                    self._nas["url"] if self._nas and remaining else None
                )
            return result

    def shutdown(self) -> None:
        # Avoid leaking the process-local Linux test secret to QNAP in
        # in-process two-application regression tests.
        if self.linux:
            set_runtime_worker_secret(None)
