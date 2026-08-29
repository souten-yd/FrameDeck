from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest

from framedeck.config import resolve_app_paths
from framedeck.core import update_service as updater


def test_normalize_arch_aliases():
    assert updater._normalize_arch("amd64") == "x86_64"
    assert updater._normalize_arch("x64") == "x86_64"
    assert updater._normalize_arch("arm64") == "aarch64"


def test_version_comparison():
    assert updater.is_newer_version("v2.2.0", "2.1.0")
    assert not updater.is_newer_version("2.2.0", "v2.2.0")
    assert not updater.is_newer_version("2.1.9", "2.2.0")
    with pytest.raises(updater.UpdateError):
        updater.is_newer_version("latest", "2.2.0")


def test_qnap_asset_prefers_ts253be_x86_64():
    release = {
        "assets": [
            {"name": "FrameDeck_2.3.0_generic_x86_64.qpkg", "size": 1},
            {"name": "FrameDeck_2.3.0_TS-253Be_x86_64.qpkg", "size": 2},
            {"name": "FrameDeck_2.3.0_TS-253Be_aarch64.qpkg", "size": 3},
        ]
    }
    asset = updater._select_qnap_asset(release, "x86_64")
    assert asset is not None
    assert asset["name"] == "FrameDeck_2.3.0_TS-253Be_x86_64.qpkg"


def test_qnap_asset_does_not_accept_wrong_architecture():
    release = {
        "assets": [
            {"name": "FrameDeck_2.3.0_TS-253Be_aarch64.qpkg", "size": 3},
        ]
    }
    assert updater._select_qnap_asset(release, "x86_64") is None


def test_check_resolves_qnap_release(monkeypatch, tmp_path: Path):
    fake_release = {
        "tag_name": "v2.3.0",
        "name": "FrameDeck v2.3.0",
        "html_url": "https://github.com/souten-yd/FrameDeck/releases/tag/v2.3.0",
        "published_at": "2026-08-29T00:00:00Z",
        "draft": False,
        "prerelease": False,
        "assets": [
            {
                "name": "FrameDeck_2.3.0_TS-253Be_x86_64.qpkg",
                "browser_download_url": "https://example.invalid/FrameDeck.qpkg",
                "size": 123,
                "digest": "sha256:" + "0" * 64,
            }
        ],
    }
    monkeypatch.setattr(updater, "_request_json", lambda _url: fake_release)
    monkeypatch.setattr(
        updater,
        "detect_platform",
        lambda: updater.PlatformInfo("qnap", "x86_64", "QNAP QTS (x86_64)", True),
    )
    monkeypatch.setattr(updater, "__version__", "2.2.0")

    manager = updater.UpdateManager(resolve_app_paths(tmp_path))
    result = manager.check()

    assert result["update_available"] is True
    assert result["can_update"] is True
    assert result["latest_version"] == "2.3.0"
    assert result["asset"]["name"].endswith("TS-253Be_x86_64.qpkg")


def test_safe_extract_rejects_path_traversal(tmp_path: Path):
    archive = tmp_path / "bad.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        item = tarfile.TarInfo("../escape.txt")
        data = b"bad"
        item.size = len(data)
        tf.addfile(item, io.BytesIO(data))

    with pytest.raises(updater.UpdateError):
        updater._safe_extract_tar(archive, tmp_path / "out")
    assert not (tmp_path / "escape.txt").exists()


def test_safe_extract_rejects_symlink(tmp_path: Path):
    archive = tmp_path / "link.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        item = tarfile.TarInfo("repo/link")
        item.type = tarfile.SYMTYPE
        item.linkname = "/etc/passwd"
        tf.addfile(item)

    with pytest.raises(updater.UpdateError):
        updater._safe_extract_tar(archive, tmp_path / "out")


def test_source_version(tmp_path: Path):
    package = tmp_path / "framedeck"
    package.mkdir()
    (package / "__init__.py").write_text('__version__ = "2.3.4"\n', encoding="utf-8")
    assert updater._source_version(tmp_path) == "2.3.4"
