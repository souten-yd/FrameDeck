"""Automatic, scoped read-only video access for an authenticated worker.

The owner keeps the media. FFmpeg reads a time-limited HTTP Range URL for
one library video, so pairing never needs mount privileges or SMB credentials.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import socket
import threading
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit, urlencode

from . import __version__
from .cluster_worker import validate_worker_url, response_signature
from .worker_offload import WorkerOffloadError, root_fingerprint, resolve_mounted_video, offload_enabled

SOURCE_PATH = "/api/cluster/source"
TICKET_TTL = 24 * 3600


def _encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def make_ticket(secret: str, payload: dict) -> str:
    body = _encode(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    mac = hmac.new(bytes.fromhex(secret), ("source-v1|" + body).encode(), hashlib.sha256).hexdigest()
    return body + "." + mac


def read_ticket(secret: str, ticket: str) -> dict:
    try:
        if len(ticket) > 12000:
            raise ValueError()
        body, mac = ticket.split(".")
        expected = hmac.new(bytes.fromhex(secret), ("source-v1|" + body).encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(mac, expected):
            raise ValueError()
        data = json.loads(_decode(body))
        if (not isinstance(data, dict) or type(data.get("expires")) is not int
                or not time.time() < data["expires"] <= time.time() + TICKET_TTL + 60
                or type(data.get("size")) is not int or data["size"] < 0
                or not isinstance(data.get("mtime"), int)
                or not isinstance(data.get("root"), str)
                or not isinstance(data.get("relative"), str)):
            raise ValueError()
        return data
    except (ValueError, TypeError, KeyError, UnicodeError) as exc:
        raise WorkerOffloadError("invalid or expired source ticket") from exc


def library_source(roots: list[dict], root: str, relative: str) -> Path:
    if (not relative or "\\" in relative or "\x00" in relative
            or any(x in ("", ".", "..") for x in relative.split("/"))):
        raise WorkerOffloadError("invalid video path")
    allowed = next((r for r in roots if r.get("kind") in ("video", "any")
                    and os.path.normpath(r["path"]) == os.path.normpath(root)), None)
    if allowed is None:
        raise WorkerOffloadError("video root is no longer shared")
    base = Path(allowed["path"]).resolve()
    path = (base / relative).resolve()
    if not path.is_relative_to(base) or not path.is_file():
        raise WorkerOffloadError("video is outside the library")
    from .video.stream import VIDEO_MIME
    if path.suffix.lower() not in VIDEO_MIME:
        raise WorkerOffloadError("not a video")
    return path


class WorkerSources:
    def __init__(self, services):
        self.services = services
        self._file = services.paths.config_dir / "cluster-parent.json"
        self._lock = threading.RLock()
        self._parent = None
        self._seen = 0.0
        self._metadata = {}
        try:
            if not self._file.is_symlink() and self._file.stat().st_size <= 16384:
                saved = json.loads(self._file.read_text())
                validate_worker_url(saved["url"])
                self._parent = saved
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def announcement(self) -> dict:
        return {
            "name": socket.gethostname(), "version": __version__,
            "port": getattr(self.services, "listen_port", 9000),
            "roots": [{"id": root_fingerprint(r["path"]),
                       "name": (r.get("display_name") or Path(r["path"]).name).encode("utf-8")[:64].decode("utf-8", "ignore")}
                      for r in self.services.library.list_roots()
                      if r.get("kind") in ("video", "any")][:64],
        }

    def accept_announcement(self, encoded: str, proof: str, nonce: str,
                            secret: str, peer_ip: str) -> None:
        try:
            if len(encoded) > 16384:
                raise ValueError()
            raw = _decode(encoded)
            if not hmac.compare_digest(proof, response_signature(secret, nonce, raw)):
                raise ValueError()
            data = json.loads(raw)
            port = data["port"]
            if type(port) is not int or not 1 <= port <= 65535:
                raise ValueError()
            host = f"[{peer_ip}]" if ":" in peer_ip else peer_ip
            url = validate_worker_url(f"http://{host}:{port}")
            roots = data["roots"]
            if (not isinstance(roots, list) or len(roots) > 64 or
                    any(not isinstance(r, dict) or len(r.get("id", "")) != 64
                        or any(c not in "0123456789abcdef" for c in r["id"])
                        or not isinstance(r.get("name"), str) or len(r["name"]) > 256
                        for r in roots)):
                raise ValueError()
            parent = {"url": url, "name": str(data["name"])[:128],
                      "version": str(data["version"])[:40], "roots": roots}
        except (ValueError, KeyError, TypeError, UnicodeError) as exc:
            raise WorkerOffloadError("invalid parent announcement") from exc
        with self._lock:
            if self._parent != parent:
                self._file.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(mode="w", dir=self._file.parent,
                                                 encoding="utf-8", delete=False) as handle:
                    tmp = Path(handle.name)
                    try:
                        handle.write(json.dumps(parent))
                        handle.flush()
                        os.fsync(handle.fileno())
                        tmp.replace(self._file)
                    finally:
                        tmp.unlink(missing_ok=True)
            self._parent = parent
            self._seen = time.time()

    def status(self) -> dict:
        from .cluster_worker import worker_secret
        with self._lock:
            paired = worker_secret() is not None
            return {"configured": bool(paired and self._parent),
                    **(self._parent if paired and self._parent else {}),
                    "online": bool(paired and self._parent and time.time() - self._seen < 35),
                    "last_seen": self._seen or None,
                    "source_access": "automatic-http" if paired and self._parent else None}

    def roots(self) -> list[str]:
        status = self.status()
        return [r["id"] for r in status.get("roots", [])] if status["online"] else []

    def clear(self):
        with self._lock:
            self._parent = None
            self._seen = 0
            self._metadata.clear()
            self._file.unlink(missing_ok=True)

    def issue(self, root: str, relative: str) -> dict:
        peer = self.services.cluster.verified_peer()
        if not peer or not offload_enabled(self.services.settings):
            raise WorkerOffloadError("worker not enabled")
        path = library_source(self.services.library.list_roots(), root, relative)
        stat = path.stat()
        # Use the same route to the worker as its health probe; never a browser Host.
        dest = urlsplit(peer["url"])
        family = socket.AF_INET6 if ":" in dest.hostname else socket.AF_INET
        with socket.socket(family, socket.SOCK_DGRAM) as sock:
            sock.connect((dest.hostname, dest.port))
            host = sock.getsockname()[0]
        if ":" in host:
            host = f"[{host}]"
        base = validate_worker_url(f"http://{host}:{getattr(self.services, 'listen_port', 9000)}")
        ticket = make_ticket(peer["secret"], {"root": root, "relative": relative,
                             "size": stat.st_size, "mtime": stat.st_mtime_ns,
                             "expires": int(time.time()) + TICKET_TTL})
        return {"source_url": base + SOURCE_PATH + "?" + urlencode({"ticket": ticket})}

    def resolve(self, root: str, relative: str, source_url: str | None, secret: str) -> str:
        if not source_url:
            return str(resolve_mounted_video(root, relative))
        from urllib.parse import parse_qs
        parent = self.status()
        parsed = urlsplit(source_url)
        if (not parent["online"] or parsed.scheme + "://" + parsed.netloc != parent["url"]
                or parsed.path != SOURCE_PATH or parsed.fragment):
            raise WorkerOffloadError("source is not the authenticated parent")
        query = parse_qs(parsed.query, strict_parsing=True)
        if set(query) != {"ticket"} or len(query["ticket"]) != 1:
            raise WorkerOffloadError("invalid source URL")
        data = read_ticket(secret, query["ticket"][0])
        if (data["root"] != root or data["relative"] != relative
                or root_fingerprint(root) not in self.roots()):
            raise WorkerOffloadError("source is not shared by the parent")
        with self._lock:
            now = time.time()
            self._metadata = {k: v for k, v in self._metadata.items() if v["expires"] > now}
            if len(self._metadata) >= 512 and source_url not in self._metadata:
                raise WorkerOffloadError("too many active sources")
            self._metadata[source_url] = {**data, "identity": parent["url"] + "|" + root + "|" + relative}
        return source_url

    def metadata(self, path: str) -> dict | None:
        with self._lock:
            return self._metadata.get(path)
