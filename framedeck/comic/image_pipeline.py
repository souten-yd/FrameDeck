"""画像パイプライン:

  decode → EXIF orientation → resize → encode

- 生データのメモリLRUキャッシュ
- 表示サイズ別のディスクキャッシュ
- ThreadPoolExecutorによる先読み(プリフェッチ)

UIスレッド・イベントループでの同期デコードを避けるための共有部品。
"""
from __future__ import annotations

import hashlib
import io
import os
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from statistics import median

from PIL import Image, ImageFilter, ImageOps

from ..models import ComicEntry, PageRef
from .crop_detector import detect_crop_box
from .image_analysis import ComicImageAnalysis
from .spread_detector import detect_spread

_RESAMPLE = {
    "lanczos": Image.Resampling.LANCZOS,
    "bicubic": Image.Resampling.BICUBIC,
    "bilinear": Image.Resampling.BILINEAR,
    "nearest": Image.Resampling.NEAREST,
}

_MIME_BY_EXT = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".bmp": "image/bmp", ".gif": "image/gif",
}

_COMIC_PROFILES = {
    "high": {"quality": 92, "max_long_edge": 3200, "dpr_limit": 2.0},
    "balanced": {"quality": 84, "max_long_edge": 2400, "dpr_limit": 2.0},
    # mobile: dpr上限1.25では高DPI端末で文字がぼやけるため2.0へ。
    # 転送量は縮小後シャープ化+WebPで抑える。
    "mobile": {"quality": 80, "max_long_edge": 2000, "dpr_limit": 2.0},
    "data_saver": {"quality": 65, "max_long_edge": 1280, "dpr_limit": 1.0},
    "original": {"quality": 95, "max_long_edge": 0, "dpr_limit": 3.0},
}

_FORMATS = {
    "jpeg": ("JPEG", "image/jpeg", ".jpg"),
    "webp": ("WEBP", "image/webp", ".webp"),
    "png": ("PNG", "image/png", ".png"),
    "avif": ("AVIF", "image/avif", ".avif"),
}


class MemoryLRU:
    """バイト数上限つきのスレッドセーフLRU。"""

    def __init__(self, max_bytes: int):
        self._max = max_bytes
        self._lock = threading.Lock()
        self._data: OrderedDict[str, bytes] = OrderedDict()
        self._size = 0

    def get(self, key: str) -> bytes | None:
        with self._lock:
            value = self._data.get(key)
            if value is not None:
                self._data.move_to_end(key)
            return value

    def put(self, key: str, value: bytes) -> None:
        with self._lock:
            old = self._data.pop(key, None)
            if old is not None:
                self._size -= len(old)
            self._data[key] = value
            self._size += len(value)
            while self._size > self._max and self._data:
                _, evicted = self._data.popitem(last=False)
                self._size -= len(evicted)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
            self._size = 0


class ImagePipeline:
    def __init__(self, page_cache_dir: Path, thumb_cache_dir: Path,
                 memory_limit_bytes: int = 512 * 1024**2,
                 resize_filter: str = "lanczos",
                 max_workers: int = 4,
                 variant_cache_dir: Path | None = None,
                 analysis_cache_dir: Path | None = None):
        self._page_cache_dir = Path(page_cache_dir)
        self._thumb_cache_dir = Path(thumb_cache_dir)
        self._variant_cache_dir = Path(variant_cache_dir or page_cache_dir)
        self._analysis_cache_dir = Path(analysis_cache_dir or page_cache_dir)
        self._raw_cache = MemoryLRU(memory_limit_bytes)
        self._size_cache: dict[str, tuple[int, int]] = {}
        self._size_lock = threading.Lock()
        self._analysis_inflight: set[str] = set()
        self._analysis_inflight_lock = threading.Lock()
        self.variant_sharpen = True
        self._read_locks: dict[int, threading.Lock] = {}
        self._read_locks_lock = threading.Lock()
        self.resize_filter = resize_filter
        self.executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="framedeck-img"
        )

    def shutdown(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=True)

    # ---------------- 生データ ----------------

    def _source_lock(self, source) -> threading.Lock:
        key = id(source)
        with self._read_locks_lock:
            lock = self._read_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._read_locks[key] = lock
            return lock

    def release_source(self, source) -> None:
        with self._read_locks_lock:
            self._read_locks.pop(id(source), None)

    @staticmethod
    def _raw_key(entry: ComicEntry, page: PageRef) -> str:
        return f"raw:{entry.id}:{page.index}"

    def get_raw(self, source, entry: ComicEntry, page: PageRef) -> bytes:
        key = self._raw_key(entry, page)
        cached = self._raw_cache.get(key)
        if cached is not None:
            return cached
        with self._source_lock(source):
            cached = self._raw_cache.get(key)
            if cached is not None:
                return cached
            data = source.read_page(page)
        self._raw_cache.put(key, data)
        return data

    def get_page_size(self, source, entry: ComicEntry,
                      page: PageRef) -> tuple[int, int]:
        key = f"{entry.id}:{page.index}"
        with self._size_lock:
            if key in self._size_cache:
                return self._size_cache[key]
        data = self.get_raw(source, entry, page)
        with Image.open(io.BytesIO(data)) as img:
            size = img.size
            try:
                transposed = ImageOps.exif_transpose(img)
                if transposed is not None:
                    size = transposed.size
            except Exception:
                pass
        with self._size_lock:
            self._size_cache[key] = size
        return size

    def is_landscape(self, source, entry: ComicEntry, page: PageRef,
                     threshold: float = 1.25) -> bool:
        try:
            w, h = self.get_page_size(source, entry, page)
        except Exception:
            return False
        return h > 0 and (w / h) >= threshold

    # ---------------- プリフェッチ ----------------

    def prefetch(self, source, entry: ComicEntry, pages: list[PageRef],
                 center: int, ahead: int = 6, behind: int = 2) -> None:
        targets: list[PageRef] = []
        for offset in range(1, ahead + 1):
            idx = center + offset
            if 0 <= idx < len(pages):
                targets.append(pages[idx])
        for offset in range(1, behind + 1):
            idx = center - offset
            if 0 <= idx < len(pages):
                targets.append(pages[idx])
        for page in targets:
            if self._raw_cache.get(self._raw_key(entry, page)) is not None:
                continue
            self.executor.submit(self._prefetch_one, source, entry, page)

    def _prefetch_one(self, source, entry: ComicEntry, page: PageRef) -> None:
        try:
            self.get_raw(source, entry, page)
            self.get_page_size(source, entry, page)
            # 解析(トリミング/見開き)も先回りしてディスクへキャッシュし、
            # 表示時の合議が待たずに揃うようにする
            self.analyze_page(source, entry, page)
        except Exception:
            pass

    # ---------------- エンコード済み画像(Web配信用) ----------------

    @staticmethod
    def mime_for_name(name: str) -> str:
        return _MIME_BY_EXT.get(os.path.splitext(name)[1].lower(),
                                "application/octet-stream")

    def _disk_key(self, entry: ComicEntry, page: PageRef,
                  width: int | None, height: int | None,
                  kind: str) -> str:
        try:
            mtime = os.path.getmtime(entry.physical_path)
        except OSError:
            mtime = 0
        raw = (f"{kind}|{entry.id}|{page.index}|{mtime}|"
               f"{width}|{height}|{self.resize_filter}")
        return hashlib.sha1(raw.encode()).hexdigest()

    def render_page(self, source, entry: ComicEntry, page: PageRef,
                    max_width: int | None = None,
                    max_height: int | None = None) -> tuple[bytes, str, str]:
        """ページ画像を返す。(bytes, mime, etag)

        リサイズ不要ならアーカイブ内の元データをそのまま返す。
        リサイズ時はディスクキャッシュを使う。
        """
        etag = self._disk_key(entry, page, max_width, max_height, "page")
        if not max_width and not max_height:
            data = self.get_raw(source, entry, page)
            return data, self.mime_for_name(page.name), etag

        cache_file = self._page_cache_dir / f"{etag}.jpg"
        if cache_file.exists():
            return cache_file.read_bytes(), "image/jpeg", etag

        data = self.get_raw(source, entry, page)
        with Image.open(io.BytesIO(data)) as img:
            img = ImageOps.exif_transpose(img) or img
            img = img.convert("RGB")
            ratio = min(
                (max_width or img.width) / img.width,
                (max_height or img.height) / img.height,
                1.0,
            )
            if ratio < 1.0:
                new_size = (max(1, int(img.width * ratio)),
                            max(1, int(img.height * ratio)))
                img = img.resize(
                    new_size, _RESAMPLE.get(self.resize_filter,
                                            Image.Resampling.LANCZOS)
                )
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=88)
        encoded = buf.getvalue()
        try:
            self._page_cache_dir.mkdir(parents=True, exist_ok=True)
            tmp = cache_file.with_suffix(".tmp")
            tmp.write_bytes(encoded)
            tmp.replace(cache_file)
        except OSError:
            pass
        return encoded, "image/jpeg", etag


    def _analysis_cache_file(self, entry: ComicEntry, page: PageRef) -> Path:
        etag = self._disk_key(entry, page, None, None, "analysis-v1")
        return self._analysis_cache_dir / f"{etag}.json"

    def _load_cached_analysis(self, entry: ComicEntry,
                              page: PageRef) -> ComicImageAnalysis | None:
        cache_file = self._analysis_cache_file(entry, page)
        if not cache_file.exists():
            return None
        try:
            import json
            data = json.loads(cache_file.read_text("utf-8"))
            crop_box = data.get("crop_box")
            return ComicImageAnalysis(
                source_width=int(data["source_width"]),
                source_height=int(data["source_height"]),
                border_type=data["border_type"],
                crop_box=tuple(crop_box) if crop_box else None,
                crop_confidence=float(data["crop_confidence"]),
                is_spread=bool(data["is_spread"]),
                spread_confidence=float(data["spread_confidence"]),
                split_x=data.get("split_x"),
                center_gutter_type=data.get("center_gutter_type"),
                has_center_crossing_content=bool(data["has_center_crossing_content"]),
                analysis_version=data.get("analysis_version", "comic-analysis-v1"),
            )
        except Exception:
            return None

    def analyze_page(self, source, entry: ComicEntry, page: PageRef) -> ComicImageAnalysis:
        cached = self._load_cached_analysis(entry, page)
        if cached is not None:
            return cached
        cache_file = self._analysis_cache_file(entry, page)
        data = self.get_raw(source, entry, page)
        with Image.open(io.BytesIO(data)) as img:
            img = ImageOps.exif_transpose(img) or img
            crop = detect_crop_box(img)
            base = img.crop(crop.crop_box) if crop.crop_box else img
            spread = detect_spread(base)
            split_x = spread.split_x
            if split_x is not None and crop.crop_box:
                split_x += crop.crop_box[0]
            analysis = ComicImageAnalysis(
                source_width=img.width,
                source_height=img.height,
                border_type=crop.border_type,
                crop_box=crop.crop_box,
                crop_confidence=crop.confidence,
                is_spread=spread.is_spread,
                spread_confidence=spread.confidence,
                split_x=split_x,
                center_gutter_type=spread.center_gutter_type,
                has_center_crossing_content=spread.has_center_crossing_content,
            )
        try:
            import json
            self._analysis_cache_dir.mkdir(parents=True, exist_ok=True)
            tmp = cache_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(analysis.to_dict(), ensure_ascii=False), "utf-8")
            tmp.replace(cache_file)
        except OSError:
            pass
        return analysis

    @staticmethod
    def _boxes_agree(boxes: list[tuple[int, int, int, int]],
                     dims: tuple[int, int], tol: float = 0.02) -> bool:
        width, height = dims
        for edge in range(4):
            values = [box[edge] for box in boxes]
            limit = tol * (width if edge % 2 == 0 else height)
            if max(values) - min(values) > limit:
                return False
        return True

    def _schedule_analysis(self, source, entry: ComicEntry, page: PageRef) -> None:
        key = self._analysis_cache_file(entry, page).name
        with self._analysis_inflight_lock:
            if key in self._analysis_inflight:
                return
            self._analysis_inflight.add(key)

        def worker() -> None:
            try:
                self.analyze_page(source, entry, page)
            except Exception:
                pass
            finally:
                with self._analysis_inflight_lock:
                    self._analysis_inflight.discard(key)

        self.executor.submit(worker)

    def analyze_page_stable(self, source, entry: ComicEntry,
                            pages: list[PageRef], index: int,
                            neighbor_count: int = 4,
                            compute_missing: bool = True) -> ComicImageAnalysis:
        """近傍ページとの合議で解析結果を安定化する。

        1枚ごとの検出はノイズ(縁に接する内容・量子化)でばらつくため、
        同一寸法の前後ページ(最大 neighbor_count 枚。先頭では後続、
        末尾では前方が自然に選ばれる)と合議する。

        見開きと単ページが混在するアーカイブを想定し、自ページの検出と
        整合する近傍だけを平均化に使う(自ページの検出が失敗した場合のみ、
        近傍同士が一致しているときに限ってその中央値で救済する)。
        """
        own = self.analyze_page(source, entry, pages[index])
        dims = (own.source_width, own.source_height)
        picked: list[ComicImageAnalysis] = []
        for offset in (1, -1, 2, -2, 3, -3, 4, -4):
            if len(picked) >= neighbor_count:
                break
            j = index + offset
            if not (0 <= j < len(pages)):
                continue
            try:
                if compute_missing:
                    analysis = self.analyze_page(source, entry, pages[j])
                else:
                    # 表示経路では待たない: キャッシュ済みの近傍だけで合議し、
                    # 未解析の近傍はバックグラウンドで解析を仕掛けておく
                    analysis = self._load_cached_analysis(entry, pages[j])
                    if analysis is None:
                        self._schedule_analysis(source, entry, pages[j])
                        continue
            except Exception:
                continue
            if (analysis.source_width, analysis.source_height) == dims:
                picked.append(analysis)
        if not picked:
            return own

        crop_box = own.crop_box
        neighbor_boxes = [a.crop_box for a in picked if a.crop_box]
        if own.crop_box:
            # 自ページと近いレイアウトの近傍だけでばらつきを均す
            close = [box for box in neighbor_boxes
                     if self._boxes_agree([own.crop_box, box], dims)]
            if close:
                merged = tuple(int(median(edge))
                               for edge in zip(own.crop_box, *close))
                if merged[0] < merged[2] and merged[1] < merged[3]:
                    crop_box = merged
        elif len(neighbor_boxes) >= 2 and self._boxes_agree(neighbor_boxes, dims):
            # 自ページだけ検出失敗: 近傍が一致しているならそれに合わせる
            merged = tuple(int(median(edge)) for edge in zip(*neighbor_boxes))
            if merged[0] < merged[2] and merged[1] < merged[3]:
                crop_box = merged

        # 救済の可否はトリミング後の縦横比で判断する(単ページや縦長は
        # 近傍が見開きでも分割しない → 見開き/単ページ混在への備え)
        if crop_box:
            cw, ch = crop_box[2] - crop_box[0], crop_box[3] - crop_box[1]
        else:
            cw, ch = dims
        cropped_aspect = cw / ch if ch > 0 else 0.0

        split_x = own.split_x
        rescued = False
        neighbor_splits = [a.split_x for a in picked if a.split_x]
        tol_x = max(1.0, dims[0] * 0.03)
        if own.split_x is not None:
            close = [s for s in neighbor_splits if abs(s - own.split_x) <= tol_x]
            if close:
                split_x = int(median([own.split_x, *close]))
        elif (cropped_aspect >= 1.35
              and len(neighbor_splits) >= 2
              and max(neighbor_splits) - min(neighbor_splits) <= tol_x):
            candidate = int(median(neighbor_splits))
            if dims[0] > 0 and 0.35 <= candidate / dims[0] <= 0.65:
                split_x = candidate
                rescued = True

        is_spread = own.is_spread or rescued
        return replace(own, crop_box=crop_box, split_x=split_x,
                       is_spread=is_spread)

    def render_variant_page(
        self,
        source,
        entry: ComicEntry,
        page: PageRef,
        viewport_width: int | None = None,
        viewport_height: int | None = None,
        dpr: float = 1.0,
        profile: str = "balanced",
        output_format: str = "auto",
        quality: int | None = None,
        auto_crop: bool = True,
        split_side: str = "full",
        crop_border_types: set[str] | None = None,
        context_pages: list[PageRef] | None = None,
    ) -> tuple[bytes, str, str]:
        profile_conf = _COMIC_PROFILES.get(profile, _COMIC_PROFILES["balanced"])
        dpr = max(1.0, min(float(dpr or 1.0), float(profile_conf["dpr_limit"])))
        format_name = "webp" if output_format == "auto" else output_format
        if format_name == "original":
            return self.render_page(source, entry, page, viewport_width, viewport_height)
        if format_name not in _FORMATS:
            format_name = "jpeg"
        quality = int(quality if quality is not None else profile_conf["quality"])
        quality = max(40, min(quality, 95))
        width = int((viewport_width or 0) * dpr) or None
        height = int((viewport_height or 0) * dpr) or None
        max_long = int(profile_conf["max_long_edge"] or 0)
        # 近傍ページ合議でトリミング/分割位置を安定化(コンテキストがある場合)
        analysis = None
        if context_pages and 0 <= page.index < len(context_pages):
            try:
                # 初回表示を待たせないため近傍はキャッシュ済み分のみで合議
                analysis = self.analyze_page_stable(
                    source, entry, context_pages, page.index,
                    compute_missing=False)
            except Exception:
                analysis = None
        cache_raw = (
            f"variant-v2|{entry.id}|{page.index}|{split_side}|{viewport_width}|"
            f"{viewport_height}|{dpr:.3f}|{profile}|{format_name}|{quality}|"
            f"{auto_crop}|{sorted(crop_border_types or [])}|{self.resize_filter}|"
            f"{analysis.crop_box if analysis else None}|"
            f"{analysis.split_x if analysis else None}|"
            f"{self.variant_sharpen}"
        )
        try:
            cache_raw += f"|{os.path.getmtime(entry.physical_path)}"
        except OSError:
            pass
        etag = hashlib.sha1(cache_raw.encode()).hexdigest()
        pil_format, mime, ext = _FORMATS[format_name]
        cache_file = self._variant_cache_dir / f"{etag}{ext}"
        if cache_file.exists():
            return cache_file.read_bytes(), mime, etag

        data = self.get_raw(source, entry, page)
        with Image.open(io.BytesIO(data)) as img:
            img = ImageOps.exif_transpose(img) or img
            applied_crop_left = 0
            if auto_crop:
                if analysis is not None:
                    allowed = crop_border_types
                    if analysis.crop_box and (
                            allowed is None or analysis.border_type in allowed):
                        applied_crop_left = analysis.crop_box[0]
                        img = img.crop(analysis.crop_box)
                else:
                    crop = detect_crop_box(img, allowed_border_types=crop_border_types)
                    if crop.crop_box:
                        applied_crop_left = crop.crop_box[0]
                        img = img.crop(crop.crop_box)
            if split_side in {"left", "right"}:
                split_x = img.width // 2
                if analysis is not None and analysis.split_x is not None:
                    local = analysis.split_x - applied_crop_left
                    if 0.35 * img.width <= local <= 0.65 * img.width:
                        split_x = int(local)
                if split_side == "left":
                    img = img.crop((0, 0, split_x, img.height))
                else:
                    img = img.crop((split_x, 0, img.width, img.height))
                if auto_crop:
                    crop = detect_crop_box(img, allowed_border_types=crop_border_types)
                    if crop.crop_box:
                        img = img.crop(crop.crop_box)
            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGB")
            if format_name == "jpeg" and img.mode != "RGB":
                img = img.convert("RGB")
            limits = []
            if width:
                limits.append(width / img.width)
            if height:
                limits.append(height / img.height)
            if max_long:
                limits.append(max_long / max(img.width, img.height))
            ratio = min([1.0, *limits]) if limits else 1.0
            if ratio < 1.0:
                new_size = (max(1, int(img.width * ratio)), max(1, int(img.height * ratio)))
                img = img.resize(new_size, _RESAMPLE.get(self.resize_filter, Image.Resampling.LANCZOS))
                if self.variant_sharpen:
                    # 縮小でなまった線と文字の輪郭を復元してから圧縮する
                    img = img.filter(ImageFilter.UnsharpMask(
                        radius=1.2, percent=70, threshold=2))
            buf = io.BytesIO()
            save_kwargs = {}
            if format_name in {"jpeg", "webp", "avif"}:
                save_kwargs["quality"] = quality
            if format_name == "jpeg":
                save_kwargs["optimize"] = True
            img.save(buf, pil_format, **save_kwargs)
        encoded = buf.getvalue()
        try:
            self._variant_cache_dir.mkdir(parents=True, exist_ok=True)
            tmp = cache_file.with_suffix(".tmp")
            tmp.write_bytes(encoded)
            tmp.replace(cache_file)
        except OSError:
            pass
        return encoded, mime, etag

    def render_thumbnail(self, source, entry: ComicEntry, page: PageRef,
                         size: int = 320) -> tuple[bytes, str, str]:
        etag = self._disk_key(entry, page, size, size, "thumb")
        cache_file = self._thumb_cache_dir / f"{etag}.jpg"
        if cache_file.exists():
            return cache_file.read_bytes(), "image/jpeg", etag
        data = self.get_raw(source, entry, page)
        with Image.open(io.BytesIO(data)) as img:
            img = ImageOps.exif_transpose(img) or img
            img = img.convert("RGB")
            img.thumbnail((size, size), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=80)
        encoded = buf.getvalue()
        try:
            self._thumb_cache_dir.mkdir(parents=True, exist_ok=True)
            tmp = cache_file.with_suffix(".tmp")
            tmp.write_bytes(encoded)
            tmp.replace(cache_file)
        except OSError:
            pass
        return encoded, "image/jpeg", etag
