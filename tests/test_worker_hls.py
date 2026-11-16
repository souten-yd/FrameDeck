"""Authenticated Linux HLS/AAC delegation, failback and measurement tests."""
from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from framedeck.cluster_worker import response_signature, signed_headers
from framedeck.core.library_service import item_id_for
from framedeck.web.app import create_app
from framedeck.worker_hls import (
    HLS_ALLOWED, WORKER_HLS_FILE, WORKER_HLS_START, RemoteHlsSessions,
    hls_eligible, valid_hls_file,
)
from framedeck.worker_offload import root_fingerprint

SECRET = "21" * 32
B_URL = "http://192.168.1.42:9000"


def test_hls_filename_policy_blocks_traversal_and_unrelated_sources():
    assert valid_hls_file("master.m3u8")
    assert valid_hls_file("original-aac/init.mp4")
    assert valid_hls_file("720p/segment_00005.m4s")
    assert valid_hls_file("original-aac/playlist.m3u8")
    for bad in [
        "../settings.json", "/etc/passwd", "", ".env", "720p/../../x",
        "720p/segment_00001.m4s/other", "720p/file.txt",
        "original/playlist.m3u8", "1080p/segment_01.m4s",
    ]:
        assert not valid_hls_file(bad)


def test_hls_new_job_requires_capability_and_opt_in(app_env, monkeypatch):
    services, _, _ = app_env
    services.cluster.configure(B_URL, SECRET)
    with services.cluster._lock:
        services.cluster._last_seen = time.time()
        services.cluster._capabilities = {
            "remote_fmp4": True,
            "remote_hls": True,
            "transcode_available": True,
            "source_roots": [root_fingerprint("/share/Download/Video")],
        }
    assert not hls_eligible(services.cluster, "/share/Download/Video", services.settings)
    services.settings.set("cluster_offload_enabled", True)
    assert hls_eligible(services.cluster, "/share/Download/Video", services.settings)
    services.cluster._capabilities["remote_hls"] = False
    assert not hls_eligible(services.cluster, "/share/Download/Video", services.settings)


def test_worker_hls_signed_endpoints_reject_unsigned_and_replay(
        app_env, tmp_path, monkeypatch):
    import framedeck.worker_offload as offload
    import framedeck.web.routers.cluster as cluster_router

    services, _, _ = app_env
    root = tmp_path / "ro-mount"
    root.mkdir()
    video = root / "movie.mp4"
    video.write_bytes(b"fake-video")
    monkeypatch.setenv(
        "FRAMEDECK_CLUSTER_WORKER_MOUNTS_JSON",
        json.dumps({"/share/Video": str(root)}),
    )
    monkeypatch.setenv("FRAMEDECK_CLUSTER_WORKER_SECRET", SECRET)
    monkeypatch.setattr(offload, "_is_read_only_mount", lambda _: True)
    monkeypatch.setattr(
        cluster_router, "resolve_ffmpeg",
        lambda _: SimpleNamespace(available=True),
    )
    key = "a" * 64
    def generate(source, profiles, source_height, start_seconds, owner):
        assert source == str(video)
        assert profiles == ["original-aac"]
        return SimpleNamespace(key=key)
    monkeypatch.setattr(services.hls, "ensure_async", generate)
    cache = services.hls.cache_root / key
    (cache / "original-aac").mkdir(parents=True, exist_ok=True)
    (cache / "master.m3u8").write_text(
        "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=20000000\n"
        "original-aac/playlist.m3u8\n",
    )
    (cache / "original-aac" / "playlist.m3u8").write_text(
        "#EXTM3U\n#EXTINF:2.0,\nsegment_00000.m4s\n",
    )
    (cache / "original-aac" / "segment_00000.m4s").write_bytes(b"encoded-aac")
    client = TestClient(create_app(services))
    query = (
        "source_root=%2Fshare%2FVideo&relative_path=movie.mp4&"
        "profiles=original-aac&height=1080&start=5.0&session=tab"
    )
    target = WORKER_HLS_START + "?" + query
    assert client.get(target).status_code == 403
    headers = signed_headers(SECRET, "GET", target)
    result = client.get(target, headers=headers)
    assert result.status_code == 200
    assert result.json()["key"] == key
    assert result.headers["x-framedeck-response-signature"] == response_signature(
        SECRET, headers["X-FrameDeck-Nonce"], result.content,
    )
    assert client.get(target, headers=headers).status_code == 403

    path = WORKER_HLS_FILE + "?key=" + key + "&relative=original-aac%2Fsegment_00000.m4s"
    request_headers = signed_headers(SECRET, "GET", path)
    segment = client.get(path, headers=request_headers)
    assert segment.status_code == 200
    assert segment.content == b"encoded-aac"
    assert segment.headers["x-framedeck-stream-proof"] == response_signature(
        SECRET, request_headers["X-FrameDeck-Nonce"], b"",
    )


def test_opaque_hls_key_is_media_scoped_and_expires(monkeypatch):
    sessions = RemoteHlsSessions()
    item = sessions.add("a" * 64, "media-1", "/tmp/video.mp4",
                        ["original-aac"], 1080, 3.0, "tab")
    assert len(item.proxy_key) == 64
    assert item.proxy_key != item.worker_key
    assert sessions.get("other-media", item.proxy_key) is None
    assert sessions.get("media-1", item.proxy_key) is item
    sessions.remove_owner("media-1", "other-tab")
    assert sessions.get("media-1", item.proxy_key) is item
    sessions.remove_owner("media-1", "tab")
    assert sessions.get("media-1", item.proxy_key) is None


def test_measured_segment_delivery_hysteresis():
    sessions = RemoteHlsSessions()
    assert sessions.eligible()
    for _ in range(2):
        sessions.record_segment(100000, 4.0, 2.0)
        assert sessions.eligible()
    sessions.record_segment(100000, 4.0, 2.0)
    assert not sessions.eligible()
    report = sessions.status()
    assert report["hls_cooldown_seconds"] > 0
    assert report["hls_segment_playback_ratio"] == .5


def test_two_app_aaac_hls_proxy_and_nas_failback(app_env, tmp_path, monkeypatch):
    """A's public URL stays on QNAP, while B serves only signed HLS files."""
    from framedeck.config import Settings, ensure_runtime_directories, resolve_app_paths
    from framedeck.core.services import build_services
    from framedeck.core.storage import Storage
    from framedeck import worker_hls, worker_offload
    import framedeck.web.routers.cluster as cluster_router

    a, _, _ = app_env
    root = tmp_path / "nas-share"
    root.mkdir()
    source = root / "movie.mp4"
    source.write_bytes(b"fixture-video")
    a.library.add_root(str(root), "video")
    media_id = item_id_for(str(source))
    a.storage.upsert_media_item(
        media_id, str(source), "video", None,
        source.stat().st_mtime, source.stat().st_size,
    )
    b_paths = resolve_app_paths(tmp_path / "B_FrameDeck_venv")
    ensure_runtime_directories(b_paths)
    b = build_services(Settings(b_paths), Storage(b_paths.database_file), b_paths)
    monkeypatch.setenv("FRAMEDECK_CLUSTER_WORKER_SECRET", SECRET)
    monkeypatch.setenv("FRAMEDECK_CLUSTER_WORKER_MOUNTS_JSON",
                       json.dumps({str(root): str(root)}))
    monkeypatch.setattr(worker_offload, "_is_read_only_mount", lambda _: True)
    monkeypatch.setattr(cluster_router, "resolve_ffmpeg",
                        lambda _: SimpleNamespace(available=True))
    key = "9" * 64
    b_hls = b.hls.cache_root / key
    (b_hls / "original-aac").mkdir(parents=True, exist_ok=True)
    (b_hls / "master.m3u8").write_text(
        "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=20000000\noriginal-aac/playlist.m3u8\n",
    )
    (b_hls / "original-aac" / "playlist.m3u8").write_text(
        "#EXTM3U\n#EXTINF:2.0,\nsegment_00000.m4s\n",
    )
    (b_hls / "original-aac" / "segment_00000.m4s").write_bytes(b"linux-aac-data")
    monkeypatch.setattr(b.hls, "ensure_async", lambda *args, **kw: SimpleNamespace(key=key))
    b_client = TestClient(create_app(b))

    class Reply:
        def __init__(self, result):
            self.status = result.status_code
            self.headers = result.headers
            self._bytes = result.content
            self._offset = 0
        def read(self, count):
            result = self._bytes[self._offset:self._offset+count]
            self._offset += len(result)
            return result
        read1 = read
        def close(self):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.close()
    class Transport:
        def open(self, request, timeout):
            parsed = urlsplit(request.full_url)
            assert request.full_url.startswith(B_URL + "/api/cluster/worker/")
            uri = parsed.path + ("?" + parsed.query if parsed.query else "")
            return Reply(b_client.get(uri, headers=dict(request.header_items())))
    monkeypatch.setattr(worker_hls, "_secure_opener", lambda: Transport())

    a.cluster.configure(B_URL, SECRET)
    with a.cluster._lock:
        a.cluster._last_seen = time.time()
        a.cluster._capabilities = {
            "remote_fmp4": True, "remote_hls": True,
            "transcode_available": True,
            "source_roots": [root_fingerprint(str(root))],
        }
    a.settings.set("cluster_offload_enabled", True)
    monkeypatch.setattr(a.video_playback, "get_info",
                        lambda *args: SimpleNamespace(height=1080))
    a_client = TestClient(create_app(a))
    master = a_client.get(
        f"/api/videos/{media_id}/hls/master.m3u8?profile=original-aac&start=3&session=tab",
        follow_redirects=False,
    )
    assert master.status_code == 302
    assert master.headers["x-framedeck-processing-node"] == "linux-worker"
    opaque_url = master.headers["location"]
    assert opaque_url.startswith(f"/api/videos/{media_id}/hls/")
    assert key not in opaque_url
    playlist = a_client.get(opaque_url)
    assert playlist.status_code == 200
    assert b"original-aac/playlist.m3u8" in playlist.content
    segment_uri = opaque_url.replace("/master.m3u8", "/original-aac/segment_00000.m4s")
    assert a_client.get(segment_uri).content == b"linux-aac-data"

    # Power off worker -> QNAP starts its *local* HLS with same profile and
    # start time; the old browser-facing URL continues functioning.
    a.cluster.mark_unhealthy()
    fallback_key = "8" * 64
    fallback = a.hls.cache_root / fallback_key
    (fallback / "original-aac").mkdir(parents=True, exist_ok=True)
    (fallback / "original-aac" / "segment_00000.m4s").write_bytes(b"nas-aac-data")
    a_hls_calls = []
    def local_gen(path, profiles, source_height, start_seconds, owner):
        a_hls_calls.append((path, profiles, start_seconds))
        return SimpleNamespace(key=fallback_key)
    monkeypatch.setattr(a.hls, "ensure_async", local_gen)
    assert a_client.get(segment_uri).content == b"nas-aac-data"
    assert a_hls_calls[-1] == (str(source), ["original-aac"], 3.0)
    b.shutdown()
