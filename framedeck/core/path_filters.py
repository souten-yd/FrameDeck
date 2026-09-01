"""FrameDeckのファイルシステム走査で共有する除外規則。"""
from __future__ import annotations

DOCEXTRACTOR_INTERNAL_PREFIX = ".docExtractor-"


def is_internal_directory_name(name: str) -> bool:
    """漫画・動画UIや読書シーケンスに出さない内部ディレクトリか判定する。"""
    return name.casefold().startswith(DOCEXTRACTOR_INTERNAL_PREFIX.casefold())
