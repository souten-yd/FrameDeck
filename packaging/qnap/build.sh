#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PACKAGING_DIR="$ROOT_DIR/packaging/qnap"
DIST_DIR="${DIST_DIR:-$ROOT_DIR/dist}"
WORK_DIR="${WORK_DIR:-${RUNNER_TEMP:-/tmp}/framedeck-qpkg}"
PYTHON_SERIES="${PYTHON_SERIES:-3.12}"
SEVENZIP_VERSION="${SEVENZIP_VERSION:-2501}"
TARGET_PLATFORM="${TARGET_PLATFORM:-manylinux2014_x86_64}"
TARGET_ABI="${TARGET_ABI:-cp312}"

# QDK must be installed from its official package.  In particular, qpkg_encrypt
# is required when cross-building off-NAS so QDK can write the checksum in the
# QPKG tail.  Treat its absence as a hard failure instead of producing a QPKG
# that QTS later rejects as a file-format error.
for cmd in curl tar xz python3 qbuild qpkg_encrypt; do
    command -v "$cmd" >/dev/null || { echo "missing build dependency: $cmd" >&2; exit 1; }
done

VERSION="${VERSION:-$(sed -n 's/^__version__ = "\([^"]*\)"/\1/p' "$ROOT_DIR/framedeck/__init__.py")}" 
if [[ -z "$VERSION" ]]; then
    echo "unable to determine FrameDeck version" >&2
    exit 1
fi
QPKG_VERSION="${VERSION//-/_}"

rm -rf "$WORK_DIR/env" "$WORK_DIR/wheels"
mkdir -p "$WORK_DIR/env/shared" "$WORK_DIR/env/icons" \
    "$WORK_DIR/env/x86_64/app" "$WORK_DIR/env/x86_64/bin" \
    "$WORK_DIR/env/x86_64/runtime" "$WORK_DIR/wheels" "$DIST_DIR"

cp "$PACKAGING_DIR/qpkg.cfg" "$WORK_DIR/env/qpkg.cfg"
sed -i "s/^QPKG_VER=.*/QPKG_VER=\"$QPKG_VERSION\"/" "$WORK_DIR/env/qpkg.cfg"
cp "$PACKAGING_DIR/package_routines" "$WORK_DIR/env/package_routines"
chmod 755 "$WORK_DIR/env/package_routines"
cp "$PACKAGING_DIR/FrameDeck.sh" "$WORK_DIR/env/shared/FrameDeck.sh"
chmod 755 "$WORK_DIR/env/shared/FrameDeck.sh"
cp -a "$ROOT_DIR/framedeck" "$WORK_DIR/env/x86_64/app/"
find "$WORK_DIR/env/x86_64/app" -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true

# Portable CPython: the NAS does not need Python, pip, Entware, or a compiler.
# Baseline x86_64 (not x86_64_v2/v3) maximizes compatibility with the J3455.
PY_META="$WORK_DIR/python-release.json"
curl -fsSL "https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest" -o "$PY_META"
PY_URL="$(python3 - "$PY_META" "$PYTHON_SERIES" <<'PY'
import json, re, sys
meta = json.load(open(sys.argv[1], encoding='utf-8'))
series = re.escape(sys.argv[2])
pat = re.compile(rf'^cpython-{series}\.\d+\+[^/]+-x86_64-unknown-linux-gnu-install_only\.tar\.gz$')
for asset in meta.get('assets', []):
    if pat.match(asset.get('name', '')):
        print(asset['browser_download_url'])
        break
else:
    raise SystemExit('no matching portable CPython release asset found')
PY
)"
curl -fL --retry 3 "$PY_URL" -o "$WORK_DIR/python.tar.gz"
tar -xzf "$WORK_DIR/python.tar.gz" -C "$WORK_DIR/env/x86_64/runtime"
PYTHON="$WORK_DIR/env/x86_64/runtime/python/bin/python3"

# Do not let the Ubuntu builder choose wheels that require the builder's newer
# glibc. Download CPython 3.12 x86_64 manylinux2014 wheels explicitly, then
# install only from that wheelhouse. This keeps compiled extensions (Pillow,
# pydantic-core, etc.) at a glibc 2.17-compatible baseline for QTS.
"$PYTHON" -m pip download \
    --disable-pip-version-check \
    --dest "$WORK_DIR/wheels" \
    --only-binary=:all: \
    --platform "$TARGET_PLATFORM" \
    --implementation cp \
    --python-version 312 \
    --abi "$TARGET_ABI" \
    -r "$PACKAGING_DIR/requirements-qnap.txt"
"$PYTHON" -m pip install \
    --disable-pip-version-check \
    --no-index \
    --find-links "$WORK_DIR/wheels" \
    -r "$PACKAGING_DIR/requirements-qnap.txt"

# Verify the packaged Python can import every compiled/runtime dependency before
# QDK packaging. This catches an unusable wheel set before producing a QPKG.
PYTHONPATH="$WORK_DIR/env/x86_64/app" "$PYTHON" - <<'PY'
import PIL
import fastapi
import pydantic
import rarfile
import uvicorn
import aiofiles
import send2trash
import framedeck
print('FrameDeck runtime imports: OK')
PY

# Static ffmpeg/ffprobe keeps video probing/transcoding independent of QTS apps.
curl -fL --retry 3 \
    "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz" \
    -o "$WORK_DIR/ffmpeg.tar.xz"
rm -rf "$WORK_DIR/ffmpeg"
mkdir -p "$WORK_DIR/ffmpeg"
tar -xJf "$WORK_DIR/ffmpeg.tar.xz" -C "$WORK_DIR/ffmpeg" --strip-components=1
install -m 755 "$WORK_DIR/ffmpeg/ffmpeg" "$WORK_DIR/env/x86_64/bin/ffmpeg"
install -m 755 "$WORK_DIR/ffmpeg/ffprobe" "$WORK_DIR/env/x86_64/bin/ffprobe"

# 7zz provides RAR/CBR extraction without requiring proprietary unrar. FrameDeck
# currently discovers the command as `7z`, so provide both names.
curl -fL --retry 3 \
    "https://www.7-zip.org/a/7z${SEVENZIP_VERSION}-linux-x64.tar.xz" \
    -o "$WORK_DIR/7zip.tar.xz"
rm -rf "$WORK_DIR/7zip"
mkdir -p "$WORK_DIR/7zip"
tar -xJf "$WORK_DIR/7zip.tar.xz" -C "$WORK_DIR/7zip"
install -m 755 "$WORK_DIR/7zip/7zz" "$WORK_DIR/env/x86_64/bin/7zz"
ln -s 7zz "$WORK_DIR/env/x86_64/bin/7z"

# Use the officially installed QDK so its helper programs and template assets
# are available. Strict mode turns QDK warnings into build failures, and the
# query step validates the finished package structure before publishing it.
pushd "$WORK_DIR/env" >/dev/null
qbuild --strict --build-arch x86_64
popd >/dev/null

QPKG_FILE="$(find "$WORK_DIR/env/build" -maxdepth 1 -type f -name '*.qpkg' -print -quit)"
if [[ -z "$QPKG_FILE" ]]; then
    echo "qbuild completed but no .qpkg was produced" >&2
    exit 1
fi

qbuild --query info "$QPKG_FILE"

OUT="$DIST_DIR/FrameDeck_${QPKG_VERSION}_TS-253Be_x86_64.qpkg"
cp "$QPKG_FILE" "$OUT"
echo "QPKG: $OUT"
