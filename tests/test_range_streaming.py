"""Range Requestストリーミングのテスト(指示書25.4)。"""
import pytest
from fastapi.testclient import TestClient

from framedeck.core.library_service import item_id_for
from framedeck.video.stream import RangeNotSatisfiable, parse_range_header
from framedeck.web.app import create_app

FILE_SIZE = 100_000


@pytest.fixture()
def video_env(app_env):
    services, paths, tmp_path = app_env
    videos = tmp_path / "videos"
    videos.mkdir()
    video = videos / "sample.mp4"
    video.write_bytes(bytes(range(256)) * (FILE_SIZE // 256) +
                      bytes(FILE_SIZE % 256))
    services.library.add_root(str(videos), "video")
    root_id = services.library.list_roots()[-1]["id"]
    client = TestClient(create_app(services))
    # 一覧取得でIDを登録
    client.get(f"/api/library/items?folder_id={root_id}&mode=video")
    return client, item_id_for(str(video)), video


def test_parse_range_header():
    assert parse_range_header(None, 1000) is None
    assert parse_range_header("bytes=0-", 1000) == (0, 999)
    assert parse_range_header("bytes=100-199", 1000) == (100, 199)
    assert parse_range_header("bytes=900-5000", 1000) == (900, 999)
    assert parse_range_header("bytes=-100", 1000) == (900, 999)
    with pytest.raises(RangeNotSatisfiable):
        parse_range_header("bytes=1000-", 1000)
    with pytest.raises(RangeNotSatisfiable):
        parse_range_header("bytes=-", 1000)
    # 不正な形式・複数レンジは全体送信へフォールバック
    assert parse_range_header("bytes=abc", 1000) is None
    assert parse_range_header("bytes=0-1,5-9", 1000) is None


def test_full_stream(video_env):
    client, media_id, video = video_env
    response = client.get(f"/api/videos/{media_id}/stream")
    assert response.status_code == 200
    assert response.headers["accept-ranges"] == "bytes"
    assert int(response.headers["content-length"]) == FILE_SIZE
    assert response.content == video.read_bytes()


def test_open_ended_range(video_env):
    client, media_id, video = video_env
    response = client.get(f"/api/videos/{media_id}/stream",
                          headers={"Range": "bytes=0-"})
    assert response.status_code == 206
    assert response.headers["content-range"] == \
        f"bytes 0-{FILE_SIZE - 1}/{FILE_SIZE}"


def test_mid_range(video_env):
    client, media_id, video = video_env
    response = client.get(f"/api/videos/{media_id}/stream",
                          headers={"Range": "bytes=1000-1999"})
    assert response.status_code == 206
    assert len(response.content) == 1000
    assert response.content == video.read_bytes()[1000:2000]


def test_suffix_range(video_env):
    client, media_id, _ = video_env
    response = client.get(f"/api/videos/{media_id}/stream",
                          headers={"Range": "bytes=-500"})
    assert response.status_code == 206
    assert len(response.content) == 500


def test_invalid_range_416(video_env):
    client, media_id, _ = video_env
    response = client.get(f"/api/videos/{media_id}/stream",
                          headers={"Range": f"bytes={FILE_SIZE + 10}-"})
    assert response.status_code == 416
    assert response.headers["content-range"] == f"bytes */{FILE_SIZE}"


def test_head_request(video_env):
    client, media_id, _ = video_env
    response = client.head(f"/api/videos/{media_id}/stream")
    assert response.status_code == 200
    assert int(response.headers["content-length"]) == FILE_SIZE
    assert response.content == b""


def test_progress_save_and_detail(video_env):
    client, media_id, _ = video_env
    response = client.post(
        f"/api/videos/{media_id}/progress",
        json={"position_seconds": 63.5, "duration_seconds": 120.0,
              "playback_speed": 1.25},
    )
    assert response.status_code == 200
    detail = client.get(f"/api/videos/{media_id}").json()
    assert detail["progress"]["position_seconds"] == 63.5
    assert detail["resume_position"] == 63.5


def test_progress_save_accepts_null_duration(video_env):
    client, media_id, _ = video_env
    response = client.post(
        f"/api/videos/{media_id}/progress",
        json={"position_seconds": None, "duration_seconds": None,
              "playback_speed": None},
    )
    assert response.status_code == 200
    detail = client.get(f"/api/videos/{media_id}").json()
    assert detail["progress"]["position_seconds"] == 0.0



def test_completed_video_restarts(video_env):
    client, media_id, _ = video_env
    client.post(
        f"/api/videos/{media_id}/progress",
        json={"position_seconds": 119.0, "duration_seconds": 120.0},
    )
    detail = client.get(f"/api/videos/{media_id}").json()
    assert detail["resume_position"] == 0.0


def test_stream_sends_cache_validators(video_env):
    """ETag/Last-Modifiedが無いとブラウザのメディアキャッシュが範囲を
    再利用できず、シークのたびに取り直しになって先読みが痩せる。"""
    client, media_id, video = video_env
    response = client.get(f"/api/videos/{media_id}/stream",
                          headers={"Range": "bytes=0-99"})
    assert response.status_code == 206
    etag = response.headers["etag"]
    assert etag.startswith('"') and etag.endswith('"')
    assert response.headers["last-modified"]
    assert response.headers["accept-ranges"] == "bytes"
    assert "max-age" in response.headers["cache-control"]

    # 同じ検証子なら304
    cached = client.get(f"/api/videos/{media_id}/stream",
                        headers={"If-None-Match": etag})
    assert cached.status_code == 304

    # If-Range が一致すれば部分応答、不一致なら全体
    partial = client.get(f"/api/videos/{media_id}/stream",
                         headers={"Range": "bytes=10-19", "If-Range": etag})
    assert partial.status_code == 206
    assert len(partial.content) == 10
    stale = client.get(f"/api/videos/{media_id}/stream",
                       headers={"Range": "bytes=10-19", "If-Range": '"stale"'})
    assert stale.status_code == 200
    assert len(stale.content) == FILE_SIZE

    # 内容が変われば検証子も変わる
    video.write_bytes(video.read_bytes() + b"extra")
    changed = client.get(f"/api/videos/{media_id}/stream",
                         headers={"Range": "bytes=0-99"})
    assert changed.headers["etag"] != etag


def test_range_stream_uses_readahead(video_env):
    """NAS側の一時的な遅延を吸収するため、読み出しは先読みして送出する。"""
    from framedeck.web.routers import video as video_router

    assert video_router.READAHEAD_CHUNKS >= 4
    assert video_router.SLOW_READ_WARN_MS > 0
    client, media_id, video = video_env
    # 先読みを挟んでも内容・長さは一致する
    response = client.get(f"/api/videos/{media_id}/stream",
                          headers={"Range": "bytes=1000-5999"})
    assert response.status_code == 206
    assert response.content == video.read_bytes()[1000:6000]
    whole = client.get(f"/api/videos/{media_id}/stream")
    assert whole.content == video.read_bytes()
