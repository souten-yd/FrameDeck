"""Opt-in HLS/AAC worker delegation without exposing the worker to browsers.

The QNAP remains the only browser origin and metadata authority.  Signed
requests allow the worker to transcode only explicitly read-only mounted
sources; opaque, volatile proxy keys never reveal an absolute file path.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
import threading
import time
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request

from .cluster_worker import _secure_opener, response_signature, signed_headers, validate_worker_url
from .worker_offload import WorkerOffloadError, remote_eligible

logger = logging.getLogger("framedeck")
WORKER_HLS_START = "/api/cluster/worker/hls/start"
WORKER_HLS_FILE = "/api/cluster/worker/hls/file"
WORKER_HLS_STOP = "/api/cluster/worker/hls/stop"
HLS_ALLOWED = frozenset(("360p", "480p", "720p", "1080p", "original-aac"))
_KEY = re.compile(r"^[0-9a-f]{64}$")
_FILE = re.compile(r"^(?:360p|480p|720p|1080p|original-aac)/(?:playlist\.m3u8|init\.mp4|segment_[0-9]{5,9}\.m4s)$")


def valid_hls_file(relative: str) -> bool:
    return relative == "master.m3u8" or bool(_FILE.fullmatch(relative))


def hls_eligible(controller, root: str, settings=None) -> bool:
    if not remote_eligible(controller, root, settings):
        return False
    return controller.status().get("capabilities", {}).get("remote_hls") is True


def _signed_open(controller, endpoint: str, *, timeout: float):
    peer = controller.verified_peer()
    if not peer:
        raise WorkerOffloadError("worker unavailable")
    request_headers = signed_headers(peer["secret"], "GET", endpoint)
    try:
        result = _secure_opener().open(Request(
            validate_worker_url(peer["url"]) + endpoint,
            headers=request_headers, method="GET",
        ), timeout=timeout)
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
        raise WorkerOffloadError("worker connection failed") from exc
    return result, peer, request_headers


def start_remote_hls(controller, root: str, relative: str, profiles: list[str],
                     height: int, start: float, session: str) -> str:
    if (not profiles or len(profiles) > 4 or
            any(profile not in HLS_ALLOWED for profile in profiles)):
        raise WorkerOffloadError("unsupported HLS profile")
    params = {
        "source_root": root, "relative_path": relative, "profiles": ",".join(profiles),
        "height": str(int(height)), "start": str(float(start)), "session": session[:64],
    }
    provider = getattr(controller, "source_provider", None)
    if controller.status().get("capabilities", {}).get("automatic_sources") and provider:
        params.update(provider(root, relative))
    path = WORKER_HLS_START + "?" + urlencode(params)
    response, peer, headers = _signed_open(controller, path, timeout=8)
    try:
        with response:
            if response.status != 200:
                raise WorkerOffloadError("worker HLS start failed")
            payload = response.read(4097)
            if len(payload) > 4096:
                raise WorkerOffloadError("oversized HLS response")
            proof = response.headers.get("X-FrameDeck-Response-Signature", "")
            if not hmac.compare_digest(proof, response_signature(
                peer["secret"], headers["X-FrameDeck-Nonce"], payload,
            )):
                raise WorkerOffloadError("invalid HLS start signature")
            value = json.loads(payload)
            key = value.get("key") if isinstance(value, dict) else None
            if not isinstance(key, str) or not _KEY.fullmatch(key):
                raise WorkerOffloadError("invalid HLS key")
            return key
    except (ValueError, TypeError, OSError) as exc:
        raise WorkerOffloadError("invalid HLS response") from exc


def open_remote_hls_file(controller, key: str, relative: str):
    if not _KEY.fullmatch(key) or not valid_hls_file(relative):
        raise WorkerOffloadError("invalid HLS file")
    resource = WORKER_HLS_FILE + "?" + urlencode({"key": key, "relative": relative})
    response, peer, headers = _signed_open(controller, resource, timeout=12)
    try:
        if response.status != 200:
            raise WorkerOffloadError("worker HLS file unavailable")
        proof = response.headers.get("X-FrameDeck-Stream-Proof", "")
        if not hmac.compare_digest(proof, response_signature(
            peer["secret"], headers["X-FrameDeck-Nonce"], b"",
        )):
            raise WorkerOffloadError("invalid HLS stream proof")
        return response
    except Exception:
        response.close()
        raise


@dataclass
class RemoteHls:
    proxy_key: str
    worker_key: str
    media_id: str
    source_path: str
    profiles: tuple[str, ...]
    height: int
    start: float
    owner: str
    seen: float


class RemoteHlsSessions:
    """Volatile, bounded per-media routing, never saved on QNAP's HDD."""

    def __init__(self):
        self._lock = threading.Lock()
        self._items: dict[str, RemoteHls] = {}
        self._slow_samples = 0
        self._cooldown_until = 0.0
        self._last_segment_rate = None

    def eligible(self) -> bool:
        with self._lock:
            return time.monotonic() >= self._cooldown_until

    def add(self, worker_key: str, media_id: str, source_path: str,
            profiles: list[str], height: int, start: float, owner: str) -> RemoteHls:
        if not _KEY.fullmatch(worker_key):
            raise WorkerOffloadError("invalid worker cache identity")
        now = time.monotonic()
        with self._lock:
            self._items = {k: v for k, v in self._items.items() if now-v.seen < 3600}
            while len(self._items) >= 64:
                self._items.pop(min(self._items, key=lambda k: self._items[k].seen))
            key = secrets.token_hex(32)
            item = RemoteHls(key, worker_key, media_id, source_path,
                             tuple(profiles), height, start, owner, now)
            self._items[key] = item
            return item

    def get(self, media_id: str, key: str) -> RemoteHls | None:
        with self._lock:
            item = self._items.get(key)
            if item is None or item.media_id != media_id:
                return None
            now = time.monotonic()
            if now-item.seen > 3600:
                self._items.pop(key, None)
                return None
            item.seen = now
            return item

    def record_segment(self, bytes_count: int, elapsed: float, segment_seconds: float) -> None:
        """Compare effective encoded-segment arrival speed with playback demand.

        Includes worker encoding + network + A proxy, rather than CPU model
        guesses. Three slow segments trigger 60s cooldown; successes reset it.
        """
        if bytes_count < 1 or elapsed <= 0 or segment_seconds <= 0:
            return
        effective_rate = segment_seconds / elapsed
        with self._lock:
            self._last_segment_rate = round(effective_rate, 2)
            if effective_rate < 0.85:
                self._slow_samples += 1
                if self._slow_samples >= 3:
                    self._cooldown_until = time.monotonic() + 60
                    self._slow_samples = 0
                    logger.info("Linux HLS was slower than playback demand; cooldown 60s")
            else:
                self._slow_samples = 0

    def status(self) -> dict:
        with self._lock:
            return {
                "hls_segment_playback_ratio": self._last_segment_rate,
                "hls_cooldown_seconds": max(0, round(self._cooldown_until-time.monotonic())),
                "active_hls_keys": len(self._items),
            }

    def remove_owner(self, media_id: str, owner: str) -> None:
        with self._lock:
            for key, item in list(self._items.items()):
                if item.media_id == media_id and item.owner == owner:
                    self._items.pop(key, None)
