from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
QNAP = ROOT / "packaging" / "qnap"


def test_qpkg_targets_x86_64_web_service():
    cfg = (QNAP / "qpkg.cfg").read_text("utf-8")
    assert 'QPKG_NAME="FrameDeck"' in cfg
    assert 'QPKG_ARCH="x86_64"' in cfg
    assert 'QPKG_SERVICE_PROGRAM="FrameDeck.sh"' in cfg
    assert 'QPKG_SERVICE_PORT="9000"' in cfg
    assert 'QPKG_DISTRIBUTION_TYPE="1"' in cfg
    assert 'QDK_DATA_DIR_X86_64="x86_64"' in cfg
    assert 'QTS_MINI_VERSION="5.0.0"' in cfg


def test_qnap_service_uses_bundled_runtime_and_external_persistent_home():
    script = (QNAP / "FrameDeck.sh").read_text("utf-8")
    assert 'runtime/python/bin/python3' in script
    assert '$VOLUME_ROOT/.framedeck' in script
    assert 'export FRAMEDECK_HOME="$VAR_DIR"' in script
    assert 'export FRAMEDECK_MODE="web"' in script
    assert 'export PATH="$QPKG_ROOT/bin:' in script
    assert 'export QNAP_QPKG="$QPKG_NAME"' in script
    assert ' -m framedeck ' in script
    # Persistent state must not default to a directory that QPKG upgrades replace.
    assert 'VAR_DIR="$QPKG_ROOT/var"' not in script


def test_builder_bundles_required_native_tools_and_validates_qpkg():
    script = (QNAP / "build.sh").read_text("utf-8")
    assert "python-build-standalone" in script
    assert "binmgr/ffmpeg/releases/download" in script
    assert 'FFMPEG_VERSION="8.1.2"' in script
    assert 'FFMPEG_SHA256=' in script
    assert 'sha256sum -c -' in script
    assert 'env/x86_64/app' in script
    assert 'env/x86_64/runtime' in script
    assert 'manylinux2014_x86_64' in script
    assert 'ln -s 7zz' in script
    assert 'qpkg_encrypt' in script
    assert 'cp "$PACKAGING_DIR/package_routines"' in script
    assert 'qbuild --strict --build-arch x86_64' in script
    assert 'qbuild --query info "$QPKG_FILE"' in script


def test_github_actions_is_manual_only_to_control_cost():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text("utf-8")
    assert "workflow_dispatch:" in workflow
    assert "pull_request:" not in workflow
    assert "branches:" not in workflow
    assert "tags:" not in workflow
    assert "build_qpkg:" in workflow
    assert "qdk_2.5.3_amd64.deb" in workflow
    assert "command -v qpkg_encrypt" in workflow
