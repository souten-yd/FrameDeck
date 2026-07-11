"""Web APIのテスト(ライブラリ・漫画セッション・削除・設定・起動)。"""
import pytest
from fastapi.testclient import TestClient

from framedeck.web.app import create_app


@pytest.fixture()
def client_env(app_env, comic_root):
    services, paths, tmp_path = app_env
    services.library.add_root(str(comic_root), "comic")
    root_id = services.library.list_roots()[-1]["id"]
    client = TestClient(create_app(services))
    return client, services, root_id, comic_root


def test_health(client_env):
    client, *_ = client_env
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_index_serves_html(client_env):
    client, *_ = client_env
    response = client.get("/")
    assert response.status_code == 200
    assert "FrameDeck" in response.text


def test_list_items(client_env):
    client, services, root_id, comic_root = client_env
    response = client.get(
        f"/api/library/items?folder_id={root_id}&mode=comic&sort=name"
    )
    assert response.status_code == 200
    data = response.json()
    names = [i["display_name"] for i in data["items"]]
    assert names == ["B", "A.zip", "C.cbz"]  # フォルダ先頭 + 自然順
    # パスは公開しない
    for item in data["items"]:
        assert "path" not in item
        assert not (item.get("relative_path") or "").startswith("/")


def test_path_outside_roots_forbidden(client_env):
    client, services, root_id, comic_root = client_env
    from framedeck.core.security import PathValidationError
    with pytest.raises(PathValidationError):
        services.library.validate_path("/etc/passwd")


def test_rating_roundtrip_preserves_format(client_env):
    client, services, root_id, comic_root = client_env
    items = client.get(
        f"/api/library/items?folder_id={root_id}&mode=comic"
    ).json()["items"]
    target = next(i for i in items if i["display_name"] == "C.cbz")

    response = client.post(f"/api/library/items/{target['id']}/rating",
                           json={"rating": 4})
    assert response.status_code == 200
    assert response.json()["rating"] == 4
    assert (comic_root / "C{zpi$r=4}.cbz").exists()   # 既存形式を維持

    # 同じIDのまま評価解除できる(IDは評価タグに依存しない)
    response = client.post(f"/api/library/items/{target['id']}/rating",
                           json={"rating": None})
    assert response.status_code == 200
    assert (comic_root / "C.cbz").exists()


def test_delete_requires_token(client_env):
    client, services, root_id, comic_root = client_env
    items = client.get(
        f"/api/library/items?folder_id={root_id}&mode=comic"
    ).json()["items"]
    target = next(i for i in items if i["display_name"] == "C.cbz")

    # トークンなし・不正トークンは拒否
    response = client.delete(
        f"/api/library/items/{target['id']}?token=bogus"
    )
    assert response.status_code == 403
    assert (comic_root / "C.cbz").exists()

    token = client.post(
        f"/api/library/items/{target['id']}/delete-request"
    ).json()["token"]
    services.settings.set("delete_to_trash", False)  # テストでは直接削除
    response = client.delete(
        f"/api/library/items/{target['id']}?token={token}"
    )
    assert response.status_code == 200
    assert not (comic_root / "C.cbz").exists()


def test_comic_session_flow(client_env):
    client, services, root_id, comic_root = client_env
    items = client.get(
        f"/api/library/items?folder_id={root_id}&mode=comic"
    ).json()["items"]

    # 複数候補の親 → requires_choice
    a_zip = next(i for i in items if i["display_name"] == "A.zip")
    response = client.post("/api/comics/session",
                           json={"item_id": a_zip["id"]})
    assert response.status_code == 200
    data = response.json()
    assert data["requires_choice"] is True
    assert len(data["entries"]) == 2

    # 候補を指定して開く
    entry_id = data["entries"][0]["id"]
    state = client.post("/api/comics/session", json={
        "item_id": a_zip["id"], "entry_id": entry_id,
    }).json()
    assert state["page_count"] == 4
    assert state["visible_pages"] == [0]
    session_id = state["session_id"]

    # ページ送り → 見開き
    state = client.post(f"/api/comics/session/{session_id}/next-page").json()
    assert state["visible_pages"] == [1, 2]

    # ページ画像取得 + ETag
    response = client.get(f"/api/comics/session/{session_id}/page/1")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    etag = response.headers["etag"]
    cached = client.get(f"/api/comics/session/{session_id}/page/1",
                        headers={"If-None-Match": etag})
    assert cached.status_code == 304

    # リサイズ付き
    response = client.get(
        f"/api/comics/session/{session_id}/page/1?w=200&h=200"
    )
    assert response.status_code == 200

    # 次のエントリー(A1 → A2)
    state = client.post(
        f"/api/comics/session/{session_id}/next-entry"
    ).json()
    assert "A2" in state["title"]

    # 範囲外ページは404
    response = client.get(f"/api/comics/session/{session_id}/page/999")
    assert response.status_code == 404

    # クローズ
    response = client.delete(f"/api/comics/session/{session_id}")
    assert response.status_code == 200


def test_single_candidate_opens_directly(client_env):
    client, services, root_id, comic_root = client_env
    items = client.get(
        f"/api/library/items?folder_id={root_id}&mode=comic"
    ).json()["items"]
    c_cbz = next(i for i in items if i["display_name"] == "C.cbz")
    state = client.post("/api/comics/session",
                        json={"item_id": c_cbz["id"]}).json()
    assert "session_id" in state
    assert state["page_count"] == 5


def test_settings_api(client_env):
    client, *_ = client_env
    settings = client.get("/api/settings").json()
    assert settings["comic_sequence_end_behavior"] == "stop"
    assert "web_pin" not in settings

    response = client.put("/api/settings",
                          json={"reading_direction": "ltr"})
    assert response.status_code == 200
    assert response.json()["reading_direction"] == "ltr"

    response = client.put("/api/settings",
                          json={"reading_direction": "bogus"})
    assert response.status_code == 422


def test_system_info(client_env):
    client, *_ = client_env
    info = client.get("/api/system/info").json()
    assert "tools" in info and "mpv" in info["tools"]


def test_invalid_app_mode_rejected(tmp_path):
    from framedeck.bootstrap import run
    with pytest.raises(ValueError):
        run(app_mode="desktop_only", port=59999, open_browser=False,
            base_dir=tmp_path / "home")
