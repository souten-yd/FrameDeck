"""Audio compatibility and low-CPU video paths for iPad Safari and NAS."""
from pathlib import Path

from framedeck.models import VideoInfo
from framedeck.video.hls_service import HlsService
from framedeck.video.profile_service import VideoClientHints, resolve_direct_play


def _video(audio_codec: str) -> VideoInfo:
    return VideoInfo(
        media_id="example", container="mp4", duration_seconds=60,
        width=1920, height=1080, video_codec="h264", audio_codec=audio_codec,
        bitrate=8_000_000, frame_rate=30.0, tracks=(), chapters=(),
        direct_play=True, direct_play_reason="client-compatible",
    )


def _hints(ipad: bool) -> VideoClientHints:
    return VideoClientHints(
        video_codecs=("h264", "hevc"),
        audio_codecs=("aac", "mp3", "ac3", "flac"),
        containers=("mp4",),
        prefer_aac_audio=ipad,
    )


def test_ipad_ac3_audio_triggers_audio_only_conversion():
    # A positive canPlayType claim is not a guarantee against silent playback.
    standard = resolve_direct_play(_video("ac3"), _hints(ipad=False))
    ipad = resolve_direct_play(_video("ac3"), _hints(ipad=True))
    assert standard.direct_play is True
    assert ipad.direct_play is False
    assert ipad.copy_video is True
    assert ipad.copy_audio is False

    flac = resolve_direct_play(_video("flac"), _hints(ipad=True))
    assert flac.copy_video is True and flac.copy_audio is False


def test_ipad_aac_can_still_stream_unmodified():
    for codec in ("aac", "mp3", ""):
        assert resolve_direct_play(_video(codec), _hints(ipad=True)).direct_play is True


def test_ios_without_codec_hints_still_requires_aac_for_ac3():
    fallback = resolve_direct_play(_video("ac3"), VideoClientHints(prefer_aac_audio=True))
    assert fallback.direct_play is False


def test_hls_original_aac_copies_video_and_encodes_audio(tmp_path):
    source = tmp_path / "sample.mp4"
    source.write_bytes(b"fake input; manifest does not inspect media content")
    service = HlsService(tmp_path / "hls", segment_duration=2)
    manifest = service.manifest_for(str(source), profiles=["original-aac"], source_height=1080)
    assert [v.name for v in manifest.variants] == ["original-aac"]
    assert manifest.variants[0].width == 0  # Unknown, not invented in playlist
    manifest.cache_dir.mkdir(parents=True, exist_ok=True)
    service._write_master(manifest)
    assert "#EXT-X-STREAM-INF:BANDWIDTH=" in manifest.master.read_text()
    assert "RESOLUTION=" not in manifest.master.read_text()
    assert "original-aac/playlist.m3u8" in manifest.master.read_text()

    cmd = service._ffmpeg_cmd(
        str(source), tmp_path, None, None, "0k", "192k", None,
        copy_video=True,
    )
    assert cmd[cmd.index("-c:v") + 1] == "copy"
    assert cmd[cmd.index("-c:a") + 1] == "aac"
    assert "-vf" not in cmd
    assert "-force_key_frames" not in cmd
    assert "-b:v" not in cmd


def test_compressed_hls_retains_video_encoding(tmp_path):
    service = HlsService(tmp_path / "hls")
    cmd = service._ffmpeg_cmd(
        "/source.mp4", tmp_path, 1280, 720, "1800k", "96k", 30,
    )
    assert cmd[cmd.index("-c:v") + 1] == "libx264"
    assert cmd[cmd.index("-c:a") + 1] == "aac"
    assert "-vf" in cmd and "-force_key_frames" in cmd


def test_original_audio_profile_not_in_auto_hls_ladder(tmp_path):
    source = tmp_path / "sample.mp4"
    source.write_bytes(b"mock")
    service = HlsService(tmp_path / "hls")
    auto = service.manifest_for(str(source), profiles=["720p"])
    audio_fixed = service.manifest_for(str(source), profiles=["original-aac"])
    assert audio_fixed.key != auto.key
    assert [v.name for v in auto.variants] == ["720p"]


def test_hls_copy_audio_route_is_exposed_to_ios():
    router = (Path(__file__).resolve().parents[1] /
              "framedeck/web/routers/video.py").read_text("utf-8")
    assert 'if profile == "original-aac":' in router
    js = (Path(__file__).resolve().parents[1] /
          "framedeck/web/static/js/app.js").read_text("utf-8")
    assert 'audioOnlyFix ? "original-aac"' in js
