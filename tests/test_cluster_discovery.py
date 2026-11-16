"""LAN worker discovery is explicit-trust, authenticated and write-thrifty."""
from __future__ import annotations

import json
import os

import pytest
from fastapi.testclient import TestClient

from framedeck.cluster_discovery import DISCOVERY_PORT, LanDiscovery, make_beacon
from framedeck.cluster_worker import ClusterController
from framedeck.web.app import create_app

SECRET = "39" * 32


def test_discovery_is_disabled_by_default_and_never_persists_peer(tmp_path):
    controller = ClusterController(tmp_path)
    discovery = LanDiscovery(controller, tmp_path)
    assert discovery.enabled() is False
    assert discovery.accept(make_beacon(SECRET, 9000), "192.168.1.15") is False
    assert controller.status()["configured"] is False


def test_discovery_enable_persists_secret_with_owner_only_permissions(tmp_path):
    controller = ClusterController(tmp_path)
    discovery = LanDiscovery(controller, tmp_path)
    secret = discovery.enable()
    assert len(secret) == 64
    assert (tmp_path / "cluster-discovery.json").is_file()
    assert os.stat(tmp_path / "cluster-discovery.json").st_mode & 0o077 == 0
    assert LanDiscovery(controller, tmp_path).discovery_secret() == secret
    assert "secret" not in json.dumps(controller.status())
    discovery.disable()
    assert not (tmp_path / "cluster-discovery.json").exists()
    assert not discovery.enabled()


def test_trusted_worker_announces_and_updates_ip_without_repeated_writes(tmp_path, monkeypatch):
    controller = ClusterController(tmp_path)
    discovery = LanDiscovery(controller, tmp_path)
    key = discovery.enable()
    monkeypatch.setattr(discovery, "_verify_candidate", lambda url, secret: True)
    beacon = make_beacon(key, 9123, now=1700000000, nonce="b" * 32)
    assert discovery.accept(beacon, "192.168.2.20", now=1700000000)
    peer_config = tmp_path / "cluster-worker.json"
    assert controller.status()["url"] == "http://192.168.2.20:9123"
    stamp = peer_config.stat().st_mtime_ns
    newer = make_beacon(key, 9123, now=1700000001, nonce="c" * 32)
    assert discovery.accept(newer, "192.168.2.20", now=1700000001)
    assert peer_config.stat().st_mtime_ns == stamp
    other = make_beacon(key, 9123, now=1700000002, nonce="d" * 32)
    assert discovery.accept(other, "192.168.2.21", now=1700000002)
    assert controller.status()["url"] == "http://192.168.2.21:9123"


def test_rejects_fake_public_replayed_or_unknown_workers(tmp_path, monkeypatch):
    controller = ClusterController(tmp_path)
    discovery = LanDiscovery(controller, tmp_path)
    secret = discovery.enable()
    monkeypatch.setattr(discovery, "_verify_candidate", lambda url, secret: True)
    packet = make_beacon(secret, 9000, now=1700000000, nonce="e" * 32)
    assert not discovery.accept(packet, "8.8.8.8", now=1700000000)
    assert not discovery.accept(packet, "127.0.0.1", now=1700000000)
    assert discovery.accept(packet, "192.168.1.10", now=1700000000)
    assert not discovery.accept(packet, "192.168.1.10", now=1700000000)
    assert not discovery.accept(
        make_beacon("17" * 32, 9000, now=1700000001),
        "192.168.1.20", now=1700000001,
    )


def test_unverified_tcp_worker_never_becomes_peer(tmp_path, monkeypatch):
    controller = ClusterController(tmp_path)
    discovery = LanDiscovery(controller, tmp_path)
    secret = discovery.enable()
    monkeypatch.setattr(discovery, "_verify_candidate", lambda *_: False)
    packet = make_beacon(secret, 9000, now=1700000000)
    assert not discovery.accept(packet, "192.168.1.10", now=1700000000)
    assert not (tmp_path / "cluster-worker.json").exists()


def test_discovery_admin_api_only_discloses_key_at_first_enable(app_env):
    services, _, _ = app_env
    client = TestClient(create_app(services))
    assert client.get("/api/cluster/status").json()["auto_discovery_enabled"] is False
    first = client.post("/api/cluster/discovery/enable")
    assert first.status_code == 200
    assert len(first.json()["secret"]) == 64
    second = client.post("/api/cluster/discovery/enable")
    assert second.status_code == 200
    assert second.json()["secret"] is None
    assert "secret" not in client.get("/api/cluster/status").text
    assert client.delete("/api/cluster/discovery").status_code == 200
    assert client.get("/api/cluster/status").json()["auto_discovery_enabled"] is False
