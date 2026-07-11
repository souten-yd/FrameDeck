"""パス検証・削除確認トークンなどのセキュリティユーティリティ。"""
from __future__ import annotations

import secrets
import threading
import time
from pathlib import Path


class PathValidationError(Exception):
    pass


def is_safe_member_name(name: str) -> bool:
    """アーカイブ内エントリ名の安全性検査(Zip Slip対策)。"""
    if not name or "\x00" in name:
        return False
    # Windowsドライブ指定 / UNC
    if len(name) >= 2 and name[1] == ":":
        return False
    normalized = name.replace("\\", "/")
    if normalized.startswith("/") or normalized.startswith("//"):
        return False
    parts = [p for p in normalized.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        return False
    return True


def resolve_within_roots(requested_path: str, roots: list[str]) -> Path:
    """登録済みライブラリルート配下のパスであることを検証して返す。

    シンボリックリンクによるルート外脱出も resolve() により拒否する。
    """
    resolved = Path(requested_path).resolve()
    for root in roots:
        try:
            root_resolved = Path(root).resolve()
        except OSError:
            continue
        try:
            if resolved.is_relative_to(root_resolved):
                return resolved
        except ValueError:
            continue
    raise PathValidationError(
        f"登録済みライブラリルートの外へのアクセスは拒否されました: {requested_path}"
    )


class ConfirmTokenBox:
    """削除などの破壊的操作に使う期限付き確認トークン。"""

    def __init__(self, ttl_seconds: float = 60.0):
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._tokens: dict[str, tuple[str, float]] = {}

    def issue(self, subject: str) -> str:
        token = secrets.token_urlsafe(24)
        with self._lock:
            self._prune()
            self._tokens[token] = (subject, time.time() + self._ttl)
        return token

    def consume(self, token: str, subject: str) -> bool:
        with self._lock:
            self._prune()
            entry = self._tokens.pop(token, None)
        if entry is None:
            return False
        stored_subject, expires = entry
        return stored_subject == subject and time.time() <= expires

    def _prune(self) -> None:
        now = time.time()
        for key in [k for k, (_, exp) in self._tokens.items() if exp < now]:
            self._tokens.pop(key, None)
