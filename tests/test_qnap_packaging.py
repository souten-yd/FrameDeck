from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
QNAP = ROOT / "packaging" / "qnap"


def test_qpkg_targets_x86_64_web_service():
    cfg = (QNAP / "qpkg.cfg").read_text("utf-8")
    assert 'QPKG_NAME="FrameDeck"' in cfg
    assert 'QPKG_SERVICE_PROGRAM="FrameDeck.sh"' in cfg
    assert 'QPKG_SERVICE_PORT="9000"' in cfg
    assert 'QDK_DATA_DIR_X86_64="x86_64"' in cfg
    assert 'QTS_MINI_VERSION="5.0.0"' in cfg


def test_qnap_service_uses_bundled_runtime_and_persistent_home():
    script = (QNAP / "FrameDeck.sh").read_text("utf-8")
    assert 'runtime/python/bin/python3' in script
    assert 'export FRAMEDECK_HOME="$VAR_DIR"' in script
    assert 'export FRAMEDECK_MODE="web"' in script
    assert 'export PATH="$QPKG_ROOT/bin:' in script
    assert ' -m framedeck ' in script


def test_builder_bundles_required_native_tools():
    script = (QNAP / "build.sh").read_text("utf-8")
    assert "python-build-standalone" in script
    assert "ffmpeg-release-amd64-static" in script
    assert "7zip/7zz" in script
    assert "qnap-dev/QDK" in script
