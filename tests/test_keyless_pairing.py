"""Keyless X25519 FrameDeck worker pairing tests.

The UI compares the same six-digit authentication string at both hosts;
the 256-bit HMAC secret is never transmitted or displayed.
"""
from __future__ import annotations

import json
import os
import time
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from framedeck import cluster_pairing
from framedeck.cluster_pairing import PairingError, PairingManager
from framedeck.cluster_worker import set_runtime_worker_secret, worker_secret
from framedeck.web.app import create_app


class FakeCluster:
    def __init__(self):
        self.configured = None
        self.probes = 0

    def status(self):
        return {"configured": self.configured is not None}

    def configure(self, url, secret):
        self.configured = (url, secret)

    def probe_once(self):
        self.probes += 1

    def _sync_config_dir(self):
        pass


class FakeDiscovery:
    def __init__(self):
        self.secret = None

    def install_secret(self, secret):
        self.secret = secret


class Result:
    def __init__(self, value, status=200):
        self.status = status
        self._content = json.dumps(value).encode("utf-8")
        self.pos = 0

    def read(self, count):
        part = self._content[self.pos:self.pos+count]
        self.pos += len(part)
        return part

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None


@pytest.fixture()
def pairing_pair(tmp_path, monkeypatch):
    monkeypatch.delenv("FRAMEDECK_CLUSTER_WORKER_SECRET", raising=False)
    set_runtime_worker_secret(None)
    if not cluster_pairing.X25519PrivateKey:
        pytest.skip("cryptography dependency not installed")
    linux_dir = tmp_path / "linux"
    qnap_dir = tmp_path / "qnap"
    worker_c = FakeCluster()
    nas_c = FakeCluster()
    worker = PairingManager(
        linux_dir, worker_c, FakeDiscovery(), linux=True,
    )
    nas_discovery = FakeDiscovery()
    nas = PairingManager(
        qnap_dir, nas_c, nas_discovery, linux=False,
    )

    class Transport:
        def __init__(self):
            self.missing_ack_once = False

        def open(self, req, timeout):
            assert timeout <= 3.0
            assert req.full_url.startswith("http://192.168.1.80:9000")
            path = urlsplit(req.full_url).path
            data = json.loads(req.data)
            if path == cluster_pairing.PAIRING_ANNOUNCE_PATH:
                return Result(worker.hello(
                    data["id"], data["nas_pub"], "192.168.1.20",
                ))
            if path == cluster_pairing.PAIRING_COMMIT_PATH:
                if self.missing_ack_once:
                    self.missing_ack_once = False
                    worker.commit(data["id"], data["proof"], "192.168.1.20")
                    raise OSError("ack lost over a power cycle")
                return Result(worker.commit(
                    data["id"], data["proof"], "192.168.1.20",
                ))
            raise AssertionError("Unexpected pairing request")

    transport = Transport()
    monkeypatch.setattr(cluster_pairing, "_secure_opener", lambda: transport)
    yield nas, worker, nas_c, nas_discovery, transport, linux_dir
    worker.shutdown()
    nas.shutdown()


def _open_and_join(nas, worker):
    assert worker.arm()["armed"] is True
    assert nas.arm()["armed"] is True
    packet = worker.offer_packet(9000)
    assert packet is not None
    assert b"secret" not in packet
    assert nas.accept_offer(packet, "192.168.1.80")
    pairing_id = json.loads(packet)["id"]
    assert nas.join(pairing_id)["code"] == worker.status()["confirmation_code"]
    return pairing_id


def test_ephemeral_key_exchange_requires_two_approvals_and_never_returns_secret(pairing_pair):
    nas, worker, nas_c, nas_discovery, _, linux_dir = pairing_pair
    pairing_id = _open_and_join(nas, worker)
    code = nas.status()["confirmation_code"]
    assert len(code) == 6 and code.isdecimal()
    assert code == worker.status()["confirmation_code"]
    assert "secret" not in json.dumps(worker.status())
    assert "secret" not in json.dumps(nas.status())
    assert not (linux_dir / "cluster-worker-secret.json").exists()

    with pytest.raises(PairingError):
        nas.finish()
    assert worker.approve_worker()["approved"]
    assert nas.finish()["paired"] is True
    assert nas_c.configured is not None
    assert nas_c.configured[1] == nas_discovery.secret
    assert worker_secret() == nas_discovery.secret
    assert len(nas_discovery.secret) == 64
    assert nas_c.probes == 1

    key_file = linux_dir / "cluster-worker-secret.json"
    assert key_file.is_file()
    assert key_file.stat().st_mode & 0o077 == 0
    assert key_file.read_text().count(nas_discovery.secret) == 1


def test_pairing_rejects_untrusted_offer_and_wrong_source(pairing_pair):
    nas, worker, *_ = pairing_pair
    worker.arm()
    nas.arm()
    offer = worker.offer_packet(9000)
    assert nas.accept_offer(offer, "8.8.8.8") is False
    assert nas.status()["candidates"] == []
    assert nas.accept_offer(offer, "127.0.0.1") is False
    assert nas.accept_offer(offer, "192.168.1.80") is True
    assert not worker_secret()
    with pytest.raises(PairingError):
        worker.hello("f" * 32, "a" * 64, "8.8.8.8")


def test_mitm_beacon_pub_mismatch_is_never_trusted(pairing_pair):
    nas, worker, nas_c, *_ = pairing_pair
    worker.arm()
    nas.arm()
    offer = json.loads(worker.offer_packet(9000))
    offer["pub"] = "01" * 32  # forged LAN announcement
    assert nas.accept_offer(json.dumps(offer).encode(), "192.168.1.80")
    with pytest.raises(PairingError, match="public key"):
        nas.join(offer["id"])
    assert nas_c.configured is None


def test_pairing_rejects_wrong_or_unapproved_mac(pairing_pair):
    nas, worker, nas_c, *_ = pairing_pair
    token = _open_and_join(nas, worker)
    with pytest.raises(PairingError):
        worker.commit(token, "00" * 32, "192.168.1.20")
    worker.approve_worker()
    with pytest.raises(PairingError):
        worker.commit(token, "00" * 32, "192.168.1.20")
    with pytest.raises(PairingError):
        worker.commit(token, "ff" * 32, "8.8.8.8")
    assert nas_c.configured is None


def test_retry_after_lost_worker_ack_is_safe(pairing_pair):
    nas, worker, nas_c, _, transport, linux_dir = pairing_pair
    _open_and_join(nas, worker)
    worker.approve_worker()
    transport.missing_ack_once = True
    with pytest.raises(PairingError):
        nas.finish()
    assert (linux_dir / "cluster-worker-secret.json").exists()
    assert nas_c.configured is None
    assert nas.finish()["paired"]
    assert nas_c.configured is not None


def test_pairing_expires_and_does_not_auto_enroll(pairing_pair):
    nas, worker, nas_c, *_ = pairing_pair
    _open_and_join(nas, worker)
    worker.approve_worker()
    nas._window = time.monotonic() - 1
    with pytest.raises(PairingError, match="active"):
        nas.finish()
    assert nas_c.configured is None


def test_saved_linux_worker_secret_reloaded_after_reboot(pairing_pair):
    nas, worker, _, nas_discovery, _, linux_dir = pairing_pair
    _open_and_join(nas, worker)
    worker.approve_worker()
    nas.finish()
    saved = nas_discovery.secret
    worker.shutdown()
    assert worker_secret() is None
    reloaded = PairingManager(
        linux_dir, FakeCluster(), FakeDiscovery(), linux=True,
    )
    assert worker_secret() == saved
    assert reloaded.status()["worker_key_configured"] is True
    assert reloaded.arm()["already_paired"] is True
    reloaded.shutdown()


def test_web_revocation_clears_linux_saved_secret_without_exposing_key(pairing_pair):
    nas, worker, _, nas_discovery, _, linux_dir = pairing_pair
    _open_and_join(nas, worker)
    worker.approve_worker()
    nas.finish()
    assert worker_secret() == nas_discovery.secret
    assert worker.reset_linux()["revoked"] is True
    assert worker_secret() is None
    assert not (linux_dir / "cluster-worker-secret.json").exists()
    assert worker.arm()["armed"] is True


def test_revoke_refuses_to_override_externally_managed_secret(pairing_pair, monkeypatch):
    nas, worker, *_ = pairing_pair
    monkeypatch.setenv("FRAMEDECK_CLUSTER_WORKER_SECRET", "7a" * 32)
    with pytest.raises(PairingError, match="externally managed"):
        worker.reset_linux()
    assert worker_secret() == "7a" * 32


def test_untrusted_pairing_origin_does_not_activate_worker(app_env):
    services, _, _ = app_env
    client = TestClient(create_app(services))
    response = client.get("/api/cluster/pairing/status")
    assert response.status_code == 200
    assert "secret" not in response.text
    assert client.post(
        "/api/cluster/pairing/revoke",
        headers={"sec-fetch-site": "cross-site"},
    ).status_code == 403


def test_pairing_endpoints_fail_closed_when_not_armed(app_env):
    services, _, _ = app_env
    client = TestClient(create_app(services))
    status = client.get("/api/cluster/status")
    assert status.status_code == 200
    assert "confirmation_code" not in status.text
    assert "candidates" not in status.text
    bad = client.post("/api/cluster/pairing/hello",
                      json={"id": "a" * 32, "nas_pub": "1" * 64})
    assert bad.status_code == 403
    unauth = client.post("/api/cluster/pairing/commit",
                         json={"id": "a" * 32, "proof": "b" * 64})
    assert unauth.status_code == 403
    assert client.post("/api/cluster/pairing/hello",
                       data="x" * 900,
                       headers={"content-type": "application/json"}).status_code == 413
    assert client.post("/api/cluster/pairing/hello",
                       data=b"{}",
                       headers={"content-type": "text/plain"}).status_code == 415


def test_two_web_apps_pair_without_ever_sending_secret(app_env, tmp_path, monkeypatch):
    """Both real FastAPI admin surfaces drive the pairing handshake."""
    from framedeck.config import Settings, ensure_runtime_directories, resolve_app_paths
    from framedeck.core.services import build_services
    from framedeck.core.storage import Storage

    monkeypatch.delenv("FRAMEDECK_CLUSTER_WORKER_SECRET", raising=False)
    set_runtime_worker_secret(None)
    a, a_paths, _ = app_env
    a.pairing = PairingManager(
        a_paths.config_dir, a.cluster, a.discovery, linux=False,
    )
    a.discovery.pairing = a.pairing
    b_paths = resolve_app_paths(tmp_path / "linux-b")
    ensure_runtime_directories(b_paths)
    b = build_services(Settings(b_paths), Storage(b_paths.database_file), b_paths)
    assert b.pairing.linux
    monkeypatch.setattr(a.cluster, "probe_once", lambda: None)
    b_client = TestClient(create_app(b))
    a_client = TestClient(create_app(a))

    class WebTransport:
        def open(self, req, timeout):
            path = urlsplit(req.full_url).path
            assert path in (
                cluster_pairing.PAIRING_ANNOUNCE_PATH,
                cluster_pairing.PAIRING_COMMIT_PATH,
            )
            assert b"secret" not in req.data
            response = b_client.post(
                path, content=req.data,
                headers={"content-type": "application/json"},
            )
            return Result(response.json(), status=response.status_code)

    monkeypatch.setattr(cluster_pairing, "_secure_opener", lambda: WebTransport())
    assert b_client.post("/api/cluster/pairing/start").status_code == 200
    assert a_client.post("/api/cluster/pairing/start").status_code == 200
    offer = b.pairing.offer_packet(9000)
    assert a.pairing.accept_offer(offer, "192.168.1.80")
    pending = a_client.get("/api/cluster/pairing/status").json()
    assert len(pending["candidates"]) == 1
    chosen = a_client.post(
        "/api/cluster/pairing/join",
        json={"id": pending["candidates"][0]["id"]},
    )
    assert chosen.status_code == 200
    nas_code = a_client.get("/api/cluster/pairing/status").json()["confirmation_code"]
    linux_code = b_client.get("/api/cluster/pairing/status").json()["confirmation_code"]
    assert nas_code == linux_code
    assert len(nas_code) == 6
    assert "secret" not in a_client.get("/api/cluster/pairing/status").text
    assert "secret" not in b_client.get("/api/cluster/pairing/status").text

    assert b_client.post("/api/cluster/pairing/approve").status_code == 200
    finished = a_client.post("/api/cluster/pairing/finish")
    assert finished.status_code == 200
    assert finished.json()["paired"] is True
    assert a_client.get("/api/cluster/status").json()["peer"]["configured"]
    assert b_client.get("/api/cluster/status").json()["secure_pairing"]["worker_key_configured"]
    assert "secret" not in a_client.get("/api/cluster/status").text
    b.shutdown()


def test_browser_pairing_mutations_reject_cross_site(app_env):
    services, _, _ = app_env
    client = TestClient(create_app(services))
    assert client.post(
        "/api/cluster/pairing/start",
        headers={"sec-fetch-site": "cross-site"},
    ).status_code == 403
