from __future__ import annotations

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from framedeck.web.routers.system import _require_same_origin


def _request(*, host: str = "nas.local:9000", origin: str | None = None,
             fetch_site: str | None = None) -> Request:
    headers = [(b"host", host.encode())]
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    if fetch_site is not None:
        headers.append((b"sec-fetch-site", fetch_site.encode()))
    scope = {
        "type": "http",
        "method": "POST",
        "scheme": "http",
        "path": "/api/update/apply",
        "headers": headers,
        "server": ("nas.local", 9000),
        "client": ("127.0.0.1", 12345),
    }
    return Request(scope)


def test_update_apply_allows_same_origin_browser_request():
    _require_same_origin(
        _request(origin="http://nas.local:9000", fetch_site="same-origin")
    )


def test_update_apply_allows_non_browser_client_without_origin():
    _require_same_origin(_request())


def test_update_apply_rejects_cross_site_browser_request():
    with pytest.raises(HTTPException) as exc:
        _require_same_origin(
            _request(origin="https://evil.example", fetch_site="cross-site")
        )
    assert exc.value.status_code == 403


def test_update_apply_rejects_mismatched_origin():
    with pytest.raises(HTTPException) as exc:
        _require_same_origin(
            _request(origin="https://other.local:9000", fetch_site="same-site")
        )
    assert exc.value.status_code == 403
