"""Cluster worker opt-in pairing, authentication, and failure handling."""
import json
import os
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from framedeck.cluster_worker import (
    CAPABILITY_PATH,
    ClusterConfigError,
    ClusterController,
    ReplayGuard,
    response_signature,
    signed_headers,
    validate_worker_url,
)
from framedeck.web.app import create_app
from framedeck.web.routers.cluster import _require_pairing_permission


SECRET = "a1" * 32
LAN_URL = "http://192.168.10.20:9000"


def test_cluster_disabled_and_worker_endpoint_closed_by_default(app_env, monkeypatch):
    services, paths, _ = app_env
    monkeypatch.delenv("FRAMEDECK_CLUSTER_WORKER_SECRET", raising=False)
    client = TestClient(create_app(services))
    data = client.get("/api/cluster/status").json()
    assert data["peer"]["configured"] is False
    assert data["peer"]["online"] is False
    assert data["peer"]["offload_active"] is False
    assert not (paths.config_dir / "cluster-worker.json").exists()
    assert client.get(CAPABILITY_PATH).status_code == 403


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:9000",
    "http://169.254.169.254:80",
    "http://8.8.8.8:9000",
    "http://evil.example:9000",
    "file:///etc/passwd",
    "http://192.168.1.2:9000/a",
    "http://192.168.1.2:9000/?token=foo",
    "http://user:password@192.168.1.2:9000",
    "http://192.168.1.2:9000#fragment",
    "http://192.168.1.2",
])
def test_rejects_unsafe_urls(url):
    with pytest.raises(ClusterConfigError):
        validate_worker_url(url)


def test_accepts_explicit_private_lan_ips():
    assert validate_worker_url(LAN_URL) == LAN_URL
    assert validate_worker_url("http://100.100.100.100:9000") == "http://100.100.100.100:9000"


def test_config_file_atomic_private_and_status_no_token(app_env):
    services, paths, _ = app_env
    client = TestClient(create_app(services))
    result = client.put("/api/cluster/peer", json={
        "url": LAN_URL, "secret": SECRET,
    })
    assert result.status_code == 200
    assert result.json()["configured"] is True
    assert "secret" not in json.dumps(result.json())
    assert "secret" not in json.dumps(client.get("/api/cluster/status").json())
    assert SECRET not in json.dumps(client.get("/api/cluster/status").json())
    assert SECRET not in json.dumps(client.get("/api/settings").json())
    saved = paths.config_dir / "cluster-worker.json"
    assert saved.is_file()
    assert os.stat(saved).st_mode & 0o077 == 0
    assert json.loads(saved.read_text())["secret"] == SECRET

    reloaded = ClusterController(paths.config_dir)
    assert reloaded.status()["configured"] is True
    assert reloaded.status()["online"] is False
    assert client.delete("/api/cluster/peer").status_code == 200
    assert not saved.exists()
    assert services.cluster.status()["configured"] is False


def test_remote_pairing_requires_pin_and_same_origin(app_env):
    services, _, _ = app_env
    scope = {
        "type": "http", "http_version": "1.1", "method": "PUT",
        "path": "/api/cluster/peer", "query_string": b"",
        "headers": [(b"host", b"192.168.1.10:9000")],
        "client": ("192.168.1.12", 50000),
        "server": ("192.168.1.10", 9000),
        "scheme": "http",
    }
    with pytest.raises(HTTPException) as exc:
        _require_pairing_permission(Request(scope), services)
    assert exc.value.status_code == 403

    services.settings.set("web_pin", "test-only-pin")
    _require_pairing_permission(Request(scope), services)
    scope["headers"] = [
        (b"host", b"192.168.1.10:9000"),
        (b"origin", b"http://bad.example"),
    ]
    with pytest.raises(HTTPException) as exc:
        _require_pairing_permission(Request(scope), services)
    assert exc.value.status_code == 403


def test_hmac_and_replay_protection():
    guard = ReplayGuard()
    headers = signed_headers(SECRET, "GET", CAPABILITY_PATH, now=1730000000,
                             nonce="1" * 32)
    normalized = {k.lower(): v for k, v in headers.items()}
    assert guard.verify(SECRET, "GET", CAPABILITY_PATH, normalized, now=1730000000)
    assert not guard.verify(SECRET, "GET", CAPABILITY_PATH, normalized, now=1730000000)
    assert not ReplayGuard().verify(SECRET, "GET", "/other", normalized, now=1730000000)
    assert not ReplayGuard().verify(SECRET, "GET", CAPABILITY_PATH, normalized, now=1730000100)


def test_worker_capabilities_signed_and_replay_rejected(app_env, monkeypatch):
    services, _, _ = app_env
    monkeypatch.setenv("FRAMEDECK_CLUSTER_WORKER_SECRET", SECRET)
    client = TestClient(create_app(services))
    assert client.get(CAPABILITY_PATH).status_code == 403
    services.settings.set("web_pin", "123456")
    headers = signed_headers(SECRET, "GET", CAPABILITY_PATH)
    result = client.get(CAPABILITY_PATH, headers=headers)
    assert result.status_code == 200
    assert result.json()["worker"] is True
    assert result.json()["protocol_version"] == 1
    assert isinstance(result.json()["transcode_available"], bool)
    assert result.headers["x-framedeck-response-signature"] == response_signature(
        SECRET, headers["X-FrameDeck-Nonce"], result.content,
    )
    assert client.get(CAPABILITY_PATH, headers=headers).status_code == 403


def test_probe_uses_signed_health_and_fails_closed(app_env, monkeypatch):
    import framedeck.cluster_worker as worker
    services, _, _ = app_env
    controller = services.cluster
    controller.configure(LAN_URL, SECRET)
    assert controller.status()["online"] is False
    calls = []

    class Reply:
        status = 200

        def __init__(self, payload, signature):
            self.payload = payload
            self.headers = {"X-FrameDeck-Response-Signature": signature}

        def read(self, size):
            return self.payload[:size]

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    class FakeOpener:
        def __init__(self, valid=True):
            self.valid = valid

        def open(self, request, timeout):
            assert timeout == 2.0
            assert request.full_url == LAN_URL + CAPABILITY_PATH
            signature = dict((k.lower(), v) for k, v in request.header_items())
            assert signature["x-framedeck-signature"]
            calls.append(request.full_url)
            payload = json.dumps({
                "protocol_version": 1,
                "worker": True, "version": "unit-test",
                "transcode_available": True,
            }).encode()
            mac = response_signature(
                SECRET, signature["x-framedeck-nonce"], payload,
            ) if self.valid else "0" * 64
            return Reply(payload, mac)

    monkeypatch.setattr(worker, "_secure_opener", lambda: FakeOpener())
    controller.probe_once()
    status = controller.status()
    assert status["online"] is True
    assert status["capabilities"]["transcode_available"] is True
    assert status["offload_active"] is False
    assert len(calls) == 1

    monkeypatch.setattr(worker, "_secure_opener", lambda: FakeOpener(valid=False))
    controller.probe_once()
    assert controller.status()["online"] is False
    assert controller.status()["capabilities"] is None


def test_insecure_or_corrupted_config_cannot_enable_worker(tmp_path):
    location = tmp_path / "config"
    location.mkdir()
    config = location / "cluster-worker.json"
    config.write_text(json.dumps({"url": LAN_URL, "secret": SECRET}))
    config.chmod(0o644)
    assert ClusterController(location).status()["configured"] is False
    config.chmod(0o600)
    config.write_text("{bad json")
    assert ClusterController(location).status()["configured"] is False


def test_peer_pairing_input_validation(app_env):
    services, _, _ = app_env
    client = TestClient(create_app(services))
    assert client.put("/api/cluster/peer", json={
        "url": LAN_URL, "secret": "short",
    }).status_code == 422
    assert client.put("/api/cluster/peer", json={
        "url": "http://example.com:9000", "secret": SECRET,
    }).status_code == 422
    assert services.cluster.status()["configured"] is False
