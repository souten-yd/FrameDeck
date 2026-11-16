"""Crash-resistant online SQLite and settings backups with bounded generations.

A backup is written to a private temporary directory, verified, fsynced and
renamed into place. Live SQLite/WAL files are never copied as raw bytes.
There is deliberately no in-process restore or delete API.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from ..config import AppPaths, Settings

logger = logging.getLogger("framedeck")
_NAME = re.compile(r"^\d{8}T\d{6}-[0-9a-f]{8}$")
DAILY_GENERATIONS = 7
WEEKLY_GENERATIONS = 4


class BackupError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _sync_directory(path: Path) -> None:
    if not hasattr(os, "O_DIRECTORY"):
        return
    try:
        fd = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        logger.warning("Backup directory fsync unsupported")


def _check_database(path: Path) -> None:
    # Read-only verification of a completed snapshot; never open a backup
    # writable or let an unrelated WAL file influence its integrity result.
    uri = "file:" + quote(str(path.resolve()), safe="/") + "?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True, timeout=5)) as conn:
            rows = conn.execute("PRAGMA integrity_check").fetchall()
    except sqlite3.Error as exc:
        raise BackupError("SQLite backup cannot be read or verified") from exc
    if rows != [("ok",)]:
        raise BackupError("SQLite backup failed integrity_check")


class BackupService:
    def __init__(self, paths: AppPaths, storage=None):
        self.paths = paths
        self._storage = storage
        self.root = paths.base / "backups"
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _ensure_private_root(self) -> None:
        if self.root.is_symlink():
            raise BackupError("Backup root must not be a symlink")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)

    def _copy_private(self, source: Path, destination: Path) -> None:
        if source.is_symlink() or not source.is_file():
            raise BackupError("Backup source is not a regular file")
        if destination.exists():
            raise BackupError("Backup destination already exists")
        with source.open("rb") as src:
            fd = os.open(str(destination), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as dst:
                shutil.copyfileobj(src, dst, length=1024 * 1024)
                dst.flush()
                os.fsync(dst.fileno())

    def _online_database_copy(self, destination: Path) -> None:
        db = self.paths.database_file
        if db.is_symlink() or not db.is_file():
            raise BackupError("FrameDeck database is unavailable")
        uri = "file:" + quote(str(db.resolve()), safe="/") + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=10)) as src:
            with closing(sqlite3.connect(str(destination), timeout=10)) as dst:
                src.backup(dst, pages=64, sleep=0.05)
                dst.commit()
        os.chmod(destination, 0o600)
        _check_database(destination)
        _sync_file(destination)

    def _make_metadata(self, tmp: Path) -> dict:
        now = datetime.now(timezone.utc)
        local = now.astimezone()
        files = {
            file.name: {"sha256": _sha256(file), "size": file.stat().st_size}
            for file in sorted(tmp.iterdir())
            if file.is_file() and file.name != "manifest.json"
        }
        return {
            "format_version": 1,
            "created_at": now.isoformat(timespec="seconds"),
            "local_date": local.date().isoformat(),
            "iso_week": f"{local.isocalendar().year}-W{local.isocalendar().week:02d}",
            "files": files,
        }

    def create_snapshot(self) -> dict:
        if not self._lock.acquire(blocking=False):
            raise BackupError("Another backup is already running")
        temp: Path | None = None
        try:
            # Include recent reading positions still buffered in memory.
            if self._storage is not None:
                self._storage.flush_reading_progress()
            self._ensure_private_root()
            if not self.paths.settings_file.is_file():
                raise BackupError("FrameDeck settings are unavailable")
            db = self.paths.database_file
            if not db.is_file():
                raise BackupError("FrameDeck database is unavailable")
            # Avoid filling the NAS volume with a failed half-copy.
            wal = Path(str(db) + "-wal")
            expected = db.stat().st_size + (wal.stat().st_size if wal.exists() else 0)
            free = os.statvfs(self.root)
            free_bytes = free.f_bavail * free.f_frsize
            if free_bytes < max(16 * 1024**2, 2 * expected + 4 * 1024**2):
                raise BackupError("Not enough free space for a safe online backup")

            temp = Path(tempfile.mkdtemp(prefix=".incomplete-", dir=self.root))
            self._online_database_copy(temp / "framedeck.db")
            self._copy_private(self.paths.settings_file, temp / "settings.json")
            peer_config = self.paths.config_dir / "cluster-worker.json"
            if peer_config.exists():
                self._copy_private(peer_config, temp / "cluster-worker.json")

            metadata = self._make_metadata(temp)
            name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            name += "-" + os.urandom(4).hex()
            manifest = temp / "manifest.json"
            fd = os.open(str(manifest), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(json.dumps(metadata, ensure_ascii=False,
                                        sort_keys=True, indent=2).encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
            _sync_directory(temp)
            self.verify_directory(temp)
            final = self.root / name
            os.replace(temp, final)
            temp = None
            _sync_directory(self.root)
            self.prune()
            return {"name": name, "created_at": metadata["created_at"],
                    "local_date": metadata["local_date"],
                    "size_bytes": sum(x["size"] for x in metadata["files"].values())}
        except (OSError, sqlite3.Error, ValueError) as exc:
            raise BackupError(f"Backup could not complete: {type(exc).__name__}") from exc
        finally:
            if temp is not None and temp.exists():
                shutil.rmtree(temp, ignore_errors=True)
            self._lock.release()

    def verify_directory(self, folder: Path) -> dict:
        if folder.is_symlink() or not folder.is_dir():
            raise BackupError("Invalid backup directory")
        manifest = folder / "manifest.json"
        if manifest.is_symlink() or not manifest.is_file() or manifest.stat().st_size > 16384:
            raise BackupError("Invalid backup manifest")
        try:
            metadata = json.loads(manifest.read_text("utf-8"))
        except (ValueError, UnicodeError) as exc:
            raise BackupError("Unreadable backup manifest") from exc
        if not isinstance(metadata, dict) or metadata.get("format_version") != 1:
            raise BackupError("Unsupported backup format")
        files = metadata.get("files")
        if (not isinstance(files, dict) or
                set(files) not in (
                    {"framedeck.db", "settings.json"},
                    {"framedeck.db", "settings.json", "cluster-worker.json"},
                )):
            raise BackupError("Incomplete backup manifest")
        for name, file_info in files.items():
            if (not isinstance(file_info, dict) or
                    not isinstance(file_info.get("size"), int) or
                    not isinstance(file_info.get("sha256"), str) or
                    not re.fullmatch(r"[0-9a-f]{64}", file_info["sha256"])):
                raise BackupError("Invalid file entry in backup manifest")
            path = folder / name
            if path.is_symlink() or not path.is_file():
                raise BackupError("Missing backup file")
            if path.stat().st_size != file_info["size"]:
                raise BackupError("Backup file size mismatch")
            if _sha256(path) != file_info["sha256"]:
                raise BackupError("Backup checksum mismatch")
        _check_database(folder / "framedeck.db")
        return metadata

    def verify(self, name: str) -> dict:
        if not _NAME.fullmatch(name):
            raise BackupError("Invalid backup identifier")
        if not self.root.is_dir() or self.root.is_symlink():
            raise BackupError("No backup directory")
        metadata = self.verify_directory(self.root / name)
        return {"name": name, "verified": True,
                "created_at": metadata["created_at"]}

    def list_snapshots(self) -> list[dict]:
        if not self.root.is_dir() or self.root.is_symlink():
            return []
        snapshots = []
        for folder in self.root.iterdir():
            if not _NAME.fullmatch(folder.name) or folder.is_symlink() or not folder.is_dir():
                continue
            try:
                manifest = folder / "manifest.json"
                if manifest.is_symlink() or manifest.stat().st_size > 16384:
                    continue
                metadata = json.loads(manifest.read_text("utf-8"))
                if not isinstance(metadata, dict) or metadata.get("format_version") != 1:
                    continue
                files = metadata.get("files", {})
                if (not isinstance(files, dict) or
                        not isinstance(metadata.get("local_date"), str) or
                        not re.fullmatch(r"\d{4}-\d{2}-\d{2}", metadata["local_date"]) or
                        not isinstance(metadata.get("iso_week"), str) or
                        not re.fullmatch(r"\d{4}-W\d{2}", metadata["iso_week"])):
                    continue
                snapshots.append({
                    "name": folder.name,
                    "created_at": metadata["created_at"],
                    "local_date": metadata["local_date"],
                    "iso_week": metadata["iso_week"],
                    "size_bytes": sum(int(v["size"]) for v in files.values()),
                })
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return sorted(snapshots, key=lambda x: x["name"], reverse=True)

    def prune(self) -> None:
        """Keep seven daily and four weekly checkpoints, counting overlap once."""
        snapshots = self.list_snapshots()
        keep: set[str] = set()
        dates: set[str] = set()
        weeks: set[str] = set()
        invalid: set[str] = set()
        # IMPORTANT: an intact-looking manifest is NOT proof that its SQLite
        # data is valid. Verify a candidate before it can displace an older
        # good generation. Preserve invalid generations for manual diagnosis.
        for snap in snapshots:
            folder = self.root / snap["name"]
            try:
                self.verify_directory(folder)
            except (BackupError, OSError, ValueError, KeyError, TypeError):
                invalid.add(snap["name"])
                logger.warning("Backup generation %s failed retention verification; preserved",
                               snap["name"])
                continue
            if snap["local_date"] not in dates and len(dates) < DAILY_GENERATIONS:
                dates.add(snap["local_date"])
                keep.add(snap["name"])
            if snap["iso_week"] not in weeks and len(weeks) < WEEKLY_GENERATIONS:
                weeks.add(snap["iso_week"])
                keep.add(snap["name"])
        for snap in snapshots:
            if snap["name"] not in keep and snap["name"] not in invalid:
                folder = self.root / snap["name"]
                if folder.is_dir() and not folder.is_symlink() and _NAME.fullmatch(folder.name):
                    shutil.rmtree(folder)
        _sync_directory(self.root)

    def start(self, settings: Settings) -> None:
        if self._thread is not None:
            return
        def worker() -> None:
            last_auto_attempt: str | None = None
            while not self._stop.is_set():
                if settings.get("backup_enabled", False) is True:
                    now = datetime.now().astimezone()
                    day = now.date().isoformat()
                    if now.hour >= 3 and last_auto_attempt != day:
                        latest = self.list_snapshots()
                        if not any(x["local_date"] == day for x in latest):
                            last_auto_attempt = day  # one attempt/day, even if storage is full
                            try:
                                self.create_snapshot()
                            except BackupError as exc:
                                logger.warning("Daily FrameDeck backup skipped: %s", exc)
                            except Exception:
                                logger.exception("Unexpected backup failure")
                self._stop.wait(600)
        self._thread = threading.Thread(
            target=worker, name="framedeck-daily-backup", daemon=True,
        )
        self._thread.start()

    def shutdown(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2)
