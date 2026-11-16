"""Stage 2: mounted-source fMP4 offload and safe QNAP fallback."""
from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

from framedeck.cluster_worker import (
    response_signature, signed_headers,
)
from framedeck.core.library_service import item_id_for
from framedeck.web.app import create_app
from framedeck.worker_offload import (
    WORKER_FMP4_PATH, WorkerOffloadError, offload_enabled,
    open_remote_fmp4, owner_source_ref, remote_eligible,
    resolve_mounted_video, root_fingerprint, worker_root_fingerprints,
)


SECRET = "13" * 32
A_ROOT = "/share/Download/Video"
B_URL = "http://192.168.0.30:9000"


@pytest.fixture()
def mounted_video(monkeypatch, tmp_path):
    import framedeck.worker_offload as offload
    # Simulate an OS-mounted ro share in the filesystem fixture.
    monkeypatch.setattr(offload, "_is_read_only_mount", lambda _: True)
    root = tmp_path / "nas-share"
    root.mkdir()
    video = root / "episode.mp4"
    video.write_bytes(b"not a real mp4 - test fixture")
    monkeypatch.setenv(
        "FRAMEDECK_CLUSTER_WORKER_MOUNTS_JSON",
        json.dumps({A_ROOT: str(root)}),
    )
    return root, video


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("FRAMEDECK_CLUSTER_ENABLE_OFFLOAD", raising=False)
    assert not offload_enabled()


def test_worker_refuses_writable_mount(mounted_video, monkeypatch):
    import framedeck.worker_offload as offload
    monkeypatch.setattr(offload, "_is_read_only_mount", lambda _: False)
    assert worker_root_fingerprints() == []
    with pytest.raises(WorkerOffloadError):
        resolve_mounted_video(A_ROOT, "episode.mp4")


def test_mounted_source_allowlist_only(mounted_video):
    root, video = mounted_video
    assert resolve_mounted_video(A_ROOT, "episode.mp4") == video
    assert worker_root_fingerprints() == [root_fingerprint(A_ROOT)]
    with pytest.raises(WorkerOffloadError):
        resolve_mounted_video("/share/Other", "episode.mp4")
    for unsafe in ["../secret.mp4", "/episode.mp4", "a/../../secret.mp4",
                   "x//episode.mp4", "x/./episode.mp4", "x\\episode.mp4",
                   "episode.mp4\x00", "episode.mp4\n", ""]:
        with pytest.raises(WorkerOffloadError):
            resolve_mounted_video(A_ROOT, unsafe)


def test_symlink_escape_rejected(mounted_video, tmp_path):
    root, _ = mounted_video
    outside = tmp_path / "private.mp4"
    outside.write_bytes(b"secret")
    (root / "escaped.mp4").symlink_to(outside)
    with pytest.raises(WorkerOffloadError):
        resolve_mounted_video(A_ROOT, "escaped.mp4")


def test_source_map_invalid_or_unmapped_rejected(monkeypatch, mounted_video):
    root, _ = mounted_video
    monkeypatch.setenv(
        "FRAMEDECK_CLUSTER_WORKER_MOUNTS_JSON", '{"bad": "/nonexistent"}',
    )
    assert worker_root_fingerprints() == []
    with pytest.raises(WorkerOffloadError):
        resolve_mounted_video(A_ROOT, "episode.mp4")
    monkeypatch.setenv(
        "FRAMEDECK_CLUSTER_WORKER_MOUNTS_JSON", '{"a":',
    )
    assert worker_root_fingerprints() == []


def test_original_id_stays_on_a(mounted_video):
    _, video = mounted_video
    root, rel = owner_source_ref(str(video), [{"path": str(video.parent)}])
    assert root == str(video.parent)
    assert rel == "episode.mp4"
    assert len(root_fingerprint(root)) == 64


def test_qnap_symlink_library_root_mapping_stays_stable(tmp_path):
    from framedeck.worker_offload import owner_source_ref, root_fingerprint
    real_root = tmp_path / "CACHEDEV1_DATA" / "Download" / "Video"
    real_root.mkdir(parents=True)
    source = real_root / "episode.mp4"
    source.write_bytes(b"fake-video")
    qnap_alias = tmp_path / "share"
    qnap_alias.symlink_to(real_root, target_is_directory=True)
    # A's configured root can be a QNAP-only symlink. Keep that original
    # spelling when matching B's explicit source mapping.
    root, relative = owner_source_ref(
        str(source), [{"path": str(qnap_alias), "kind": "video"}],
    )
    assert root == str(qnap_alias)
    assert relative == "episode.mp4"
    assert root_fingerprint(root) == root_fingerprint(str(qnap_alias))


def test_eligibility_requires_opt_in_fresh_health_and_matching_mount(app_env, monkeypatch):
    services, _, _ = app_env
    controller = services.cluster
    controller.configure(B_URL, SECRET)
    monkeypatch.setenv("FRAMEDECK_CLUSTER_ENABLE_OFFLOAD", "1")
    assert not remote_eligible(controller, A_ROOT)
    with controller._lock:
        controller._last_seen = time.time()
        controller._capabilities = {
            "remote_fmp4": True, "transcode_available": True,
            "source_roots": [root_fingerprint(A_ROOT)],
        }
    assert remote_eligible(controller, A_ROOT)
    assert not remote_eligible(controller, "/share/Other")
    controller.mark_unhealthy()
    assert not remote_eligible(controller, A_ROOT)


def test_remote_request_checks_worker_response_proof(app_env, monkeypatch):
    from framedeck import worker_offload
    services, _, _ = app_env
    controller = services.cluster
    controller.configure(B_URL, SECRET)
    monkeypatch.setenv("FRAMEDECK_CLUSTER_ENABLE_OFFLOAD", "true")
    with controller._lock:
        controller._last_seen = time.time()
        controller._capabilities = {
            "remote_fmp4": True, "transcode_available": True,
            "source_roots": [root_fingerprint(A_ROOT)],
        }

    class Reply:
        status = 200
        def __init__(self, proof):
            self.headers = {"X-FrameDeck-Stream-Proof": proof}
            self.closed = False
        def close(self):
            self.closed = True
        def read(self, n):
            return b"MP4"

    class Opener:
        valid = True
        def open(self, req, timeout):
            assert timeout == 10
            assert req.full_url.startswith(B_URL + WORKER_FMP4_PATH + "?")
            headers = {k.lower(): v for k, v in req.header_items()}
            assert headers["x-framedeck-signature"]
            nonce = headers["x-framedeck-nonce"]
            return Reply(response_signature(SECRET, nonce, b"")
                         if self.valid else "0" * 64)

    opener = Opener()
    monkeypatch.setattr(worker_offload, "_secure_opener", lambda: opener)
    remote = open_remote_fmp4(
        controller, A_ROOT, "episode.mp4",
        max_height=1080, session="session-1",
    )
    assert remote.read(4) == b"MP4"
    remote.close()

    opener.valid = False
    with pytest.raises(WorkerOffloadError):
        open_remote_fmp4(controller, A_ROOT, "episode.mp4")
    assert controller.verified_peer() is None


def test_worker_fmp4_requires_signed_request_and_safe_source(app_env, mounted_video, monkeypatch):
    import framedeck.web.routers.cluster as cluster_router
    services, _, _ = app_env
    monkeypatch.setenv("FRAMEDECK_CLUSTER_WORKER_SECRET", SECRET)
    monkeypatch.setattr(
        cluster_router, "resolve_ffmpeg",
        lambda auto_download: SimpleNamespace(available=True),
    )
    closed = []

    class FakeStream:
        def __init__(self):
            self.calls = 0
        def read(self, size=131072):
            self.calls += 1
            return b"ENCODED" if self.calls == 1 else b""
        def close(self):
            closed.append(True)

    invoked = []
    def fake_fmp4(path, start, **kwargs):
        invoked.append((path, start, kwargs))
        return FakeStream()
    monkeypatch.setattr(services.transcode, "open_fmp4", fake_fmp4)
    client = TestClient(create_app(services))
    query = urlencode({
        "source_root": A_ROOT,
        "relative_path": "episode.mp4",
        "start": "12.5",
        "session": "test-1",
    })
    resource = WORKER_FMP4_PATH + "?" + query
    assert client.get(resource).status_code == 403
    headers = signed_headers(SECRET, "GET", resource)
    response = client.get(resource, headers=headers)
    assert response.status_code == 200
    assert response.content == b"ENCODED"
    assert response.headers["x-framedeck-stream-proof"] == response_signature(
        SECRET, headers["X-FrameDeck-Nonce"], b"",
    )
    assert invoked[0][0].endswith("episode.mp4")
    assert invoked[0][1] == 12.5
    assert closed
    # Identical request cannot be replayed to run a second ffmpeg.
    assert client.get(resource, headers=headers).status_code == 403
    assert len(invoked) == 1


def test_worker_fmp4_rejects_mount_escape(app_env, mounted_video, monkeypatch):
    import framedeck.web.routers.cluster as cluster_router
    services, _, _ = app_env
    monkeypatch.setenv("FRAMEDECK_CLUSTER_WORKER_SECRET", SECRET)
    monkeypatch.setattr(
        cluster_router, "resolve_ffmpeg",
        lambda auto_download: SimpleNamespace(available=True),
    )
    client = TestClient(create_app(services))
    query = urlencode({
        "source_root": A_ROOT,
        "relative_path": "../outside.mp4",
    })
    resource = WORKER_FMP4_PATH + "?" + query
    response = client.get(
        resource, headers=signed_headers(SECRET, "GET", resource),
    )
    assert response.status_code == 503


def test_video_route_falls_back_to_local_when_worker_connect_fails(app_env, monkeypatch):
    import framedeck.web.routers.video as video_router
    services, _, tmp_path = app_env
    source = tmp_path / "media"
    source.mkdir()
    file = source / "test.mp4"
    file.write_bytes(b"fake test media")
    services.library.add_root(str(source), "video")
    media_id = item_id_for(str(file))
    services.storage.upsert_media_item(
        media_id, str(file), "video", None, file.stat().st_mtime, file.stat().st_size,
    )

    monkeypatch.setattr(video_router, "offload_enabled", lambda *_: True)
    monkeypatch.setattr(video_router, "remote_eligible", lambda *args: True)
    remote_attempts = []
    def fail_connect(*args, **kwargs):
        remote_attempts.append(True)
        raise WorkerOffloadError("network disconnected")
    monkeypatch.setattr(video_router, "open_remote_fmp4", fail_connect)
    closed = []
    class LocalStream:
        def __init__(self):
            self.calls = 0
        def read(self, size=131072):
            self.calls += 1
            return b"LOCAL" if self.calls == 1 else b""
        def close(self):
            closed.append(True)
    monkeypatch.setattr(
        services.transcode, "open_fmp4",
        lambda *a, **kw: LocalStream(),
    )
    response = TestClient(create_app(services)).get(
        f"/api/videos/{media_id}/stream-transcode?start=12",
    )
    assert response.status_code == 200
    assert response.content == b"LOCAL"
    assert remote_attempts == [True]
    forced = TestClient(create_app(services)).get(
        f"/api/videos/{media_id}/stream-transcode?start=12&force_local=1",
    )
    assert forced.status_code == 200
    assert forced.content == b"LOCAL"
    assert remote_attempts == [True]  # bypass B, even if health still looks good
    assert closed


def test_midstream_worker_disconnect_marks_unhealthy(app_env):
    import asyncio
    import framedeck.web.routers.video as video_router
    services, _, _ = app_env
    services.cluster.configure(B_URL, SECRET)
    with services.cluster._lock:
        services.cluster._last_seen = time.time()
        services.cluster._capabilities = {
            "remote_fmp4": True, "transcode_available": True,
            "source_roots": [root_fingerprint(A_ROOT)],
        }
    assert services.cluster.verified_peer() is not None
    class BrokenResponse:
        closed = False
        def read(self, n):
            raise ConnectionResetError("worker lost power")
        def close(self):
            self.closed = True
    response = BrokenResponse()
    async def consume():
        chunks = []
        async for chunk in video_router._aiter_worker_fmp4(
            response, services.cluster, b"fmp4-init",
        ):
            chunks.append(chunk)
        return chunks
    assert asyncio.run(consume()) == [b"fmp4-init"]
    assert response.closed
    assert services.cluster.verified_peer() is None
