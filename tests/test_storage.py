"""SQLite永続化のテスト。"""
from framedeck.core.storage import Storage


def test_reading_progress_roundtrip(tmp_path):
    storage = Storage(tmp_path / "test.db")
    storage.save_reading_progress("entry1", 4, 20, reader_mode="spread",
                                  reading_direction="rtl")
    progress = storage.get_reading_progress("entry1")
    assert progress["page_index"] == 4
    assert progress["page_count"] == 20
    assert progress["completed"] == 0
    storage.save_reading_progress("entry1", 19, 20, completed=True)
    assert storage.get_reading_progress("entry1")["completed"] == 1
    storage.close()


def test_video_progress_roundtrip(tmp_path):
    storage = Storage(tmp_path / "test.db")
    storage.save_video_progress("vid1", 120.5, 3600.0, speed=1.5,
                                audio_track=1, subtitle_track=None)
    progress = storage.get_video_progress("vid1")
    assert progress["position_seconds"] == 120.5
    assert progress["playback_speed"] == 1.5
    storage.close()


def test_roots_and_state(tmp_path):
    storage = Storage(tmp_path / "test.db")
    storage.add_root("r1", "/some/path", "comic")
    storage.add_root("r2", "/some/path", "video")
    assert [root["kind"] for root in storage.list_roots()] == ["comic", "video"]
    assert storage.list_roots()[0]["path"] == "/some/path"
    storage.remove_root("r1")
    storage.remove_root("r2")
    assert storage.list_roots() == []

    storage.set_state("last_folder", {"path": "/a", "mode": "comic"})
    assert storage.get_state("last_folder")["mode"] == "comic"
    storage.close()


def test_recent_dedup(tmp_path):
    storage = Storage(tmp_path / "test.db")
    storage.add_recent("m1", "/p1", "video")
    storage.add_recent("m2", "/p2", "video")
    storage.add_recent("m1", "/p1", "video")
    recent = storage.list_recent()
    assert [r["media_id"] for r in recent] == ["m1", "m2"]
    storage.close()


def test_settings_persistence(tmp_path):
    from framedeck.config import Settings, ensure_runtime_directories, resolve_app_paths
    paths = resolve_app_paths(tmp_path / "home")
    ensure_runtime_directories(paths)
    settings = Settings(paths)
    settings.set("reading_direction", "ltr")
    reloaded = Settings(paths)
    assert reloaded.get("reading_direction") == "ltr"

    # 不正値は既定値へフォールバック
    import json
    data = json.loads(paths.settings_file.read_text())
    data["view_mode"] = "bogus"
    paths.settings_file.write_text(json.dumps(data))
    fixed = Settings(paths)
    assert fixed.get("view_mode") == "spread"
