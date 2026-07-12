"""アーカイブ読み取りバックエンド(ZIP / RAR)。

RAR対応の優先順位: bsdtar → rarfile(unrar/unar) → 7z。
すべての外部コマンドにタイムアウトを設定し、アーカイブ内エントリ名は
Zip Slip対策の検証を通す。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import zipfile

from ..config import (
    IMAGE_EXTENSIONS,
    SUBPROCESS_TIMEOUT_LIST,
    SUBPROCESS_TIMEOUT_READ,
)
from ..core.rating_service import natural_key
from ..core.security import is_safe_member_name


class ArchiveError(Exception):
    pass


def rar_backend_available() -> str | None:
    """利用可能なRARバックエンド名を返す(なければNone)。"""
    if shutil.which("bsdtar"):
        return "bsdtar"
    try:
        import rarfile
        tool = getattr(rarfile, "UNRAR_TOOL", "unrar")
        if shutil.which(tool) or shutil.which("unrar") or shutil.which("unar"):
            return "rarfile"
    except ImportError:
        pass
    if shutil.which("7z"):
        return "7z"
    return None


class ArchiveReader:
    """ZIP/CBZ/RAR/CBRの一覧・読み出し。"""

    def __init__(self, path: str):
        self.path = path
        self.ext = os.path.splitext(path)[1].lower()
        self._zip: zipfile.ZipFile | None = None
        self._rar = None
        self._backend: str | None = None

    def open(self) -> "ArchiveReader":
        if self.ext in (".zip", ".cbz"):
            try:
                self._zip = zipfile.ZipFile(self.path)
            except (zipfile.BadZipFile, OSError) as e:
                raise ArchiveError(f"ZIPを開けませんでした: {e}") from e
            self._backend = "zip"
            return self

        if self.ext in (".rar", ".cbr"):
            backend = rar_backend_available()
            if backend is None:
                raise ArchiveError(
                    "RARを読むには bsdtar、7z、unrar、または unar が必要です。\n"
                    "例: sudo apt install libarchive-tools もしくは sudo apt install unrar"
                )
            self._backend = backend
            if backend == "rarfile":
                import rarfile
                try:
                    self._rar = rarfile.RarFile(self.path)
                    if self._rar.needs_password():
                        raise ArchiveError(
                            "パスワード付きアーカイブには対応していません。"
                        )
                except ArchiveError:
                    raise
                except Exception as e:
                    raise ArchiveError(f"RARを開けませんでした: {e}") from e
            return self

        raise ArchiveError(f"対応していない漫画ファイルです: {self.path}")

    def _run(self, cmd: list[str], timeout: int) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                cmd, check=True, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, timeout=timeout,
            )
        except subprocess.TimeoutExpired as e:
            raise ArchiveError(f"アーカイブ操作がタイムアウトしました: {cmd[0]}") from e
        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or b"").decode("utf-8", "replace")[:400]
            if "password" in stderr.lower() or "encrypted" in stderr.lower():
                raise ArchiveError("パスワード付きアーカイブには対応していません。") from e
            raise ArchiveError(f"アーカイブ操作に失敗しました: {stderr}") from e
        except FileNotFoundError as e:
            raise ArchiveError(f"コマンドが見つかりません: {cmd[0]}") from e

    def list_names(self) -> list[str]:
        if self._backend == "zip":
            names = self._zip.namelist()
        elif self._backend == "rarfile":
            names = self._rar.namelist()
        elif self._backend == "bsdtar":
            result = self._run(
                ["bsdtar", "-tf", self.path], SUBPROCESS_TIMEOUT_LIST
            )
            names = result.stdout.decode("utf-8", "replace").splitlines()
        elif self._backend == "7z":
            result = self._run(
                ["7z", "l", "-slt", self.path], SUBPROCESS_TIMEOUT_LIST
            )
            names = [
                line.split(" = ", 1)[1]
                for line in result.stdout.decode("utf-8", "replace").splitlines()
                if line.startswith("Path = ")
            ]
        else:
            raise ArchiveError("アーカイブが開かれていません。")
        return [n for n in names if is_safe_member_name(n)]

    def list_images(self) -> list[str]:
        images = []
        for name in self.list_names():
            base = os.path.basename(name.replace("\\", "/"))
            if not base:
                continue
            if os.path.splitext(base)[1].lower() in IMAGE_EXTENSIONS:
                images.append(name)
        images.sort(key=natural_key)
        return images

    def read(self, name: str) -> bytes:
        if not is_safe_member_name(name):
            raise ArchiveError(f"不正なアーカイブ内パスです: {name}")
        if self._backend == "zip":
            try:
                return self._zip.read(name)
            except (KeyError, zipfile.BadZipFile, OSError) as e:
                raise ArchiveError(f"ページを読み出せませんでした: {e}") from e
        if self._backend == "rarfile":
            try:
                with self._rar.open(name) as f:
                    return f.read()
            except Exception as e:
                raise ArchiveError(f"ページを読み出せませんでした: {e}") from e
        if self._backend == "bsdtar":
            result = self._run(
                ["bsdtar", "-xOf", self.path, name], SUBPROCESS_TIMEOUT_READ
            )
            return result.stdout
        if self._backend == "7z":
            result = self._run(
                ["7z", "x", "-so", self.path, name], SUBPROCESS_TIMEOUT_READ
            )
            return result.stdout
        raise ArchiveError("アーカイブが開かれていません。")

    def close(self) -> None:
        if self._zip:
            self._zip.close()
            self._zip = None
        if self._rar:
            try:
                self._rar.close()
            except Exception:
                pass
            self._rar = None

    def __enter__(self) -> "ArchiveReader":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()
