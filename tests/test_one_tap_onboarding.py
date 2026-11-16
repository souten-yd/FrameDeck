"""Onboarding PIN protection without a shell, raw URL keys, or LAN trust bypass."""
from __future__ import annotations

from fastapi.testclient import TestClient

from framedeck.web.app import PinLoginLimiter, create_app


def test_one_tap_prerequisites_can_be_set_from_web_ui(app_env):
    services, paths, _ = app_env
    client = TestClient(create_app(services))
    status = client.get("/api/cluster/status")
    assert status.status_code == 200
    assert status.json()["web_pin_configured"] is False
    assert status.json()["auto_discovery_enabled"] is False

    # Web UI button creates 20 cryptographically secure random bytes.
    pin = "a5" * 20
    updated = client.put("/api/settings", json={"web_pin": pin})
    assert updated.status_code == 200
    assert "web_pin" not in updated.json()
    assert services.settings.get("web_pin") == pin
    assert paths.settings_file.stat().st_mode & 0o077 == 0
    assert (paths.settings_file.parent / "settings.json.tmp").exists() is False

    wrong = client.post("/api/auth/pin", json={"pin": "wrong"})
    assert wrong.status_code == 401
    authenticated = client.post("/api/auth/pin", json={"pin": pin})
    assert authenticated.status_code == 200
    cookie = authenticated.headers.get("set-cookie", "").lower()
    assert "httponly" in cookie
    assert "samesite=strict" in cookie
    assert "framedeck_pin=" in cookie
    assert pin not in authenticated.text
    assert client.get("/api/cluster/status").json()["web_pin_configured"] is True

    enabled = client.post("/api/cluster/discovery/enable")
    assert enabled.status_code == 200
    assert enabled.json()["enabled"] is True
    assert len(enabled.json()["secret"]) == 64
    again = client.post("/api/cluster/discovery/enable")
    assert again.status_code == 200
    assert again.json()["secret"] is None
    assert "secret" not in client.get("/api/cluster/status").text


def test_web_pin_login_rejects_cross_site_and_wrong_type(app_env):
    services, _, _ = app_env
    services.settings.set("web_pin", "f1" * 20)
    client = TestClient(create_app(services))
    assert client.post("/api/auth/pin", json={"pin": ["f1"]}).status_code == 401
    assert client.post(
        "/api/auth/pin", json={"pin": "f1" * 20},
        headers={"origin": "https://attacker.invalid", "host": "nas.internal:9000"},
    ).status_code == 403
    assert client.post(
        "/api/auth/pin", json={"pin": "f1" * 20},
        headers={"sec-fetch-site": "cross-site"},
    ).status_code == 403


def test_web_pin_login_fails_closed_without_configured_pin(app_env):
    services, _, _ = app_env
    client = TestClient(create_app(services))
    assert client.post("/api/auth/pin", json={"pin": "secret"}).status_code == 403


def test_pin_login_memory_only_rate_limit_and_reset(monkeypatch):
    from framedeck.web import app as app_module
    tick = [500.0]
    monkeypatch.setattr(app_module.time, "monotonic", lambda: tick[0])
    limiter = PinLoginLimiter()
    for _ in range(5):
        assert limiter.allow("192.168.1.9")
        limiter.failed("192.168.1.9")
    assert not limiter.allow("192.168.1.9")
    assert limiter.allow("192.168.1.10")
    tick[0] += 61.0
    assert limiter.allow("192.168.1.9")
    limiter.failed("192.168.1.9")
    limiter.success("192.168.1.9")
    assert limiter.allow("192.168.1.9")
