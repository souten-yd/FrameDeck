"""Automatic parent video access, authentication and external quality safety."""
import base64
import json
import time
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

from framedeck.cluster_worker import signed_headers, response_signature
from framedeck.worker_offload import WorkerOffloadError, root_fingerprint
from framedeck.worker_sources import WorkerSources, make_ticket, read_ticket, library_source
from framedeck.web.app import create_app
from framedeck.core.library_service import item_id_for

SECRET = "ad" * 32
NAS = "192.168.10.2"
LINUX = "192.168.10.3"


def client_at(services, ip):
    app = create_app(services)

    @app.middleware("http")
    async def real_peer(request, call_next):
        request.scope["client"] = (ip, 43210)
        return await call_next(request)

    return TestClient(app, base_url=f"http://{NAS}:9000")


def announce(sources, roots, *, secret=SECRET, peer=NAS):
    data = {"name": "my-nas", "version": "2.4.26", "port": 9000,
            "roots": [{"id": root_fingerprint(r), "name": "Movies"} for r in roots]}
    raw = json.dumps(data).encode()
    sources.accept_announcement(base64.urlsafe_b64encode(raw).decode(),
                                response_signature(secret, "nonce", raw),
                                "nonce", secret, peer)


@pytest.fixture()
def owner(app_env, monkeypatch, tmp_path):
    services, paths, _ = app_env
    root = tmp_path / "movies"
    root.mkdir()
    path = root / "sample.mp4"
    path.write_bytes(b"0123456789")
    services.library.add_root(str(root), "video")
    services.settings.set("cluster_offload_enabled", True)
    peer = {"url": f"http://{LINUX}:9000", "secret": SECRET}
    monkeypatch.setattr(services.cluster, "verified_peer", lambda: peer)
    stat = path.stat()
    data = {"root": str(root), "relative": path.name, "size": stat.st_size,
            "mtime": stat.st_mtime_ns, "expires": int(time.time()) + 300}
    return services, paths, root, path, data


def test_source_range_head_and_current_library_boundary(owner, monkeypatch):
    services, _, root, path, data = owner
    ticket = make_ticket(SECRET, data)
    client = client_at(services, LINUX)
    url = "/api/cluster/source?" + urlencode({"ticket": ticket})
    assert client.get(url).content == b"0123456789"
    part = client.get(url, headers={"range": "bytes=3-6"})
    assert part.status_code == 206 and part.content == b"3456"
    assert part.headers["content-range"] == "bytes 3-6/10"
    head = client.head(url)
    assert head.status_code == 200 and head.content == b""
    assert head.headers["content-length"] == "10"
    assert client.get(url, headers={"range": "bytes=100-"}).status_code == 416
    assert client.get(url.replace(ticket, ticket[:-1] + ("0" if ticket[-1] != "0" else "1"))).status_code == 403
    expired = make_ticket(SECRET, {**data, "expires": int(time.time()) - 1})
    assert client.get("/api/cluster/source", params={"ticket": expired}).status_code == 403
    path.write_bytes(b"changed")
    assert client.get(url).status_code == 403
    services.settings.set("cluster_offload_enabled", False)
    assert client.get(url).status_code == 403
    services.settings.set("cluster_offload_enabled", True)
    monkeypatch.setattr(services.cluster, "verified_peer", lambda: None)
    assert client.get(url).status_code == 403


def test_source_ticket_cannot_be_used_by_other_lan_browser(owner):
    services, _, _, _, data = owner
    client = client_at(services, "192.168.10.99")
    assert client.get("/api/cluster/source", params={"ticket": make_ticket(SECRET, data)}).status_code == 403


def test_library_source_rejects_traversal_symlink_nonvideo_and_unshared(owner, tmp_path):
    services, _, root, _, data = owner
    roots = services.library.list_roots()
    for relative in ["../secret.mp4", "/sample.mp4", "a//sample.mp4", "a/./sample.mp4", "sample.mp4\x00"]:
        with pytest.raises(WorkerOffloadError):
            library_source(roots, str(root), relative)
    outside = tmp_path / "private.mp4"
    outside.write_bytes(b"private")
    (root / "escape.mp4").symlink_to(outside)
    (root / "note.txt").write_text("private")
    for relative in ["escape.mp4", "note.txt"]:
        with pytest.raises(WorkerOffloadError):
            library_source(roots, str(root), relative)
    with pytest.raises(WorkerOffloadError):
        library_source([], str(root), "sample.mp4")


def test_signed_heartbeat_records_parent_without_mount_and_survives_restart(app_env, monkeypatch):
    services, paths, _ = app_env
    monkeypatch.setenv("FRAMEDECK_CLUSTER_WORKER_SECRET", SECRET)
    client = client_at(services, NAS)
    resource = "/api/cluster/worker/capabilities"
    headers = signed_headers(SECRET, "GET", resource)
    announcement = {"name": "nas", "version": "2.4.26", "port": 9001,
                    "roots": [{"id": root_fingerprint("/share/Movies"), "name": "Movies"}]}
    raw = json.dumps(announcement).encode()
    headers["X-FrameDeck-Parent"] = base64.urlsafe_b64encode(raw).decode()
    headers["X-FrameDeck-Parent-Proof"] = response_signature(SECRET, headers["X-FrameDeck-Nonce"], raw)
    response = client.get(resource, headers=headers)
    assert response.status_code == 200
    assert response.json()["source_roots"] == [root_fingerprint("/share/Movies")]
    assert response.json()["automatic_sources"]
    parent = client.get("/api/cluster/status").json()["parent"]
    assert parent["online"] and parent["url"] == f"http://{NAS}:9001"
    reloaded = WorkerSources(services).status()
    assert reloaded["configured"] and not reloaded["online"]
    assert reloaded["name"] == "nas"
    headers.update(signed_headers(SECRET, "GET", resource))
    headers["X-FrameDeck-Parent-Proof"] = "0" * 64
    assert client.get(resource, headers=headers).status_code == 403
    services.worker_sources.clear()
    assert not services.worker_sources.status()["configured"]


def test_worker_only_reads_authenticated_parent_and_stable_cache_identity(owner, monkeypatch):
    services, _, root, _, data = owner
    monkeypatch.setenv("FRAMEDECK_CLUSTER_WORKER_SECRET", SECRET)
    announce(services.worker_sources, [str(root)])
    def source_url(payload):
        return f"http://{NAS}:9000/api/cluster/source?" + urlencode({"ticket": make_ticket(SECRET, payload)})
    first = source_url(data)
    source = services.worker_sources.resolve(str(root), "sample.mp4", first, SECRET)
    key = services.hls.cache_key(source, ["1080p"], 1080)
    second = source_url({**data, "expires": data["expires"] + 10})
    services.worker_sources.resolve(str(root), "sample.mp4", second, SECRET)
    assert services.hls.cache_key(second, ["1080p"], 1080) == key
    for url in [first.replace(NAS, "8.8.8.8"), first.replace('/source?', '/worker/hls/start?')]:
        with pytest.raises(WorkerOffloadError):
            services.worker_sources.resolve(str(root), "sample.mp4", url, SECRET)
    with pytest.raises(WorkerOffloadError):
        services.worker_sources.resolve(str(root), "other.mp4", first, SECRET)
    services.worker_sources._seen = time.time() - 40
    with pytest.raises(WorkerOffloadError):
        services.worker_sources.resolve(str(root), "sample.mp4", first, SECRET)


def test_qnap_external1080_profile_and_no_local_fallback(owner, monkeypatch):
    import framedeck.web.routers.video as video
    services, _, _, path, _ = owner
    media = item_id_for(str(path))
    services.storage.upsert_media_item(media, str(path), "video", None, path.stat().st_mtime, path.stat().st_size)
    info = SimpleNamespace(height=1080, width=1920, video_codec="h264", audio_codec="aac", container="mp4", direct_play=True, direct_play_reason="")
    monkeypatch.setattr(services.video_playback, "get_info", lambda *a: info)
    monkeypatch.setattr(video, "qnap_lite", lambda: True)
    monkeypatch.setattr(video, "_worker_ready", lambda *a, **kw: True)
    client = client_at(services, NAS)
    decision = client.post(f"/api/videos/{media}/playback-profile", json={"requestedProfile": "1080p-ext"})
    assert decision.status_code == 200
    assert decision.json()["worker_required"]
    assert decision.json()["profile"]["height"] == 1080
    observed = []
    monkeypatch.setattr(video, "hls_eligible", lambda *a: True)
    monkeypatch.setattr(video, "start_remote_hls", lambda *a: observed.append(a[3]) or "a" * 64)
    response = client.get(f"/api/videos/{media}/hls/master.m3u8?profile=1080p&worker_only=1", follow_redirects=False)
    assert response.status_code == 302 and observed == [["1080p"]]
    services.cluster.mark_unhealthy()
    monkeypatch.setattr(services.cluster, "verified_peer", lambda: None)
    monkeypatch.setattr(video, "_worker_ready", lambda *a, **kw: False)
    def unsafe(*a, **kw):
        pytest.fail("external1080 must not encode on the QNAP")
    monkeypatch.setattr(services.hls, "ensure_async", unsafe)
    monkeypatch.setattr(services.transcode, "open_fmp4", unsafe)
    assert client.get(response.headers["location"]).status_code == 503
    assert client.get(f"/api/videos/{media}/hls/master.m3u8?profile=1080p&worker_only=1").status_code == 503
    assert client.get(f"/api/videos/{media}/stream-transcode?worker_only=1").status_code == 503
    assert client.post(f"/api/videos/{media}/playback-profile", json={"requestedProfile": "1080p-ext"}).status_code == 409


def test_new_pairing_enables_conversion_without_extra_settings(app_env, monkeypatch):
    services, _, _ = app_env
    monkeypatch.setattr(services.pairing, "finish", lambda: {"paired": True})
    assert services.settings.get("cluster_offload_enabled") is False
    response = client_at(services, NAS).post("/api/cluster/pairing/finish")
    assert response.status_code == 200 and response.json()["paired"]
    assert services.settings.get("cluster_offload_enabled") is True
