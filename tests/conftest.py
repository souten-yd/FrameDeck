import io
import os
import sys
import zipfile
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).parent.parent))

from framedeck.config import Settings, ensure_runtime_directories, resolve_app_paths
from framedeck.core.services import build_services
from framedeck.core.storage import Storage


def make_image_bytes(width=800, height=1200, color=(120, 40, 40)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, "JPEG")
    return buf.getvalue()


def make_zip(path: Path, pages: int = 4, landscape_pages=(),
             nested: dict | None = None, prefix: str = "") -> Path:
    """テスト用ZIP漫画を作る。nested={"child.cbz": ページ数}で子アーカイブ追加。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        for i in range(pages):
            if i in landscape_pages:
                data = make_image_bytes(1600, 900)
            else:
                data = make_image_bytes()
            zf.writestr(f"{prefix}{i + 1:03d}.jpg", data)
        for name, child_pages in (nested or {}).items():
            child_buf = io.BytesIO()
            with zipfile.ZipFile(child_buf, "w") as child:
                for i in range(child_pages):
                    child.writestr(f"{i + 1:03d}.jpg", make_image_bytes())
            zf.writestr(name, child_buf.getvalue())
    return path


@pytest.fixture()
def app_env(tmp_path):
    """一時ディレクトリにFrameDeckの実行環境一式を作る。"""
    home = tmp_path / "FrameDeck_venv"
    paths = resolve_app_paths(home)
    ensure_runtime_directories(paths)
    settings = Settings(paths)
    storage = Storage(paths.database_file)
    services = build_services(settings, storage, paths)
    yield services, paths, tmp_path
    services.shutdown()


@pytest.fixture()
def comic_root(tmp_path):
    """指示書25.1の構成:
    root/
    ├── A.zip        (A1.cbz, A2.cbz を含む / 直接画像なし)
    ├── B/
    │   ├── B1.cbz
    │   └── B2.cbz
    └── C.cbz
    """
    root = tmp_path / "root"
    root.mkdir()
    make_zip(root / "A.zip", pages=0,
             nested={"A1.cbz": 4, "A2.cbz": 4})
    make_zip(root / "B" / "B1.cbz", pages=4)
    make_zip(root / "B" / "B2.cbz", pages=4)
    make_zip(root / "C.cbz", pages=5)
    return root
