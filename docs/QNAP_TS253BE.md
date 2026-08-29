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
Python dependencies, static ffmpeg/ffprobe, and 7zz.

## Build policy

Normal pushes and pull requests run only the Python test suite. QPKG creation
is deliberately limited to either:

1. a manual **Run workflow** invocation of `FrameDeck CI`, or
2. pushing a release tag such as `v2.1.1`.

This avoids repeatedly downloading/building the large portable runtime and
keeps GitHub Actions usage low.

A tag build publishes:

```text
FrameDeck_<version>_TS-253Be_x86_64.qpkg
```

as both a short-lived Actions artifact and a GitHub Release asset.

## Install

1. Download the `.qpkg` produced by GitHub Actions or a GitHub Release.
2. Open QTS **App Center**.
3. Choose **Install Manually**.
4. Select the FrameDeck QPKG and accept the third-party/manual package warning.
5. Start FrameDeck from App Center.
6. Open `http://<NAS-IP>:9000/`.
7. In FrameDeck settings, register the QNAP shared folders containing manga and
   video files (for example `/share/Download/Manga`).

QTS may require allowing installation of unsigned/manual applications. The
package is currently not QNAP code-signed.

## Runtime layout

QDK installs the package under the volume's `.qpkg/FrameDeck` directory. The
important directories are:

```text
FrameDeck/
├── app/                  shared FrameDeck Python application
├── runtime/python/       bundled portable CPython + site-packages
├── bin/
│   ├── ffmpeg
│   ├── ffprobe
│   ├── 7zz
│   └── 7z -> 7zz
└── var/                  persistent writable application state
    ├── config/
    ├── data/
    ├── cache/
    ├── logs/
    └── runtime/
```

The service exports `FRAMEDECK_HOME=<QPKG>/var`, so application code never has
to know QNAP-specific storage paths.

## Start / stop / diagnostics

App Center controls `packaging/qnap/FrameDeck.sh`, which accepts:

```sh
FrameDeck.sh start
FrameDeck.sh stop
FrameDeck.sh restart
FrameDeck.sh status
```

Application logs are stored under:

```text
<FrameDeck QPKG install path>/var/logs/
```

`service.log` contains launcher/stdout/stderr output and `framedeck.log` is the
normal rotating FrameDeck application log.

To resolve the actual install path over SSH:

```sh
/sbin/getcfg FrameDeck Install_Path -f /etc/config/qpkg.conf
```

## Ubuntu and QNAP feature parity

New features should normally be implemented only below `framedeck/`. Platform
launchers use these variables:

| Variable | Default | QNAP |
|---|---|---|
| `FRAMEDECK_MODE` | `web` | `web` |
| `FRAMEDECK_HOST` | `0.0.0.0` | `0.0.0.0` |
| `FRAMEDECK_PORT` | `9000` | `9000` |
| `FRAMEDECK_HOME` | auto | `<QPKG>/var` |
| `FRAMEDECK_OPEN_BROWSER` | `false` | `false` |

The portable entry point is:

```sh
python3 -m framedeck
```

Ubuntu may continue using `python3 FrameDeck.py`; services/containers may use
the same environment-driven module entry point as QNAP.

## Local QPKG build on Ubuntu

The same builder used by Actions can be run on Ubuntu:

```sh
sudo apt install curl git xz-utils libxml2-utils xmlstarlet pv dos2unix
bash packaging/qnap/build.sh
```

Output is written to `dist/`. The build machine needs internet access because
portable CPython, ffmpeg, 7-Zip, and QDK are downloaded at build time. These are
build-time downloads only; the target NAS does not download them.

## Updating dependencies

- Python series: `PYTHON_SERIES` in `packaging/qnap/build.sh` (default `3.12`)
- 7-Zip: `SEVENZIP_VERSION` in `packaging/qnap/build.sh`
- Python libraries: `packaging/qnap/requirements-qnap.txt`
- FrameDeck version: `framedeck/__init__.py`

For releases, prefer a tag matching the application version, e.g. `v2.1.1`.

## Design rule

Do not add QNAP checks throughout `framedeck/`. If QNAP-specific behavior is
required, prefer one of these in order:

1. environment/configuration value,
2. generic runtime capability detection,
3. a thin adapter under `packaging/qnap/`.

This keeps Ubuntu and QNAP on the same code and makes future FrameDeck feature
changes apply to both with minimal or zero platform-specific edits.
