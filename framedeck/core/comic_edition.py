"""漫画の版表記を判定し、表示上だけカラー版を識別する。"""
from __future__ import annotations

import re
import unicodedata


COLOR_MARK = "🌈"

# 実ライブラリで使われている「カラー版」「セミカラー版」等を優先しつつ、
# 英語名の一般的な表記にも対応する。単なる色名や作品名中の ``color`` は
# 誤判定しないよう、英語は edition/version/comic の併記を必要とする。
_COLOR_EDITION = re.compile(
    r"(?:フル|セミ|オール)カラー(?:版|化)?"
    r"|カラー(?:版|化)"
    r"|(?:^|[\s_\-\[\]()（）【】])カラー(?=$|[\s_\-\[\]()（）【】])"
    r"|彩色版"
    r"|\b(?:full[\s_-]+colou?r|colou?r[\s_-]+(?:edition|version|comic))\b",
    re.IGNORECASE,
)


def is_color_edition(name: str) -> bool:
    """ファイル名・エントリ名がカラー版を明示しているか返す。"""
    normalized = unicodedata.normalize("NFKC", name)
    return bool(_COLOR_EDITION.search(normalized))


def mark_color_edition(label: str, *, source_name: str | None = None) -> str:
    """カラー版の表示ラベルに一度だけマークを付ける。"""
    source = source_name if source_name is not None else label
    if not is_color_edition(source) or COLOR_MARK in label:
        return label
    return f"{COLOR_MARK} {label}"
