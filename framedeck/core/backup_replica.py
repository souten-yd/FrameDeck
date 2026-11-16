"""Optional verified OFF-NAS copy of *completed* SQLite backup generations.

Only a deliberately configured mountpoint on another filesystem is accepted.
The NAS never automatically mounts an NFS/SMB share, never edits originals,
and never deletes previously completed off-NAS copies. Transport and volume
encryption are provided by the administrator's OS-level mount configuration.
"""
from __future__ import annotations

import logging
import os
import shutil
import stat
import tempfile
import threading
import time
from pathlib import Path

from .backup_service import BackupError, BackupService, _NAME, _sync_directory
from ..config import AppPaths, Settings

logger = logging.getLogger("framedeck")
_MAX_COPY_BYTES = 1024 * 1024 * 1024


def _separate_device(folder: Path, local: Path) -> bool:
    return folder.stat().st_dev != local.stat().st_dev


class BackupReplicaService:
    """Bounded, opt-in snapshot copy. No remove or restore endpoint."""

    def __init__(self, paths: AppPaths, backups: BackupService,
                 cluster, settings: Settings) -> None:
        self.paths = paths
        self.backups = backups
        self.cluster = cluster
        self.settings = settings
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_result: dict | None = None

    def _destination(self) -> Path:
        target = self.settings.get("backup_replica_target", "")
        if (not isinstance(target, str) or not 1 <= len(target) <= 1024
                or not target.startswith("/") or "\x00" in target
                or any(ord(ch) < 32 or ord(ch) == 127 for ch in target)
                or any(p in ("..", ".") for p in target.split("/"))):
            raise BackupError("An explicit absolute off-NAS mountpoint is required")
        mount = Path(target)
        if mount.is_symlink() or not mount.is_dir() or not mount.is_mount():
            raise BackupError("Backup replica destination is not an OS mountpoint")
        if not _separate_device(mount, self.paths.base):
            raise BackupError("Backup replica must be on a different filesystem")
        root = mount / "FrameDeck-Metadata-Backups"
        if root.is_symlink():
            raise BackupError("Symlinked backup replica directory is forbidden")
        root.mkdir(mode=0o700, exist_ok=True)
        os.chmod(root, 0o700)
        if root.stat().st_mode & 0o077:
            raise BackupError("Backup target does not enforce private permissions")
        return root

    def _copy_file(self, source: Path, destination: Path) -> None:
        # Source has already passed manifest+integrity verification.
        if source.is_symlink() or not source.is_file():
            raise BackupError("Invalid snapshot source file")
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(str(destination), flags, 0o600)
        with source.open("rb") as src, os.fdopen(fd, "wb") as dst:
            shutil.copyfileobj(src, dst, length=1024 * 1024)
            dst.flush()
            os.fsync(dst.fileno())

    def replicate(self, name: str | None = None) -> dict:
        if self.settings.get("backup_replica_enabled", False) is not True:
            raise BackupError("Off-NAS backup replication is disabled")
        if not self._lock.acquire(blocking=False):
            raise BackupError("Off-NAS backup replication already running")
        pending: Path | None = None
        try:
            snapshots = self.backups.list_snapshots()
            if not snapshots:
                raise BackupError("No completed SQLite backup to replicate")
            if name is None:
                name = snapshots[0]["name"]
            if not isinstance(name, str) or not _NAME.fullmatch(name):
                raise BackupError("Invalid snapshot name")
            if not any(x["name"] == name for x in snapshots):
                raise BackupError("Unknown snapshot name")
            source = self.backups.root / name
            metadata = self.backups.verify_directory(source)
            files = tuple(sorted(metadata["files"])) + ("manifest.json",)
            size = sum((source / item).stat().st_size for item in files)
            if size > _MAX_COPY_BYTES:
                raise BackupError("Backup snapshot exceeds 1 GiB replication limit")

            root = self._destination()
            dest = root / name
            if dest.exists() or dest.is_symlink():
                if dest.is_symlink() or not dest.is_dir():
                    raise BackupError("Replica generation path is unsafe")
                saved = self.backups.verify_directory(dest)
                if saved["files"] != metadata["files"]:
                    raise BackupError("Replica generation differs; refusing overwrite")
                result = {"name": name, "copied": False, "verified": True,
                          "bytes": size}
                self._last_result = result
                return result
            free = os.statvfs(root)
            if free.f_bavail * free.f_frsize < size + 16 * 1024 * 1024:
                raise BackupError("Off-NAS destination lacks free space")

            pending = Path(tempfile.mkdtemp(prefix=".incoming-", dir=root))
            for filename in files:
                self._copy_file(source / filename, pending / filename)
            _sync_directory(pending)
            copied = self.backups.verify_directory(pending)
            if copied["files"] != metadata["files"]:
                raise BackupError("Replica checksum mismatch")
            os.replace(pending, dest)
            pending = None
            _sync_directory(root)
            result = {"name": name, "copied": True, "verified": True,
                      "bytes": size}
            self._last_result = result
            return result
        except (OSError, ValueError) as exc:
            raise BackupError(f"Off-NAS backup failed: {type(exc).__name__}") from exc
        finally:
            if pending is not None:
                shutil.rmtree(pending, ignore_errors=True)
            self._lock.release()

    def status(self) -> dict:
        # No mount probe in this frequently polled API: a dead NFS server
        # could otherwise hang FrameDeck's settings page.
        return {
            "enabled": self.settings.get("backup_replica_enabled", False) is True,
            "target_configured": bool(self.settings.get("backup_replica_target", "")),
            "last_result": self._last_result,
        }

    def start(self) -> None:
        if self._thread is not None:
            return
        def worker() -> None:
            # Run only when explicitly enabled and the paired Linux worker
            # is online. The first check is delayed to avoid startup I/O.
            while not self._stop.wait(1800):
                if self.settings.get("backup_replica_enabled", False) is not True:
                    continue
                if self.cluster.verified_peer() is None:
                    continue
                try:
                    # One generation per interval: avoid heavy NAS I/O spikes.
                    for snapshot in self.backups.list_snapshots():
                        # Checking a completed target is read-only, but the
                        # replication itself is already fully atomic.
                        result = self.replicate(snapshot["name"])
                        if result["copied"]:
                            break
                except BackupError as exc:
                    logger.warning("Off-NAS backup replication skipped: %s", exc)
                except Exception:
                    logger.exception("Unexpected off-NAS backup error")
        self._thread = threading.Thread(
            target=worker, daemon=True, name="framedeck-offnas-replica",
        )
        self._thread.start()

    def shutdown(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2)
