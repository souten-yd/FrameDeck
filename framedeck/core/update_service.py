"""GitHub Releases based self-update support.

Release discovery/download is shared across platforms. Installation is kept
platform-specific so QNAP can upgrade the QPKG while a normal Ubuntu checkout
can atomically replace the application sources and restart itself.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform as platform_module
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .. import __version__
from ..config import AppPaths

REPOSITORY = "souten-yd/FrameDeck"
LATEST_RELEASE_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
_USER_AGENT = f"FrameDeck/{__version__} (+https://github.com/{REPOSITORY})"
_MAX_QPKG_BYTES = 768 * 1024 * 1024
_MAX_SOURCE_BYTES = 128 * 1024 * 1024
_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$")


class UpdateError(RuntimeError):
    """A user-facing update error."""


@dataclass(frozen=True)
class PlatformInfo:
    kind: str
    arch: str
    label: str
    install_supported: bool
    reason: str | None = None


def _normalize_arch(value: str | None = None) -> str:
    machine = (value or platform_module.machine() or "unknown").lower()
    aliases = {
        "amd64": "x86_64",
        "x64": "x86_64",
        "arm64": "aarch64",
    }
    return aliases.get(machine, machine)


def detect_platform() -> PlatformInfo:
    """Return the installation platform used by the updater."""
    arch = _normalize_arch()
    qnap = bool(os.environ.get("QNAP_QPKG")) or (
        Path("/etc/config/qpkg.conf").exists() and Path("/sbin/getcfg").exists()
    )
    if qnap:
        root_ok = not hasattr(os, "geteuid") or os.geteuid() == 0
        reason = None if root_ok else "QPKG更新には管理者権限が必要です"
        return PlatformInfo("qnap", arch, f"QNAP QTS ({arch})", root_ok, reason)

    if sys.platform.startswith("linux"):
        source_root = Path(__file__).resolve().parents[2]
        launcher = source_root / "FrameDeck.py"
        writable = launcher.exists() and os.access(source_root, os.W_OK)
        reason = None
        if not launcher.exists():
            reason = "Ubuntuソース配置として認識できません"
        elif not writable:
            reason = "FrameDeckの配置先に書き込み権限がありません"
        return PlatformInfo("ubuntu", arch, f"Ubuntu/Linux ({arch})", writable, reason)

    return PlatformInfo(
        "unsupported", arch, f"{platform_module.system() or sys.platform} ({arch})",
        False, "このプラットフォームの自動更新は未対応です",
    )


def _version_tuple(value: str) -> tuple[int, int, int]:
    match = _VERSION_RE.match((value or "").strip())
    if not match:
        raise UpdateError(f"解釈できないバージョンです: {value!r}")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def _clean_version(value: str) -> str:
    _version_tuple(value)
    return value.strip().removeprefix("v")


def is_newer_version(candidate: str, current: str = __version__) -> bool:
    return _version_tuple(candidate) > _version_tuple(current)


def _request_json(url: str, timeout: int = 20) -> dict[str, Any]:
    req = Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": _USER_AGENT,
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urlopen(req, timeout=timeout) as response:
            return json.load(response)
    except HTTPError as exc:
        raise UpdateError(f"GitHub APIエラー: HTTP {exc.code}") from exc
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        raise UpdateError(f"GitHub Releasesへ接続できません: {exc}") from exc


def _select_qnap_asset(release: dict[str, Any], arch: str) -> dict[str, Any] | None:
    assets = [
        asset for asset in release.get("assets", [])
        if str(asset.get("name", "")).lower().endswith(".qpkg")
    ]
    if not assets:
        return None
    arch_tokens = {arch.lower()}
    if arch == "x86_64":
        arch_tokens.update({"amd64", "x64"})

    compatible = [
        asset for asset in assets
        if any(token in str(asset.get("name", "")).lower() for token in arch_tokens)
    ]
    if not compatible:
        return None

    def score(asset: dict[str, Any]) -> tuple[int, int]:
        name = str(asset.get("name", "")).lower()
        return (1 if "ts-253be" in name else 0, 1 if "framedeck" in name else 0)

    return max(compatible, key=score)


def _release_target(release: dict[str, Any], info: PlatformInfo) -> dict[str, Any] | None:
    if info.kind == "qnap":
        asset = _select_qnap_asset(release, info.arch)
        if not asset:
            return None
        return {
            "kind": "qpkg",
            "name": asset.get("name"),
            "url": asset.get("browser_download_url"),
            "size": int(asset.get("size") or 0),
            "digest": asset.get("digest"),
        }
    if info.kind == "ubuntu":
        url = release.get("tarball_url")
        if not url:
            return None
        return {
            "kind": "source",
            "name": f"FrameDeck-{release.get('tag_name', 'release')}-source.tar.gz",
            "url": url,
            "size": 0,
            "digest": None,
        }
    return None


def _safe_filename(value: str) -> str:
    name = Path(value).name
    if not name or name in {".", ".."}:
        raise UpdateError("不正な更新ファイル名です")
    return re.sub(r"[^A-Za-z0-9._+-]", "_", name)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_digest(path: Path, expected: str | None) -> str:
    actual = _sha256(path)
    if expected:
        algorithm, sep, value = str(expected).partition(":")
        if sep and algorithm.lower() == "sha256" and value:
            if actual.lower() != value.lower():
                raise UpdateError("更新ファイルのSHA-256がGitHub Releaseと一致しません")
    return actual


def _safe_extract_tar(archive: Path, destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    base = destination.resolve()
    with tarfile.open(archive, "r:gz") as tf:
        members = tf.getmembers()
        for member in members:
            target = (destination / member.name).resolve()
            if target != base and base not in target.parents:
                raise UpdateError("更新アーカイブに不正なパスが含まれています")
            if member.issym() or member.islnk() or member.isdev():
                raise UpdateError("更新アーカイブに許可されていないリンク/デバイスがあります")
        tf.extractall(destination, members=members)

    roots = [path for path in destination.iterdir() if path.is_dir()]
    candidates = [path for path in roots if (path / "framedeck" / "__init__.py").exists()]
    if len(candidates) != 1:
        raise UpdateError("FrameDeckソースを更新アーカイブから特定できません")
    return candidates[0]


def _source_version(source_root: Path) -> str:
    init_py = source_root / "framedeck" / "__init__.py"
    try:
        content = init_py.read_text(encoding="utf-8")
    except OSError as exc:
        raise UpdateError("更新ソースのバージョンを確認できません") from exc
    match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', content, re.MULTILINE)
    if not match:
        raise UpdateError("更新ソースにバージョン情報がありません")
    return _clean_version(match.group(1))


class UpdateManager:
    def __init__(self, paths: AppPaths):
        self.paths = paths
        self.update_dir = paths.runtime_dir / "updates"
        self.state_file = self.update_dir / "state.json"
        self.qnap_result_file = self.update_dir / "qnap-install-result.json"
        self.log_file = paths.log_dir / "update.log"
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._last_release: dict[str, Any] | None = None
        self._last_target: dict[str, Any] | None = None

    def _platform_dict(self, info: PlatformInfo | None = None) -> dict[str, Any]:
        value = info or detect_platform()
        return {
            "kind": value.kind,
            "arch": value.arch,
            "label": value.label,
            "install_supported": value.install_supported,
            "reason": value.reason,
        }

    def _set_state(self, status: str, **extra: Any) -> dict[str, Any]:
        payload = {
            "status": status,
            "current_version": __version__,
            "platform": self._platform_dict(),
            "updated_at": time.time(),
            **extra,
        }
        _atomic_json(self.state_file, payload)
        return payload

    def job_status(self) -> dict[str, Any]:
        qnap_result = _load_json(self.qnap_result_file)
        if qnap_result:
            try:
                self.qnap_result_file.unlink()
            except OSError:
                pass
            if qnap_result.get("ok"):
                return self._set_state(
                    "completed",
                    target_version=qnap_result.get("target_version"),
                    message="QPKG更新が完了しました",
                )
            return self._set_state(
                "failed",
                target_version=qnap_result.get("target_version"),
                message=qnap_result.get("message") or "QPKG更新に失敗しました",
            )

        state = _load_json(self.state_file)
        if state:
            state["current_version"] = __version__
            state["platform"] = self._platform_dict()
            return state
        return {
            "status": "idle",
            "current_version": __version__,
            "platform": self._platform_dict(),
        }

    def check(self) -> dict[str, Any]:
        info = detect_platform()
        release = _request_json(LATEST_RELEASE_URL)
        if release.get("draft") or release.get("prerelease"):
            raise UpdateError("安定版Releaseを取得できませんでした")
        tag = str(release.get("tag_name") or "")
        latest_version = _clean_version(tag)
        available = is_newer_version(latest_version, __version__)
        target = _release_target(release, info)
        can_update = bool(available and info.install_supported and target)
        reason = info.reason
        if available and info.install_supported and target is None:
            if info.kind == "qnap":
                reason = f"{info.arch}向けQPKG assetがReleaseにありません"
            else:
                reason = "このReleaseに対応する更新ファイルがありません"

        self._last_release = release
        self._last_target = target
        result = {
            "current_version": __version__,
            "latest_version": latest_version,
            "update_available": available,
            "can_update": can_update,
            "reason": reason,
            "platform": self._platform_dict(info),
            "release": {
                "name": release.get("name") or tag,
                "tag": tag,
                "url": release.get("html_url"),
                "published_at": release.get("published_at"),
            },
            "asset": None if target is None else {
                "name": target.get("name"),
                "size": target.get("size", 0),
                "digest": target.get("digest"),
            },
        }
        self._set_state(
            "checked",
            target_version=latest_version,
            update_available=available,
            can_update=can_update,
            message=("更新があります" if available else "最新バージョンです"),
        )
        return result

    def start_update(self) -> dict[str, Any]:
        with self._lock:
            if self._thread and self._thread.is_alive():
                raise UpdateError("更新処理はすでに実行中です")
            check = self.check()
            if not check["update_available"]:
                raise UpdateError("利用可能な更新はありません")
            if not check["can_update"] or not self._last_release or not self._last_target:
                raise UpdateError(check.get("reason") or "この環境では自動更新できません")
            target_version = str(check["latest_version"])
            release = dict(self._last_release)
            target = dict(self._last_target)
            queued = self._set_state(
                "queued",
                target_version=target_version,
                message="更新を開始しました",
                progress=0,
            )
            self._thread = threading.Thread(
                target=self._worker,
                args=(release, target, target_version),
                name="framedeck-updater",
                daemon=True,
            )
            self._thread.start()
            return queued

    def _worker(self, release: dict[str, Any], target: dict[str, Any], target_version: str) -> None:
        try:
            self.update_dir.mkdir(parents=True, exist_ok=True)
            name = _safe_filename(str(target.get("name") or "FrameDeck-update"))
            destination = self.update_dir / name
            self._download(target, destination, target_version)
            digest = _validate_digest(destination, target.get("digest"))
            self._set_state(
                "verified",
                target_version=target_version,
                message="更新ファイルを検証しました",
                progress=100,
                sha256=digest,
            )
            info = detect_platform()
            if info.kind == "qnap":
                self._apply_qnap(destination, target_version)
            elif info.kind == "ubuntu":
                self._apply_ubuntu(destination, target_version)
            else:
                raise UpdateError("このプラットフォームのインストーラはありません")
        except Exception as exc:  # worker must always persist a diagnosable result
            message = str(exc) if isinstance(exc, UpdateError) else f"更新処理に失敗しました: {exc}"
            self._set_state("failed", target_version=target_version, message=message)

    def _download(self, target: dict[str, Any], destination: Path, target_version: str) -> None:
        url = str(target.get("url") or "")
        if not url.startswith("https://"):
            raise UpdateError("更新URLがHTTPSではありません")
        limit = _MAX_QPKG_BYTES if target.get("kind") == "qpkg" else _MAX_SOURCE_BYTES
        expected_size = int(target.get("size") or 0)
        if expected_size and expected_size > limit:
            raise UpdateError("更新ファイルが許容サイズを超えています")

        part = destination.with_suffix(destination.suffix + ".part")
        try:
            part.unlink()
        except OSError:
            pass
        req = Request(url, headers={"User-Agent": _USER_AGENT, "Accept": "application/octet-stream"})
        try:
            with urlopen(req, timeout=45) as response, part.open("wb") as out:
                header_size = int(response.headers.get("Content-Length") or 0)
                total = expected_size or header_size
                if total and total > limit:
                    raise UpdateError("更新ファイルが許容サイズを超えています")
                downloaded = 0
                last_bucket = -1
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    downloaded += len(chunk)
                    if downloaded > limit:
                        raise UpdateError("更新ファイルが許容サイズを超えています")
                    out.write(chunk)
                    progress = int(downloaded * 100 / total) if total else 0
                    bucket = progress // 5
                    if bucket != last_bucket:
                        last_bucket = bucket
                        self._set_state(
                            "downloading",
                            target_version=target_version,
                            message="更新ファイルをダウンロード中",
                            progress=min(progress, 99),
                            downloaded_bytes=downloaded,
                            total_bytes=total or None,
                        )
        except HTTPError as exc:
            raise UpdateError(f"更新ファイルのダウンロードに失敗しました: HTTP {exc.code}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise UpdateError(f"更新ファイルのダウンロードに失敗しました: {exc}") from exc
        if expected_size and part.stat().st_size != expected_size:
            raise UpdateError("更新ファイルのサイズがGitHub Releaseと一致しません")
        os.replace(part, destination)

    def _apply_qnap(self, qpkg: Path, target_version: str) -> None:
        if hasattr(os, "geteuid") and os.geteuid() != 0:
            raise UpdateError("QPKG更新には管理者権限が必要です")
        self.paths.log_dir.mkdir(parents=True, exist_ok=True)
        script = self.update_dir / "apply-qnap-update.sh"
        result = self.qnap_result_file
        success_json = json.dumps({
            "ok": True,
            "target_version": target_version,
            "message": "QPKG update completed",
        }, ensure_ascii=False)
        failed_json = json.dumps({
            "ok": False,
            "target_version": target_version,
            "message": "QPKG installer returned a non-zero status",
        }, ensure_ascii=False)
        script.write_text(
            "#!/bin/sh\n"
            "set +e\n"
            "sleep 1\n"
            f"echo '[FrameDeck] installing {shlex.quote(str(qpkg))}' >> {shlex.quote(str(self.log_file))}\n"
            f"/bin/sh {shlex.quote(str(qpkg))} >> {shlex.quote(str(self.log_file))} 2>&1\n"
            "RC=$?\n"
            "if [ \"$RC\" -eq 0 ]; then\n"
            f"  printf '%s\\n' {shlex.quote(success_json)} > {shlex.quote(str(result))}.tmp\n"
            "else\n"
            f"  printf '%s\\n' {shlex.quote(failed_json)} > {shlex.quote(str(result))}.tmp\n"
            "fi\n"
            f"mv {shlex.quote(str(result))}.tmp {shlex.quote(str(result))}\n"
            "exit $RC\n",
            encoding="utf-8",
        )
        script.chmod(0o700)
        self._set_state(
            "installing",
            target_version=target_version,
            message="QPKGをインストールしています。FrameDeckは再起動します",
            progress=100,
        )
        subprocess.Popen(
            ["/bin/sh", str(script)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )

    def _apply_ubuntu(self, archive: Path, target_version: str) -> None:
        install_root = Path(__file__).resolve().parents[2]
        launcher = install_root / "FrameDeck.py"
        if not launcher.exists() or not os.access(install_root, os.W_OK):
            raise UpdateError("UbuntuのFrameDeck配置先へ書き込めません")

        extracted = self.update_dir / f"source-{target_version}"
        shutil.rmtree(extracted, ignore_errors=True)
        source_root = _safe_extract_tar(archive, extracted)
        source_version = _source_version(source_root)
        if source_version != target_version:
            raise UpdateError(
                f"更新ソースのバージョン({source_version})がRelease({target_version})と一致しません"
            )
        if not (source_root / "FrameDeck.py").exists():
            raise UpdateError("更新ソースにFrameDeck.pyがありません")

        stamp = time.strftime("%Y%m%d-%H%M%S")
        stage = install_root / f".framedeck-update-{target_version}-{os.getpid()}"
        backup = install_root / f".framedeck-backup-{__version__}-{stamp}"
        shutil.rmtree(stage, ignore_errors=True)
        stage.mkdir(parents=True)
        shutil.copytree(
            source_root / "framedeck",
            stage / "framedeck",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        shutil.copy2(source_root / "FrameDeck.py", stage / "FrameDeck.py")
        backup.mkdir(parents=True)

        old_package = install_root / "framedeck"
        swapped = False
        try:
            os.replace(old_package, backup / "framedeck")
            os.replace(stage / "framedeck", old_package)
            swapped = True
            shutil.copy2(launcher, backup / "FrameDeck.py")
            os.replace(stage / "FrameDeck.py", launcher)
        except Exception:
            if swapped:
                try:
                    shutil.rmtree(old_package, ignore_errors=True)
                    os.replace(backup / "framedeck", old_package)
                    if (backup / "FrameDeck.py").exists():
                        os.replace(backup / "FrameDeck.py", launcher)
                except Exception:
                    pass
            raise
        finally:
            shutil.rmtree(stage, ignore_errors=True)

        self._set_state(
            "restarting",
            target_version=target_version,
            message="更新を適用しました。FrameDeckを再起動しています",
            progress=100,
            backup=str(backup),
        )
        time.sleep(1.0)
        env = os.environ.copy()
        argv0 = Path(sys.argv[0]).name.lower() if sys.argv else ""
        if argv0 == "framedeck.py":
            argv = [sys.executable, str(launcher), *sys.argv[1:]]
        else:
            env.setdefault("FRAMEDECK_HOME", str(self.paths.base))
            argv = [sys.executable, "-m", "framedeck"]
        os.execvpe(sys.executable, argv, env)


_MANAGERS: dict[str, UpdateManager] = {}
_MANAGERS_LOCK = threading.Lock()


def get_update_manager(paths: AppPaths) -> UpdateManager:
    key = str(paths.base.resolve())
    with _MANAGERS_LOCK:
        manager = _MANAGERS.get(key)
        if manager is None:
            manager = UpdateManager(paths)
            _MANAGERS[key] = manager
        return manager
