# FrameDeck Linux HLS and iPad AAC worker (experimental)

Tracking: https://github.com/souten-yd/FrameDeck/issues/62

## Architecture

QNAP A remains the only public browser endpoint, persistent metadata writer
and source file owner. Linux B runs FrameDeck with the same shared secret and
an explicitly configured, OS-level read-only NFS/SMB mount of the QNAP video
library. B only processes a selected video from that mapped root.

This stage delegates both HLS fMP4 and the existing original-aac profile
(video bitstream copied; only unsupported audio converted to AAC for iPad).
Browser-facing HLS master, variant and segment URLs stay on A. The worker
file key is replaced with a random, media-scoped, volatile A-side key.
Nothing changes direct HTTP Range streaming or original video delivery.

The worker start and HLS file endpoints require timestamped HMAC requests
with replay protection. HLS file requests have a strict allowlist:
master.m3u8, named profile/playlist.m3u8, profile/init.mp4 and
profile/segment_00000.m4s. Worker access never receives general URLs or
unrestricted filesystem paths.

If B stops, A can start local HLS for the same video, profile and start
position while preserving the existing browser URL. The viewer may buffer.
Existing QNAP process/resource caps still apply. This does not guarantee
gapless failover or uninterrupted playback on low-powered QNAP codecs.

## Eligibility and effective-speed measurement

Remote HLS is initially disabled with the same cluster_offload_enabled
toggle as the existing remote fMP4. B must be paired, healthy, advertise
remote_hls, and match a read-only source-root fingerprint. A measures
end-to-end receipt of completed HLS video segments against their playback
duration. Three consecutive requests below 0.85x playback demand cause
a 60-second cooldown for **new** Linux HLS jobs. Existing streams are not
switched simply because a speed sample changed.

The status API includes hls_performance containing recent effective
segment rate, cooldown duration and current proxy session count.
This measurement includes encode waiting and network transfer; it is NOT
a dedicated FFmpeg FPS benchmark or a complete CPU/bandwidth comparison.

## Safety and limitations

The initial pairing secret must be transferred through protected admin
access. HMAC authenticates the request and the worker response handshake;
plain HTTP does not encrypt the video or ensure per-byte content integrity.
Use HTTPS on an untrusted LAN. B mounts the original files read-only.

The A-side proxy table is in memory, bounded to 64 keys with a one-hour
idle expiration. If A restarts, stale playlist keys may fail and the player
must request a new master. B has the existing HLS job idle cancellation
and concurrency limits.

This stage does NOT enable two database writers, remote source mutation,
raw-source serving, file deletion or automatic startup compilation.
Metadata revision synchronization, separate Linux copy of backup
generations and actual device benchmark-driven node selection remain
separate work.

## Validation

Run tests/test_worker_hls.py and the whole pytest suite, including video
direct Range, HLS AAC, URI-signing/replay and QNAP-lite resource tests.
Check browser/iPad Safari output and audio, worker failure during a
segment and exact playback position on real QNAP/Linux devices.
Review server storage writes and network transfer: HLS is already
compressed before passing from Linux to NAS, but it is still proxied.

See docs/CLUSTER_WORKER.md for enrollment and read-only mount instructions.
