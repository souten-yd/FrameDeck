"""Off-NAS snapshot replication, privacy, failure and no-delete invariants."""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from framedeck.core.backup_service import BackupError
from framedeck.web.app import create_app


@pytest.fixture()
def offnas(app_env, tmp_path, monkeypatch):
    from framedeck.core import backup_replica
    services, _, _ = app_env
    target = tmp_path / "linux-crypt-mount"
    target.mkdir()
    real = Path.is_mount
    monkeypatch.setattr(
        Path, "is_mount",
        lambda path: True if path == target else real(path),
    )
    monkeypatch.setattr(backup_replica, "_separate_device", lambda a, b: True)
    monkeypatch.setattr(backup_replica, "_is_remote_filesystem", lambda _: True)
    services.settings.set("backup_replica_target", str(target))
    services.settings.set("backup_replica_enabled", True)
    return services, target


def test_remote_filesystem_proof_comes_from_kernel_mountinfo(monkeypatch):
    from framedeck.core import backup_replica
    original = Path.read_text
    mountinfo = (
        "41 23 0:4 / / rw - ext4 /dev/root rw\\n"
        "42 23 0:22 / /tmp rw - tmpfs tmpfs rw\\n"
        "43 23 0:88 / /mnt/linux-backup rw - cifs //linux/backup rw\\n"
        "44 23 0:89 / /mnt/unencrypted-usb rw - ext4 /dev/sdb1 rw\\n"
    )
    def fake_read(path, *args, **kwargs):
        if str(path) == "/proc/self/mountinfo":
            return mountinfo
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", fake_read)
    assert backup_replica._is_remote_filesystem(Path("/mnt/linux-backup"))
    assert not backup_replica._is_remote_filesystem(Path("/"))
    assert not backup_replica._is_remote_filesystem(Path("/tmp"))
    assert not backup_replica._is_remote_filesystem(Path("/mnt/unencrypted-usb"))


def test_default_disabled_and_no_disk_activity(app_env):
    services, _, _ = app_env
    assert services.backup_replica.status()["enabled"] is False
    with pytest.raises(BackupError, match="disabled"):
        services.backup_replica.replicate()


def test_verified_offnas_copy_private_and_idempotent(offnas):
    services, target = offnas
    snapshot = services.backups.create_snapshot()
    first = services.backup_replica.replicate()
    assert first["name"] == snapshot["name"]
    assert first["copied"] is True
    assert first["verified"] is True
    remote = target / "FrameDeck-Metadata-Backups" / snapshot["name"]
    assert services.backups.verify_directory(remote)["format_version"] == 1
    assert (remote.stat().st_mode & 0o077) == 0
    assert set(p.name for p in remote.iterdir()) == {
        "framedeck.db", "settings.json", "manifest.json"
    }
    for path in remote.iterdir():
        assert path.stat().st_mode & 0o077 == 0
    result = services.backup_replica.replicate()
    assert result["copied"] is False
    assert result["verified"] is True


def test_previous_complete_generations_are_never_deleted(offnas):
    services, target = offnas
    first = services.backups.create_snapshot()
    services.backup_replica.replicate()
    second = services.backups.create_snapshot()
    assert second["name"] != first["name"]
    services.backup_replica.replicate()
    root = target / "FrameDeck-Metadata-Backups"
    assert (root / first["name"]).is_dir()
    assert (root / second["name"]).is_dir()


def test_corruption_of_existing_replica_is_not_silently_overwritten(offnas):
    services, target = offnas
    snapshot = services.backups.create_snapshot()
    services.backup_replica.replicate()
    remote = target / "FrameDeck-Metadata-Backups" / snapshot["name"] / "framedeck.db"
    with remote.open("ab") as out:
        out.write(b"corrupt")
    with pytest.raises(BackupError):
        services.backup_replica.replicate()
    assert remote.read_bytes().endswith(b"corrupt")  # no auto repair/delete


def test_source_backup_integrity_checked_before_any_remote_writes(offnas):
    services, target = offnas
    snapshot = services.backups.create_snapshot()
    manifest = services.backups.root / snapshot["name"] / "manifest.json"
    manifest.write_text("not-valid-json")
    with pytest.raises(BackupError):
        services.backup_replica.replicate()
    assert not (target / "FrameDeck-Metadata-Backups" / snapshot["name"]).exists()


def test_incomplete_remote_copy_never_publishes(offnas, monkeypatch):
    services, target = offnas
    snapshot = services.backups.create_snapshot()
    original = services.backup_replica._copy_file
    calls = []
    def broken(*args):
        calls.append(True)
        if len(calls) == 2:
            raise OSError("worker unplugged")
        return original(*args)
    monkeypatch.setattr(services.backup_replica, "_copy_file", broken)
    with pytest.raises(BackupError):
        services.backup_replica.replicate()
    root = target / "FrameDeck-Metadata-Backups"
    assert not (root / snapshot["name"]).exists()
    assert not list(root.glob(".incoming-*"))


def test_nonmounted_and_symlink_targets_refused(app_env, tmp_path):
    services, _, _ = app_env
    target = tmp_path / "ordinary-local-dir"
    target.mkdir()
    services.settings.set("backup_replica_target", str(target))
    services.settings.set("backup_replica_enabled", True)
    services.backups.create_snapshot()
    with pytest.raises(BackupError, match="mountpoint"):
        services.backup_replica.replicate()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    services.settings.set("backup_replica_target", str(link))
    with pytest.raises(BackupError, match="mountpoint"):
        services.backup_replica.replicate()


def test_local_root_or_tmpfs_mount_cannot_masquerade_as_offnas(offnas, monkeypatch):
    from framedeck.core import backup_replica
    services, _ = offnas
    services.backups.create_snapshot()
    # Simulates an OS-mounted local filesystem on a different device.
    # This must never count as disaster-recovery storage.
    monkeypatch.setattr(backup_replica, "_is_remote_filesystem", lambda _: False)
    with pytest.raises(BackupError, match="remote NFS/SMB/SSHFS"):
        services.backup_replica.replicate()


def test_backup_replica_api_requires_enable_and_returns_no_absolute_path(offnas):
    services, target = offnas
    services.backups.create_snapshot()
    client = TestClient(create_app(services))
    status = client.get("/api/backups/status")
    assert status.status_code == 200
    assert status.json()["offnas"]["enabled"] is True
    assert str(target) not in status.text
    result = client.post("/api/backups/replicate")
    assert result.status_code == 200
    assert result.json()["verified"] is True
    # No off-NAS delete, restore, arbitrary path or command API.
    assert client.delete("/api/backups/replicate").status_code in (404, 405)
    assert client.post("/api/backups/replicate?path=/etc/passwd").status_code == 200
    assert (target / "FrameDeck-Metadata-Backups").exists()
