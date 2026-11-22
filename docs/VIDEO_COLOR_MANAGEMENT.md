# Video HDR / SDR colour management

FrameDeck chooses a video output colour path per browser display capability and
source colour metadata. This setting does **not** calibrate monitors or obtain
their ICC profiles or peak luminance.

## How it works

- `ffprobe` video-stream fields `color_primaries`, `color_transfer`,
  `color_space` and `color_range` are exposed in `/api/videos/{id}`.
  PQ (`smpte2084`) is HDR10-like, HLG (`arib-std-b67`) is HLG; Dolby
  Vision side data is reported separately. Unknown remains unknown.
- The browser reports the current `(dynamic-range: high)` result. This is
  only a display capability *hint*, not an exact HDR playback test.
- In **Auto**, suitable native HDR playback or video stream-copy/remux is
  preserved on an HDR display. A PQ/HLG source on an SDR display uses an
  actual HDR→SDR tone map when the FFmpeg path is required.
- In **Original**, the native HDR video stays unchanged where direct play or
  video stream-copy is possible. If video must be re-encoded for compatibility,
  HDR is tone-mapped rather than silently tagged as SDR.
- In **SDR**, PQ/HLG video always uses the BT.709 tone-mapping path.
- Ordinary SDR sources retain existing direct-play, HLS, fMP4 and remux
  behavior. No automatic brightness/saturation CSS filters are applied.

The transform is applied **only when re-encoding**:
`zscale` to linear light → floating-point RGB → `tonemap=hable` →
BT.709 primaries/transfer/matrix and limited range. H.264 output declares
BT.709 colour metadata. A remux never applies a video filter.

## Performance and compatibility

- QNAP TS-253Be still serves original files via HTTP Range when possible.
  HDR→SDR transcoding on QNAP is limited to 480p when automatically selected;
  manual lower profiles and existing CPU/thread limits remain effective.
- When Linux conversion offload is already enabled and available, the
  tone-mapping flag is part of the existing authenticated fMP4 or HLS worker
  request. If a remote HLS stream fails and QNAP can produce the same profile,
  fallback keeps the SDR conversion flag; worker-only 1080p does not fall back
  to an unsafe high-resolution NAS encode.
- FFmpeg must provide **both** `zscale` and `tonemap` (the bundled QNAP
  build advertises libzimg, but the installed binary is checked at request
  time). Missing filters fail explicitly instead of serving wrongly converted
  HDR. HDR can be slow to process on the J3455 CPU.
- HDR video that supplies **only** Dolby Vision signalling and no recognised
  PQ or HLG transfer characteristic is not guessed into a filter path.
- Existing HLS cache entries are not used for the new colour decision. The
  colour-conversion flag forms part of its versioned cache identity.
- As of this change, display capability is re-evaluated when a video is
  opened/reopened. A monitor move during playback is left to OS/browser HDR
  presentation until the video is reopened.

## Troubleshooting

Compare the same file with VLC at original quality. In the FrameDeck video
player, identify whether the source is **Direct**, **HLS**, or **fMP4** and
whether the badge says `HDR→SDR (BT.709)`. For a suspect file:

```sh
ffprobe -v error -select_streams v:0 \
  -show_entries stream=codec_name,pix_fmt,color_range,color_space,color_transfer,color_primaries \
  -of default=noprint_wrappers=1 "/path/to/movie.mkv"
```

`smpte2084` means PQ/HDR; `arib-std-b67` means HLG.
The website cannot read exact monitor calibration or enforce the browser's
HDR colour-management implementation.
