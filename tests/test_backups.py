"""Online metadata snapshot, corruption rejection and backup rotation tests."""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from framedeck.core.backup_service import BackupError, BackupService
from framedeck.web.app import create_app


def test_online_backup_verifies_and_preserves_progress(app_env):
    services, paths, _ = app_env
    services.storage.save_video_progress("video-1", 61.5, 360.0)
    services.storage.save_reading_progress("comic-entry", 3, 12)
    result = services.backups.create_snapshot()
    assert result["name"]
    root = paths.base / "backups"
    snapshot = root / result["name"]
    assert (root.stat().st_mode & 0o077) == 0
    assert (snapshot.stat().st_mode & 0o077) == 0
    for name in ("framedeck.db", "settings.json", "manifest.json"):
        assert (snapshot / name).exists()
        assert ((snapshot / name).stat().st_mode & 0o077) == 0
    assert services.backups.verify(result["name"])["verified"]
    with sqlite3.connect(str(snapshot / "framedeck.db")) as backup_db:
        pos = backup_db.execute(
            "SELECT position_seconds FROM video_progress WHERE media_id=?",
            ("video-1",),
        ).fetchone()
        assert pos[0] == pytest.approx(61.5)
        comic = backup_db.execute(
            "SELECT page_index FROM reading_progress WHERE entry_id=?",
            ("comic-entry",),
        ).fetchone()
        assert comic[0] == 3

    # The source remains writable and independent of the completed snapshot.
    services.storage.save_video_progress("video-1", 90.0, 360.0)
    with sqlite3.connect(str(snapshot / "framedeck.db")) as backup_db:
        pos = backup_db.execute(
            "SELECT position_seconds FROM video_progress WHERE media_id=?",
            ("video-1",),
        ).fetchone()
        assert pos[0] == pytest.approx(61.5)


def test_checksum_prevents_using_corrupted_generation(app_env):
    services, paths, _ = app_env
    saved = services.backups.create_snapshot()
    db_copy = paths.base / "backups" / saved["name"] / "framedeck.db"
    with db_copy.open("ab") as fd:
        fd.write(b"corruption")
    with pytest.raises(BackupError):
        services.backups.verify(saved["name"])
    assert services.storage.get_state("anything", None) is None


def test_invalid_name_and_malformed_manifest_rejected(app_env):
    services, paths, _ = app_env
    result = services.backups.create_snapshot()
    for name in ("../outside", ".incomplete-123", "/tmp/", "unknown"):
        with pytest.raises(BackupError):
            services.backups.verify(name)
    folder = paths.base / "backups" / result["name"]
    (folder / "manifest.json").write_text('{"format_version":1,"files":null}')
    with pytest.raises(BackupError):
        services.backups.verify(result["name"])


def test_includes_private_worker_pairing_file_if_present(app_env):
    services, paths, _ = app_env
    pairing = paths.config_dir / "cluster-worker.json"
    pairing.write_text('{"url":"http://192.168.1.2:9000","secret":"very-secret"}')
    pairing.chmod(0o600)
    result = services.backups.create_snapshot()
    backup = paths.base / "backups" / result["name"]
    assert (backup / "cluster-worker.json").is_file()
    assert (backup / "cluster-worker.json").stat().st_mode & 0o077 == 0
    assert services.backups.verify(result["name"])["verified"]


def test_backup_status_api_and_manual_verify(app_env):
    services, _, _ = app_env
    client = TestClient(create_app(services))
    status = client.get("/api/backups/status")
    assert status.status_code == 200
    assert status.json()["enabled"] is False
    assert status.json()["daily_generations"] == 7
    assert status.json()["weekly_generations"] == 4
    assert status.json()["snapshots"] == []

    created = client.post("/api/backups/create")
    assert created.status_code == 200
    name = created.json()["name"]
    assert client.post(f"/api/backups/{name}/verify").json()["verified"]
    assert client.post("/api/backups/../verify").status_code != 200
    assert len(client.get("/api/backups/status").json()["snapshots"]) == 1
    # Do not expose absolute paths, secrets, or restore/delete interfaces.
    assert "web_pin" not in status.text
    assert client.post(f"/api/backups/{name}/restore").status_code == 404
    assert client.delete(f"/api/backups/{name}").status_code in (404, 405)


def test_retention_seven_daily_four_weekly(tmp_path):
    from framedeck.config import resolve_app_paths
    paths = resolve_app_paths(tmp_path / "FrameDeck_venv")
    backup = BackupService(paths)
    backup._ensure_private_root()
    start = datetime(2026, 11, 16, 3, tzinfo=timezone.utc)
    for day in range(35):
        t = start - timedelta(days=day)
        name = t.strftime("%Y%m%dT%H%M%S") + "-" + f"{day:08x}"
        folder = backup.root / name
        folder.mkdir()
        with sqlite3.connect(str(folder / "framedeck.db")) as db:
            db.execute("CREATE TABLE IF NOT EXISTS marker (day INTEGER)")
            db.execute("INSERT INTO marker VALUES (?)", (day,))
        (folder / "settings.json").write_text('{"video_stream_mode":"auto"}')
        metadata = backup._make_metadata(folder)
        metadata["created_at"] = t.isoformat()
        metadata["local_date"] = t.date().isoformat()
        metadata["iso_week"] = f"{t.isocalendar().year}-W{t.isocalendar().week:02d}"
        (folder / "manifest.json").write_text(json.dumps(metadata))
    backup.prune()
    remaining = backup.list_snapshots()
    assert len(remaining) <= 11
    assert len(remaining) >= 7
    assert len({s["local_date"] for s in remaining[:7]}) >= 5
    assert len({s["iso_week"] for s in remaining}) >= 4


def test_corrupted_newest_snapshot_cannot_displace_verified_prior(app_env):
    services, paths, _ = app_env
    first = services.backups.create_snapshot()
    second = services.backups.create_snapshot()
    root = paths.base / "backups"
    corrupted = root / second["name"] / "framedeck.db"
    with corrupted.open("ab") as out:
        out.write(b"corruption")
    services.backups.prune()
    assert (root / first["name"]).exists()
    assert (root / second["name"]).exists()  # retain corrupt for diagnosis
    assert services.backups.verify(first["name"])["verified"]




def test_no_snapshot_created_if_disk_is_full(app_env, monkeypatch):
    services, paths, _ = app_env
    import framedeck.core.backup_service as backup_module
    original = backup_module.os.statvfs
    class NoSpace:
        f_bavail = 0
        f_frsize = 4096
    monkeypatch.setattr(backup_module.os, "statvfs", lambda path: NoSpace())
    with pytest.raises(BackupError):
        services.backups.create_snapshot()
    assert services.backups.list_snapshots() == []
    monkeypatch.setattr(backup_module.os, "statvfs", original)


def test_unauthenticated_lan_cannot_trigger_backup_when_pin_absent(app_env):
    from starlette.requests import Request
    from fastapi import HTTPException
    from framedeck.web.routers.backups import _require_backup_admin
    services, _, _ = app_env
    scope = {
        "type": "http", "http_version": "1.1", "method": "POST",
        "path": "/api/backups/create", "query_string": b"",
        "headers": [(b"host", b"192.168.1.10:9000")],
        "client": ("192.168.1.12", 50000),
        "server": ("192.168.1.10", 9000), "scheme": "http",
    }
    with pytest.raises(HTTPException) as exc:
        _require_backup_admin(Request(scope), services)
    assert exc.value.status_code == 403
