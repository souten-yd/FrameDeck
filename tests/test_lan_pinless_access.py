"""Direct private-LAN browser access works without a Web PIN.

Regression for a previously auto-generated PIN locking the user's iPhone
out of their own FrameDeck UI.  Public/forwarded/other-origin sessions
must never inherit the same access.
"""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from starlette.requests import Request

from framedeck.web.app import create_app
from framedeck.web.lan_access import trusted_direct_lan


def _client_at(services, peer: str, target: str):
    app = create_app(services)

    @app.middleware("http")
    async def synthetic_tcp_peer(request, call_next):
        request.scope["client"] = (peer, 53920)
        return await call_next(request)

    return TestClient(app, base_url=target)


def test_private_lan_browser_no_pin_even_if_old_random_pin_exists(app_env):
    services, _, _ = app_env
    services.settings.set("web_pin", "previously-generated-random-secret")
    with _client_at(services, "192.168.68.24",
                    "http://192.168.68.200:9000") as client:
        settings = client.get("/api/settings")
        assert settings.status_code == 200
        assert "web_pin" not in settings.json()
        status = client.get("/api/cluster/status")
        assert status.status_code == 200
        assert status.json()["secure_pairing"]["available"] in (True, False)
        pairing = client.post("/api/cluster/pairing/start")
        assert pairing.status_code in (200, 409)
        assert pairing.status_code != 401
        backup = client.get("/api/backups/status")
        assert backup.status_code == 200


def test_no_pin_does_not_make_external_browser_an_admin(app_env):
    services, _, _ = app_env
    with _client_at(services, "8.8.8.8",
                    "http://frame.example:9000") as client:
        assert client.get("/api/settings").status_code == 403
        assert client.get("/api/cluster/status").status_code == 403

    services.settings.set("web_pin", "manual-external-pin")
    with _client_at(services, "8.8.8.8",
                    "http://frame.example:9000") as client:
        assert client.get("/api/settings").status_code == 401
        response = client.post("/api/auth/pin", json={"pin": "manual-external-pin"})
        assert response.status_code == 200
        assert client.get("/api/settings").status_code == 200


def test_lan_pinless_access_does_not_trust_proxy_dns_rebinding_or_vpn_peer(app_env):
    services, _, _ = app_env
    services.settings.set("web_pin", "")
    for peer, target, headers in (
        ("192.168.68.24", "http://attacker.example:9000", {}),
        ("192.168.68.24", "http://192.168.68.200:9000",
         {"x-forwarded-for": "8.8.8.8"}),
        ("100.100.100.100", "http://192.168.68.200:9000", {}),
        ("192.168.68.24", "http://8.8.8.8:9000", {}),
    ):
        with _client_at(services, peer, target) as client:
            assert client.get("/api/settings", headers=headers).status_code == 403


def test_private_lan_pinless_state_changes_reject_browser_csrf(app_env):
    services, _, _ = app_env
    with _client_at(services, "192.168.68.24",
                    "http://192.168.68.200:9000") as client:
        assert client.post(
            "/api/cluster/pairing/start",
            headers={"sec-fetch-site": "cross-site"},
        ).status_code == 403
        assert client.put(
            "/api/settings", json={"comic_progress_save_seconds": 30},
            headers={"origin": "https://attacker.example"},
        ).status_code == 403
        assert client.post(
            "/api/cluster/pairing/start",
            headers={"origin": "http://192.168.68.200:9000"},
        ).status_code in (200, 409)


def test_pinless_direct_lan_is_explicitly_disableable(app_env):
    services, _, _ = app_env
    services.settings.set("web_pin", "manual-pin")
    services.settings.set("lan_pinless_access", False)
    with _client_at(services, "192.168.68.24",
                    "http://192.168.68.200:9000") as client:
        assert client.get("/api/settings").status_code == 401
        assert client.post("/api/auth/pin",
                           json={"pin": "manual-pin"}).status_code == 200
        assert client.get("/api/settings").status_code == 200
        assert client.put("/api/settings", json={
            "lan_pinless_access": True,
        }).status_code == 200
        assert client.get("/api/settings").json()["lan_pinless_access"] is True


def test_trusted_ip_detection_uses_real_peer_not_host_alone():
    scope = {
        "type": "http", "http_version": "1.1", "method": "GET",
        "path": "/api/settings", "query_string": b"", "scheme": "http",
        "headers": [(b"host", b"192.168.68.200:9000")],
        "client": ("192.168.68.25", 50000),
        "server": ("192.168.68.200", 9000),
    }
    assert trusted_direct_lan(Request(scope))
    scope["client"] = ("203.0.113.25", 50000)
    assert not trusted_direct_lan(Request(scope))


def test_browser_pairing_no_longer_auto_generates_web_pin():
    script = (Path(__file__).resolve().parents[1] /
              "framedeck" / "web" / "static" / "js" / "app.js").read_text("utf-8")
    assert "ensureLocalAdminSession(" not in script
    assert "generatedPin" not in script
    assert 'json: { web_pin: pin }' not in script
    assert '"/api/cluster/pairing/start"' in script
