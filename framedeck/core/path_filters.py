"""FrameDeckのファイルシステム走査で共有する除外規則。"""
from __future__ import annotations

DOCEXTRACTOR_INTERNAL_PREFIX = ".docExtractor-"


def is_internal_directory_name(name: str) -> bool:
    """UIや読書シーケンスに出さない内部管理ディレクトリかを判定する。"""
    return name.startswith(DOCEXTRACTOR_INTERNAL_PREFIX)
