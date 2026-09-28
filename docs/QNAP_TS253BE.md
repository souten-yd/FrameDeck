# FrameDeck on QNAP TS-253Be

FrameDeck uses one application codebase on Ubuntu and QNAP. The QPKG launcher
sets `FRAMEDECK_PROFILE=qnap-lite`; common features under `framedeck/` apply
to both platforms without a fork. Linux keeps its original defaults and limits.

## Lightweight execution policy (2.4.0)

The QNAP profile is adopted once for new and existing QPKG data directories.
After that, settings edited in the UI are retained across restarts and QPKG
upgrades. The migration marker is `config/qnap-lite-v1`. The platform safety
caps on transcoding and thread counts remain in force even if settings request
a higher resolution.

| Work | QNAP default / limit | Linux behavior | Quality tradeoff |
|---|---|---|---|
| Comic page | Original archive bytes, no resize, crop, sharpen or AVIF/WebP re-encode | Existing adaptive image pipeline | Large originals use more bandwidth; no border removal |
| Spread analysis | No background analysis in prefetch; no automatic split | Existing prefetch and analysis | Manual spread layout remains, wide page half splitting is off |
| Comic prefetch | One page ahead, none behind; two workers, 64 MB raw cache | Eight ahead, two behind; four workers, 512 MB | Fast page flipping may wait for archive I/O |
| Comic thumbnails | Up to 160 px, bilinear, JPEG quality 65 | 320 px, Lanczos, quality 80 | Softer previews |
| Nested archive cache | 1 GB | 10 GB | Older nested entries may need re-extraction |
| Video direct play | Range streaming first; 2 MB per stream read-ahead | Adaptive profiles, 8 MB read-ahead | Browser must support the source codec |
| Remux | Retain encoded video without resizing when possible | Existing remux path | The source codec must be playable in the browser |
| Video encode | Bundled ffmpeg on demand, max 1280×720 only for manual quality choice; unsupported codecs automatically fall back to 480p, one process shared across HLS and fMP4, two encoder threads | Higher profiles and parallel jobs allowed | Quality and speed depend on the source; demanding codecs can still fall behind real time |
| Video thumbnail | Serve existing cache only; no cold thumbnail decoding | Generate on demand | New video thumbnails are absent |
| Library volume view | Skip recent nested archive rescan | Scan recently opened nested entries when needed | Per-entry progress can be less precise in the volume overview |
| Web requests | Eight worker thread tokens | 96 | Concurrent requests may queue rather than saturating the NAS |
| Cache maintenance | Deferred two minutes after launch | During startup | Disk use can briefly remain above the configured limit |

Transcoding is **included**, but is only used if direct play or remux cannot
serve the client, or if the user explicitly requests conversion. On QNAP,
**Auto means original resolution on desktop, mobile, cellular, and saveData**.
No network-based or starvation-based quality downgrade is triggered. A manual
720p/480p/360p selection is honored (higher old choices are capped to 720p);
an unsupported video codec falls back to 480p so it can still play. The bundled
static ffmpeg uses software H.264 encoding. TS-253Be's J3455 has Intel Quick
Sync, but this QPKG does not claim GPU acceleration: that would require a
QTS-compatible VAAPI-enabled ffmpeg and access to `/dev/dri`, verified on the
actual NAS. Test with 480p material before relying on real-time conversion of
high-bitrate HEVC or 4K inputs. Try manual 720p only after checking conversion
speed on the TS-253Be. HLS requests wait
for the shared conversion slot; another fMP4 request receives a busy response.

### Quality improvements to try later

| Candidate | Benefit | NAS cost / risk | Priority |
|---|---|---|---|
| Enable client-side contrast/sharpen only | Improve display without NAS image processing | Device GPU/battery, no NAS CPU | First |
| Raise comic prefetch to two pages | Faster page turns | More archive reads and memory | Second |
| Resize original pages to mobile width on demand | Lower network use | Pillow decoding and encoding each uncached page | Only if network is bottleneck |
| Restore auto crop / spread detection | Better framing | Full-page analysis and cache I/O | Only for archives needing it |
| Raise software encode from 480p to 720p | Sharper unsupported video | J3455 may fail real-time playback | After measuring CPU and fps |
| VAAPI H.264 on QTS | Faster compatible transcodes | Device access, ffmpeg compatibility and fallback engineering | Test on NAS before implementation |

These are toggles or isolated runtime policies in the shared codebase; keep
QNAP-only caps in `framedeck/runtime_profile.py` and the launch profile rather
than maintaining an application fork. For diagnosis compare playback startup
time, CPU load, resident memory, page-turn latency, and actual conversion fps
on the TS-253Be.

## Large library listings (2.4.1)

The listing improvements live in the shared `framedeck/` code and also apply
to the Linux release. A directory is enumerated with `os.scandir`, then only
matching entries are stat'ed once. Ratings and IDs are parsed from the name
without another filesystem lookup. SQLite persists the full listing in one
transaction. The comic volume view is computed from that same listing and
returned with the items, avoiding a second scan. Navigating to a nested folder
no longer scans the grandparent. The browser renders the list once; QNAP
initially creates 300 rows and reveals more in groups of 300 on demand.
Linux keeps its full-list rendering behavior and existing quality settings.

From 2.4.3, returning to a parent folder centers the previously opened child
row. QNAP draws a 300-row window around it, with buttons to reveal preceding
or following rows. Folder history and refresh also restore the saved scroll
position; the same navigation applies to Linux without limiting its row count.

In a local warm-cache benchmark with 4,000 empty `.cbz` files on temporary
storage, the original listing took 0.20–0.24 s and the shared listing took
0.11 s. This is a synthetic measurement, not a TS-253Be HDD result. On an
actual NAS, compare cold and warm loads for a representative folder and watch
CPU, disk utilization, API latency, and browser responsiveness.

| Further candidate | Assessment | When to revisit |
|---|---|---|
| Four parallel directory stat workers | Extra seeks on HDD and SQLite serialization may outweigh parallelism; not enabled | Measure on NAS SSD or multiple independent disks after the single-scan change |
| Folder mtime/TTL cache | Avoids repeated readdir, but external file edits and rating renames can go stale | Only if repeat loads remain slow; define invalidation first |
| Server pagination / incremental volume metadata | Cuts network payload for tens of thousands of files, but changes global sorting, counts, selection and volume order | If JSON transfer is the remaining bottleneck |
| Virtualized browser list | Bounds DOM nodes while preserving keyboard focus and scroll position | If QNAP's 300-row batches still feel slow |

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

QDK packages the same artwork as the Web PWA into three App Center GIFs:
`FrameDeck.gif` (64×64), `FrameDeck_80.gif` (80×80), and
`FrameDeck_gray.gif` (64×64, disabled). They are generated from the checked-in
`icon-512.png` during the build, so the Web and QTS icons stay in sync.

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
| `FRAMEDECK_PROFILE` | unset | `qnap-lite` |

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
