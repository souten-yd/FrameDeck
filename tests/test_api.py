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


def test_list_items_searches_files_and_folders(client_env):
    client, services, root_id, comic_root = client_env
    (comic_root / "FindFolder").mkdir()
    response = client.get(
        f"/api/library/items?folder_id={root_id}&mode=comic&sort=name&query=findfolder"
    )
    assert response.status_code == 200
    names = [i["display_name"] for i in response.json()["items"]]
    assert names == ["FindFolder"]

    response = client.get(
        f"/api/library/items?folder_id={root_id}&mode=comic&sort=name&query=cbz"
    )
    assert response.status_code == 200
    names = [i["display_name"] for i in response.json()["items"]]
    assert names == ["C.cbz"]


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

    # 見開き送り
    state = client.post(f"/api/comics/session/{session_id}/next-spread").json()
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
    assert "ffmpeg_source" in info["tools"]



def test_invalid_app_mode_rejected(tmp_path):
    from framedeck.bootstrap import run
    with pytest.raises(ValueError):
        run(app_mode="desktop_only", port=59999, open_browser=False,
            base_dir=tmp_path / "home")

def test_comic_spread_and_page_endpoints_are_distinct(client_env):
    client, services, root_id, comic_root = client_env
    items = client.get(
        f"/api/library/items?folder_id={root_id}&mode=comic"
    ).json()["items"]
    c_cbz = next(i for i in items if i["display_name"] == "C.cbz")
    state = client.post("/api/comics/session",
                        json={"item_id": c_cbz["id"]}).json()
    session_id = state["session_id"]

    state = client.post(f"/api/comics/session/{session_id}/next-spread").json()
    assert state["page_index"] == 1
    assert state["visible_pages"] == [1, 2]
    assert state["root_item_id"] == c_cbz["id"]
    assert state["root_folder_id"] == root_id

    state = client.post(f"/api/comics/session/{session_id}/next-page").json()
    assert state["page_index"] == 2
    assert state["visible_pages"] == [2, 3]

    state = client.post(f"/api/comics/session/{session_id}/previous-page").json()
    assert state["page_index"] == 1
    assert state["visible_pages"] == [1, 2]


def test_library_roots_allow_same_path_for_different_kinds(client_env):
    client, services, root_id, comic_root = client_env

    response = client.post("/api/library/roots",
                           json={"path": str(comic_root), "kind": "video"})
    assert response.status_code == 200
    video_root = response.json()
    assert video_root["kind"] == "video"
    assert video_root["id"] != root_id

    response = client.post("/api/library/roots",
                           json={"path": str(comic_root), "kind": "comic"})
    assert response.status_code == 409

    roots = services.library.list_roots()
    resolved = str(comic_root.resolve())
    assert sum(
        1 for root in roots
        if root["kind"] == "comic" and root["path"] == resolved
    ) == 1
    assert sum(
        1 for root in roots
        if root["kind"] == "video" and root["path"] == resolved
    ) == 1

    comic_items = client.get(
        f"/api/library/items?folder_id={root_id}&mode=comic"
    ).json()
    assert comic_items["folder"]["root_id"] == root_id

    any_root = client.post("/api/library/roots",
                           json={"path": str(comic_root), "kind": "any"}).json()
    assert any_root["id"] != root_id
    comic_items = client.get(
        f"/api/library/items?folder_id={root_id}&mode=comic"
    ).json()
    assert comic_items["folder"]["root_id"] == root_id

    video_items = client.get(
        f"/api/library/items?folder_id={video_root['id']}&mode=video"
    ).json()
    assert video_items["folder"]["root_id"] == video_root["id"]

    response = client.get(
        f"/api/library/items?folder_id={video_root['id']}&mode=comic"
    )
    assert response.status_code == 404


def test_library_root_display_name_is_persisted(client_env):
    client, services, root_id, comic_root = client_env
    extra = comic_root / "B"
    response = client.post("/api/library/roots", json={
        "path": str(extra),
        "kind": "comic",
        "display_name": "Nested Comics",
    })
    assert response.status_code == 200
    created = response.json()
    assert created["display_name"] == "Nested Comics"

    roots = client.get("/api/library/roots").json()
    assert any(
        root["id"] == created["id"] and root["display_name"] == "Nested Comics"
        for root in roots
    )

    response = client.patch(f"/api/library/roots/{created['id']}",
                            json={"display_name": "Updated Comics"})
    assert response.status_code == 200
    assert response.json()["display_name"] == "Updated Comics"

    roots = client.get("/api/library/roots").json()
    assert any(
        root["id"] == created["id"] and root["display_name"] == "Updated Comics"
        for root in roots
    )


def test_folder_item_id_cannot_cross_library_kind(client_env, tmp_path):
    client, services, root_id, comic_root = client_env
    video_root_path = tmp_path / "video_root"
    video_root_path.mkdir()
    response = client.post("/api/library/roots",
                           json={"path": str(video_root_path), "kind": "video"})
    assert response.status_code == 200

    items = client.get(
        f"/api/library/items?folder_id={root_id}&mode=comic"
    ).json()["items"]
    folder = next(item for item in items if item["display_name"] == "B")

    response = client.get(
        f"/api/library/items?folder_id={folder['id']}&mode=video"
    )
    assert response.status_code == 404



def test_comic_analysis_and_variant_page_api(client_env):
    client, services, root_id, comic_root = client_env
    items = client.get(
        f"/api/library/items?folder_id={root_id}&mode=comic"
    ).json()["items"]
    c_cbz = next(i for i in items if i["display_name"] == "C.cbz")
    state = client.post("/api/comics/session", json={"item_id": c_cbz["id"]}).json()
    session_id = state["session_id"]

    analysis = client.get(
        f"/api/comics/session/{session_id}/page/0/analysis"
    )
    assert analysis.status_code == 200
    assert analysis.json()["source_width"] > 0
    assert "is_spread" in analysis.json()

    response = client.get(
        f"/api/comics/session/{session_id}/page/0?width=240&height=320&dpr=1&profile=mobile&format=webp"
    )
    assert response.status_code == 200
    assert response.headers["content-type"] in {"image/webp", "image/jpeg"}
    assert "etag" in response.headers


def test_video_playback_profile_api(client_env, tmp_path):
    from framedeck.core.library_service import item_id_for

    client, services, root_id, comic_root = client_env
    video_root = tmp_path / "videos"
    video_root.mkdir()
    video_path = video_root / "sample.mp4"
    video_path.write_bytes(b"not a real video")
    services.library.add_root(str(video_root), "video")
    media_id = item_id_for(str(video_path))
    stat = video_path.stat()
    services.storage.upsert_media_item(
        media_id, str(video_path), "video", None, stat.st_mtime, stat.st_size
    )

    response = client.post(
        f"/api/videos/{media_id}/playback-profile",
        json={
            "uiProfile": "mobile",
            "saveData": True,
            "viewportWidth": 390,
            "viewportHeight": 844,
            "devicePixelRatio": 2,
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["profile"]["name"] == "480p"
    assert data["profile"]["height"] == 480


def test_hls_cached_playlist_and_segment_delivery(client_env, tmp_path):
    from framedeck.core.library_service import item_id_for

    client, services, root_id, comic_root = client_env
    video_root = tmp_path / "hls_videos"
    video_root.mkdir()
    video_path = video_root / "sample.mp4"
    video_path.write_bytes(b"not a real video")
    services.library.add_root(str(video_root), "video")
    media_id = item_id_for(str(video_path))
    stat = video_path.stat()
    services.storage.upsert_media_item(
        media_id, str(video_path), "video", None, stat.st_mtime, stat.st_size
    )

    manifest = services.hls.manifest_for(
        str(video_path), profiles=["480p"], source_height=0
    )
    variant_dir = manifest.cache_dir / "480p"
    variant_dir.mkdir(parents=True)
    manifest.master.write_text(
        "#EXTM3U\n#EXT-X-VERSION:7\n#EXT-X-STREAM-INF:BANDWIDTH=914000,RESOLUTION=0x480\n480p/playlist.m3u8\n",
        "utf-8",
    )
    (variant_dir / "playlist.m3u8").write_text(
        "#EXTM3U\n#EXT-X-VERSION:7\n#EXT-X-MAP:URI=\"init.mp4\"\n#EXTINF:4.0,\nsegment_00000.m4s\n#EXT-X-ENDLIST\n",
        "utf-8",
    )
    (variant_dir / "init.mp4").write_bytes(b"init")
    (variant_dir / "segment_00000.m4s").write_bytes(b"segment")
    (manifest.cache_dir / "complete").write_text("{}", "utf-8")

    # master はキー付きURLへリダイレクトされる(相対URL解決のため)
    response = client.get(
        f"/api/videos/{media_id}/hls/master.m3u8?profile=480p",
        follow_redirects=False,
    )
    assert response.status_code == 302
    location = response.headers["location"]
    assert f"/hls/{manifest.key}/master.m3u8" in location

    response = client.get(location)
    assert response.status_code == 200
    assert "#EXTM3U" in response.text

    response = client.get(
        f"/api/videos/{media_id}/hls/{manifest.key}/480p/playlist.m3u8")
    assert response.status_code == 200
    assert "segment_00000.m4s" in response.text

    response = client.get(
        f"/api/videos/{media_id}/hls/{manifest.key}/480p/segment_00000.m4s")
    assert response.status_code == 200
    assert response.content == b"segment"

    # 生成ジョブが無いソースへの停止要求は0件
    response = client.post(f"/api/videos/{media_id}/hls/stop")
    assert response.status_code == 200
    assert response.json() == {"stopped": 0}


def test_video_playback_profile_requested_resolution_wins(client_env, tmp_path):
    from framedeck.core.library_service import item_id_for

    client, services, root_id, comic_root = client_env
    video_root = tmp_path / "requested_videos"
    video_root.mkdir()
    video_path = video_root / "sample.mp4"
    video_path.write_bytes(b"not a real video")
    services.library.add_root(str(video_root), "video")
    media_id = item_id_for(str(video_path))
    stat = video_path.stat()
    services.storage.upsert_media_item(
        media_id, str(video_path), "video", None, stat.st_mtime, stat.st_size
    )

    response = client.post(
        f"/api/videos/{media_id}/playback-profile",
        json={"uiProfile": "desktop", "requestedProfile": "360p"},
    )
    assert response.status_code == 200
    assert response.json()["profile"]["name"] == "360p"

    response = client.post(
        f"/api/videos/{media_id}/playback-profile",
        json={"uiProfile": "desktop", "requestedProfile": "original"},
    )
    assert response.status_code == 200
    assert response.json()["profile"]["transcode"] is False
