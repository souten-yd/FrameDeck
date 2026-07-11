"""HTTP Range Request対応のファイルストリーミング。

- Accept-Ranges: bytes / 206 Partial Content / Content-Range / HEAD対応
- 動画全体をメモリへ読み込まない(チャンク読み)
- クライアント切断はStreamingResponse側で安全に打ち切られる
"""
from __future__ import annotations

import os
import re
from typing import Iterator

CHUNK_SIZE = 1024 * 256

_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)$")


class RangeNotSatisfiable(Exception):
    pass


def parse_range_header(header: str | None,
                       file_size: int) -> tuple[int, int] | None:
    """Rangeヘッダーを (start, end) の閉区間として解釈する。

    Rangeなし・複数レンジは None(全体送信)。不正なら RangeNotSatisfiable。
    """
    if not header:
        return None
    header = header.strip()
    if "," in header:
        # 複数レンジは非対応 → 全体送信で応答する
        return None
    match = _RANGE_RE.match(header)
    if not match:
        return None
    start_s, end_s = match.groups()
    if start_s == "" and end_s == "":
        raise RangeNotSatisfiable(header)
    if start_s == "":
        # suffix range: 末尾 N バイト
        length = int(end_s)
        if length == 0:
            raise RangeNotSatisfiable(header)
        start = max(0, file_size - length)
        end = file_size - 1
    else:
        start = int(start_s)
        end = int(end_s) if end_s else file_size - 1
        if start >= file_size or end < start:
            raise RangeNotSatisfiable(header)
        end = min(end, file_size - 1)
    return start, end


def iter_file_range(path: str, start: int, end: int,
                    chunk_size: int = CHUNK_SIZE) -> Iterator[bytes]:
    remaining = end - start + 1
    with open(path, "rb") as f:
        f.seek(start)
        while remaining > 0:
            chunk = f.read(min(chunk_size, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


VIDEO_MIME = {
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".webm": "video/webm",
    ".mkv": "video/x-matroska", ".avi": "video/x-msvideo",
    ".mov": "video/quicktime", ".wmv": "video/x-ms-wmv",
    ".flv": "video/x-flv", ".mpg": "video/mpeg", ".mpeg": "video/mpeg",
    ".ts": "video/mp2t",
}


def mime_for_video(path: str) -> str:
    return VIDEO_MIME.get(os.path.splitext(path)[1].lower(),
                          "application/octet-stream")
