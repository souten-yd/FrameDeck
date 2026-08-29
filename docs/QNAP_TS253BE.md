# FrameDeck on QNAP TS-253Be

FrameDeck uses one application codebase on Ubuntu and QNAP. QNAP-specific
behavior is limited to `packaging/qnap/` and environment variables; features
implemented under `framedeck/` apply to both platforms without a fork.

## Supported target

- QNAP TS-253Be
- Intel Celeron J3455 / x86_64
- QTS 5.0 or later
- Web mode only (`web_desktop` / Tkinter is not packaged)
- Default port: `9000`

The QPKG is self-contained. The NAS does **not** need Python, pip, Entware,
ffmpeg, ffprobe, 7-Zip, or a compiler. The package bundles portable CPython,
Python dependencies, static ffmpeg/ffprobe, and 7zz (also exposed as `7z` for
FrameDeck's archive backend).

The portable Python target is baseline `x86_64-unknown-linux-gnu`; compiled
Python dependencies are explicitly downloaded as `manylinux2014_x86_64`
wheels. This avoids accidentally packaging binaries linked against the newer
glibc version of the Ubuntu build host.

## GitHub Actions / cost policy

GitHub Actions is **manual-only**. A push, pull request, tag, or GitHub Release
does not automatically start CI and therefore does not incur an Actions build
for ordinary development.

When validation is genuinely needed, open **Actions -> FrameDeck Manual
Validation -> Run workflow**. The workflow has two optional switches:

- `build_qpkg=false` (default): run the Python tests only.
- `build_qpkg=true`: after tests pass, also create the self-contained TS-253Be
  QPKG.
- `publish_release=true`: when building a QPKG, upload it to an already-created
  GitHub Release for the selected ref.

The QPKG build is intentionally opt-in because it downloads portable Python,
ffmpeg, 7-Zip, and QDK and is much heavier than ordinary tests.

The output package name is:

```text
FrameDeck_<version>_TS-253Be_x86_64.qpkg
```

## Install

1. Build the `.qpkg` locally on Ubuntu, or explicitly run the manual Actions
   workflow with `build_qpkg=true`.
2. Open QTS **App Center**.
3. Choose **Install Manually**.
4. Select the FrameDeck QPKG and accept the third-party/manual package warning.
5. Start FrameDeck from App Center.
6. Open `http://<NAS-IP>:9000/`.
7. In FrameDeck settings, register the QNAP shared folders containing manga and
   video files (for example `/share/Download/Manga`).

QTS may require allowing installation of unsigned/manual applications. The
package is currently not QNAP code-signed.

## Runtime and persistent-data layout

QDK installs replaceable application files under the volume's
`.qpkg/FrameDeck` directory:

```text
/share/<volume>/.qpkg/FrameDeck/
├── app/                  shared FrameDeck Python application
├── runtime/python/       bundled portable CPython + site-packages
├── bin/
│   ├── ffmpeg
│   ├── ffprobe
│   ├── 7zz
│   └── 7z -> 7zz
└── FrameDeck.sh          App Center service controller
```

Mutable FrameDeck state deliberately lives **outside** the QPKG installation
directory:

```text
/share/<volume>/.framedeck/
├── config/               settings.json
├── data/                 framedeck.db and session/index data
├── cache/                comic/video generated caches
├── logs/                 framedeck.log and qpkg-service.log
└── runtime/              pid/temp/locks
```

The service first asks QTS for the default data-volume mount point and falls
back to deriving the containing volume from the QPKG path. It then exports
`FRAMEDECK_HOME=/share/<volume>/.framedeck`.

This separation is important: updating the QPKG can replace `app/`, `runtime/`
and `bin/` without replacing the user's settings, database, logs, or caches.
A legacy `<QPKG>/var` directory is migrated on first start when no external
FrameDeck data exists yet.

## Start / stop / diagnostics

App Center controls `packaging/qnap/FrameDeck.sh`, which accepts:

```sh
FrameDeck.sh start
FrameDeck.sh stop
FrameDeck.sh restart
FrameDeck.sh status
```

To resolve the actual install path over SSH:

```sh
/sbin/getcfg FrameDeck Install_Path -f /etc/config/qpkg.conf
```

To resolve the default data volume:

```sh
/sbin/getcfg SHARE_DEF defVolMP -f /etc/config/def_share.info
```

The service launcher log is normally:

```text
/share/<volume>/.framedeck/logs/qpkg-service.log
```

and the normal rotating application log is:

```text
/share/<volume>/.framedeck/logs/framedeck.log
```

## Ubuntu and QNAP feature parity

New features should normally be implemented only below `framedeck/`. Platform
launchers use these variables:

| Variable | Default | QNAP |
|---|---|---|
| `FRAMEDECK_MODE` | `web` | `web` |
| `FRAMEDECK_HOST` | `0.0.0.0` | `0.0.0.0` |
| `FRAMEDECK_PORT` | `9000` | `9000` |
| `FRAMEDECK_HOME` | auto | `/share/<volume>/.framedeck` |
| `FRAMEDECK_OPEN_BROWSER` | `false` | `false` |

The portable entry point is:

```sh
python3 -m framedeck
```

Ubuntu may continue using `python3 FrameDeck.py`; services/containers may use
the same environment-driven module entry point as QNAP.

## Local QPKG build on Ubuntu

Local building is preferred while developing because it does not consume
GitHub Actions minutes:

```sh
sudo apt install curl git xz-utils
bash packaging/qnap/build.sh
```

Output is written to `dist/`. The build machine needs internet access because
portable CPython, Python wheels, ffmpeg, 7-Zip, and QDK are downloaded at build
time. These are build-time downloads only; the target NAS does not download
them.

QDK source layout is generated as:

```text
qpkg.cfg
shared/                 # service script
x86_64/                 # app + Python + native tools
```

so QDK produces an x86_64-only package suitable for the TS-253Be.

## Updating dependencies

- Python series: `PYTHON_SERIES` in `packaging/qnap/build.sh` (default `3.12`)
- Python binary target: baseline `x86_64-unknown-linux-gnu`
- Python wheel target: `TARGET_PLATFORM` (default `manylinux2014_x86_64`)
- 7-Zip: `SEVENZIP_VERSION` in `packaging/qnap/build.sh`
- Python libraries: `packaging/qnap/requirements-qnap.txt`
- FrameDeck version: `framedeck/__init__.py`

Dependency changes should be validated locally first. Use the manual GitHub
Actions workflow only when an independent Actions environment build is worth
the cost.

## Design rule

Do not add QNAP checks throughout `framedeck/`. If QNAP-specific behavior is
required, prefer one of these in order:

1. environment/configuration value,
2. generic runtime capability detection,
3. a thin adapter under `packaging/qnap/`.

This keeps Ubuntu and QNAP on the same code and makes future FrameDeck feature
changes apply to both with minimal or zero platform-specific edits.
