"""LAN-only optional FrameDeck worker discovery and authenticated health monitoring.

No media is sent to a worker in this foundation release.  The NAS stays the
sole source of truth and continues to serve every existing request locally.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import threading
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

logger = logging.getLogger("framedeck")
PROTOCOL_VERSION = 1
CAPABILITY_PATH = "/api/cluster/worker/capabilities"
POLL_INTERVAL_SECONDS = 10
MAX_STALE_SECONDS = 35
_TOKEN_PATTERN = re.compile(r"^[a-fA-F0-9]{64,128}$")
_LAN_V4 = tuple(ipaddress.ip_network(net) for net in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
    "100.64.0.0/10",  # opt-in Tailscale/CGNAT address
))
_LAN_V6 = ipaddress.ip_network("fc00::/7")


class ClusterConfigError(ValueError):
    """Invalid/insecure worker registration."""


def validate_secret(value: str) -> str:
    if not isinstance(value, str) or not _TOKEN_PATTERN.fullmatch(value):
        raise ClusterConfigError("共有キーは64～128文字の16進数で指定してください")
    return value.lower()


def validate_worker_url(value: str) -> str:
    """Accept only explicit LAN IP literals; never resolve arbitrary DNS.

    This is also used when loading saved settings and immediately before
    polling, preventing redirects, credentials, DNS rebinding and SSRF.
    """
    if not isinstance(value, str) or len(value) > 250:
        raise ClusterConfigError("ワーカーのURLが不正です")
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in ("http", "https") or
                parsed.username is not None or parsed.password is not None or
                parsed.path not in ("", "/") or parsed.query or parsed.fragment):
            raise ValueError("URL shape")
        addr = ipaddress.ip_address(parsed.hostname or "")
        port = parsed.port
        if port is None or not (1 <= port <= 65535):
            raise ValueError("Explicit port required")
        if isinstance(addr, ipaddress.IPv4Address):
            allowed = any(addr in net for net in _LAN_V4)
        else:
            allowed = addr in _LAN_V6
        if not allowed:
            raise ValueError("Not LAN")
    except (ValueError, AttributeError) as exc:
        raise ClusterConfigError(
            "LAN/Tailscale内のIPアドレスとポートを指定してください（ホスト名・リダイレクト不可）"
        ) from exc
    return f"{parsed.scheme}://{parsed.netloc}"


def _canonical(method: str, path: str, timestamp: str, nonce: str,
               body: bytes = b"") -> bytes:
    return "\n".join((
        method.upper(), path, timestamp, nonce,
        hashlib.sha256(body).hexdigest(),
    )).encode("utf-8")


def signed_headers(secret: str, method: str, path: str,
                   body: bytes = b"", *, now: int | None = None,
                   nonce: str | None = None) -> dict[str, str]:
    key = bytes.fromhex(validate_secret(secret))
    timestamp = str(int(time.time()) if now is None else now)
    nonce = nonce or secrets.token_hex(16)
    signature = hmac.new(
        key, _canonical(method, path, timestamp, nonce, body), hashlib.sha256,
    ).hexdigest()
    return {
        "X-FrameDeck-Timestamp": timestamp,
        "X-FrameDeck-Nonce": nonce,
        "X-FrameDeck-Signature": signature,
    }


def response_signature(secret: str, nonce: str, payload: bytes) -> str:
    key = bytes.fromhex(validate_secret(secret))
    digest = hashlib.sha256(payload).hexdigest()
    return hmac.new(key, f"response\n{nonce}\n{digest}".encode(),
                    hashlib.sha256).hexdigest()


class ReplayGuard:
    """Bounded one-use nonce registry; intended for short-lived control API."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seen: dict[str, float] = {}

    def verify(self, secret: str, method: str, path: str,
               headers, body: bytes = b"", *, now: int | None = None) -> bool:
        now = int(time.time()) if now is None else now
        timestamp = headers.get("x-framedeck-timestamp", "")
        nonce = headers.get("x-framedeck-nonce", "")
        signature = headers.get("x-framedeck-signature", "")
        if (not re.fullmatch(r"\d{1,12}", timestamp) or
                not re.fullmatch(r"[0-9a-f]{32}", nonce) or
                not re.fullmatch(r"[0-9a-f]{64}", signature) or
                abs(int(timestamp) - now) > 30):
            return False
        key = bytes.fromhex(validate_secret(secret))
        expected = hmac.new(
            key, _canonical(method, path, timestamp, nonce, body),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return False
        with self._lock:
            self._seen = {
                item: expiry for item, expiry in self._seen.items()
                if expiry > now
            }
            if nonce in self._seen or len(self._seen) >= 4096:
                return False
            self._seen[nonce] = float(now + 65)
            return True


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def _secure_opener():
    # Avoid inherited HTTP_PROXY and urllib's otherwise automatic redirect.
    return build_opener(ProxyHandler({}), _NoRedirect())


class ClusterController:
    """One optional intermittently-powered worker, with no hot-path IO."""

    def __init__(self, config_dir: Path) -> None:
        self._file = config_dir / "cluster-worker.json"
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._peer: dict | None = None
        self._capabilities: dict | None = None
        self._last_seen: float | None = None
        self._error = "not_configured"
        self._load()

    def _load(self) -> None:
        if not self._file.exists():
            return
        try:
            stat = self._file.stat()
            if stat.st_mode & 0o077:
                raise ClusterConfigError("unsafe file permissions")
            if stat.st_size > 4096:
                raise ClusterConfigError("config too large")
            data = json.loads(self._file.read_text(encoding="utf-8"))
            self._peer = {
                "url": validate_worker_url(data["url"]),
                "secret": validate_secret(data["secret"]),
            }
            self._error = "not_checked"
        except (OSError, ValueError, KeyError, TypeError):
            logger.warning("Cluster worker configuration is invalid; disabled")
            self._peer = None
            self._error = "invalid_configuration"

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            self._thread = threading.Thread(
                target=self._monitor, name="framedeck-cluster-monitor",
                daemon=True,
            )
            self._thread.start()

    def shutdown(self) -> None:
        self._stop.set()
        self._wake.set()
        with self._lock:
            thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.5)

    def _sync_config_dir(self) -> None:
        if not hasattr(os, "O_DIRECTORY"):
            return
        try:
            dir_fd = os.open(str(self._file.parent), os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            # Some QNAP-backed filesystems may not support directory fsync.
            # Do not leave in-memory state out of sync with an already renamed
            # file merely because its parent directory could not be synced.
            logger.warning("Cluster configuration directory fsync unavailable")

    def configure(self, url: str, secret: str) -> None:
        peer = {"url": validate_worker_url(url),
                "secret": validate_secret(secret)}
        content = (json.dumps(peer, ensure_ascii=False) + "\n").encode()
        self._file.parent.mkdir(parents=True, exist_ok=True)
        temp = self._file.with_name(self._file.name + "." + secrets.token_hex(8) + ".tmp")
        try:
            fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self._file)
            self._sync_config_dir()
        finally:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
        with self._lock:
            self._peer = peer
            self._capabilities = None
            self._last_seen = None
            self._error = "not_checked"
        self._wake.set()

    def clear(self) -> None:
        with self._lock:
            # Do not leave a saved secret behind if the process is terminated.
            self._file.unlink(missing_ok=True)
            self._sync_config_dir()
            self._peer = None
            self._capabilities = None
            self._last_seen = None
            self._error = "not_configured"
        self._wake.set()

    def status(self) -> dict:
        with self._lock:
            peer = self._peer
            online = bool(
                peer and self._last_seen is not None and
                time.time() - self._last_seen <= MAX_STALE_SECONDS
            )
            return {
                "configured": bool(peer),
                "url": peer["url"] if peer else None,
                "online": online,
                "last_seen": self._last_seen,
                "error": None if online else self._error,
                "capabilities": dict(self._capabilities) if online and self._capabilities else None,
                "offload_active": False,  # enabled in a subsequent, separately tested PR
            }

    def probe_once(self) -> None:
        with self._lock:
            peer = dict(self._peer) if self._peer else None
        if peer is None:
            return
        url = validate_worker_url(peer["url"]) + CAPABILITY_PATH
        headers = signed_headers(peer["secret"], "GET", CAPABILITY_PATH)
        request = Request(url, method="GET", headers=headers)
        try:
            with _secure_opener().open(request, timeout=2.0) as response:
                if response.status != 200:
                    raise ValueError("status")
                payload = response.read(8193)
                if len(payload) > 8192:
                    raise ValueError("response too large")
                signature = response.headers.get("X-FrameDeck-Response-Signature", "")
                expected = response_signature(
                    peer["secret"], headers["X-FrameDeck-Nonce"], payload,
                )
                if not hmac.compare_digest(signature, expected):
                    raise ValueError("bad response signature")
                data = json.loads(payload)
                if (data.get("protocol_version") != PROTOCOL_VERSION or
                        data.get("worker") is not True or
                        not isinstance(data.get("transcode_available"), bool)):
                    raise ValueError("incompatible worker")
                capabilities = {
                    "protocol_version": PROTOCOL_VERSION,
                    "version": str(data.get("version", ""))[:40],
                    "transcode_available": data["transcode_available"],
                }
        except (HTTPError, URLError, OSError, ValueError, TypeError) as exc:
            # No secrets or detailed server response in logs / status.
            logger.debug("Cluster worker unavailable: %s", type(exc).__name__)
            with self._lock:
                if self._peer == peer:
                    self._capabilities = None
                    self._last_seen = None
                    self._error = "unreachable_or_incompatible"
            return
        with self._lock:
            if self._peer == peer:
                self._capabilities = capabilities
                self._last_seen = time.time()
                self._error = ""

    def _monitor(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                configured = self._peer is not None
            if configured:
                self.probe_once()
            self._wake.wait(POLL_INTERVAL_SECONDS if configured else 300)
            self._wake.clear()


def worker_secret() -> str | None:
    value = os.environ.get("FRAMEDECK_CLUSTER_WORKER_SECRET", "")
    try:
        return validate_secret(value) if value else None
    except ClusterConfigError:
        logger.warning("Invalid FRAMEDECK_CLUSTER_WORKER_SECRET; worker disabled")
        return None
