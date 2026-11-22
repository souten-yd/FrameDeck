"""HDR/SDR metadata, passthrough and explicit tone mapping regressions."""
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from framedeck.models import VideoInfo
from framedeck.video import probe
from framedeck.video.color import check_tonemap_filters, detect_hdr_format, hdr_to_sdr_filters
from framedeck.video.hls_service import HlsService
from framedeck.video.transcode import TranscodeError, build_fmp4_transcode_cmd


@pytest.mark.parametrize(("transfer", "expected"), [
    ("smpte2084", "hdr10"),
    ("arib-std-b67", "hlg"),
    ("bt709", "sdr"),
    ("", "unknown"),
])
def test_transfer_metadata_detected(transfer, expected):
    assert detect_hdr_format({"color_transfer": transfer}) == expected


def test_dolby_vision_tag_not_mistaken_for_hdr10():
    assert detect_hdr_format({
        "color_transfer": "smpte2084",
        "side_data_list": [{"side_data_type": "DOVI configuration record"}],
    }) == "dolby_vision"


def test_probe_exposes_colour_metadata(monkeypatch):
    data = {
        "format": {"format_name": "matroska", "duration": "4"},
        "streams": [{"index": 0, "codec_type": "video", "codec_name": "hevc",
                     "width": 1920, "height": 1080, "avg_frame_rate": "24/1",
                     "color_primaries": "bt2020", "color_transfer": "smpte2084",
                     "color_space": "bt2020nc", "color_range": "tv"}],
    }
    monkeypatch.setattr(probe, "ffprobe_available", lambda: True)
    monkeypatch.setattr(probe.subprocess, "run",
                        lambda *a, **kw: SimpleNamespace(
                            stdout=json.dumps(data).encode("utf-8")))
    info = probe.probe_video("/nonexistent.mkv", "a")
    assert info.hdr_format == "hdr10"
    assert info.color_primaries == "bt2020"
    assert info.color_space == "bt2020nc"
    assert info.to_dict()["color_transfer"] == "smpte2084"


def test_old_video_info_constructor_remains_compatible():
    info = VideoInfo(
        media_id="x", container="mp4", duration_seconds=1.0,
        width=1, height=1, video_codec="h264", audio_codec="aac",
        bitrate=0, frame_rate=30.0, tracks=(), chapters=(),
        direct_play=True, direct_play_reason="ok",
    )
    assert info.hdr_format == "unknown"
    assert info.to_dict()["hdr_format"] == "unknown"


def test_hdr_fmp4_emits_bt709_tags_and_tonemap():
    cmd = build_fmp4_transcode_cmd(
        "hdr.mkv", max_height=720, max_width=1280, tone_map=True
    )
    filters = cmd[cmd.index("-vf") + 1]
    assert "zscale=t=linear:npl=100" in filters
    assert "tonemap=tonemap=hable" in filters
    assert "zscale=p=bt709:t=bt709:m=bt709" in filters
    assert cmd[cmd.index("-color_trc") + 1] == "bt709"
    assert cmd[cmd.index("-color_primaries") + 1] == "bt709"
    assert cmd[cmd.index("-colorspace") + 1] == "bt709"
    assert cmd[cmd.index("-color_range") + 1] == "tv"


def test_sdr_or_original_remux_never_tonemaps():
    for options in ({}, {"copy_video": True, "copy_audio": False}):
        cmd = build_fmp4_transcode_cmd("film.mp4", **options)
        assert "tonemap=" not in " ".join(cmd)
        assert "-color_trc" not in cmd
    with pytest.raises(TranscodeError):
        build_fmp4_transcode_cmd("hdr.mkv", tone_map=True, copy_video=True)


def test_hls_colorspace_is_in_cache_key(tmp_path):
    source = tmp_path / "hdr.mkv"
    source.write_bytes(b"placeholder")
    hls = HlsService(tmp_path / "cache")
    normal = hls.cache_key(str(source), ["720p"], 720, 0.0, False)
    to_sdr = hls.cache_key(str(source), ["720p"], 720, 0.0, True)
    assert normal != to_sdr
    assert hls.manifest_for(str(source), ["720p"], 720, 0.0, True).key == to_sdr


def test_hls_tonemap_and_copy_original_are_mutually_exclusive(tmp_path):
    service = HlsService(tmp_path / "hls")
    cmd = service._ffmpeg_cmd(
        "/movie.mkv", tmp_path, 1280, 720, "1800k", "96k", 30,
        tone_map=True,
    )
    assert "tonemap=tonemap=hable" in cmd[cmd.index("-vf") + 1]
    assert cmd[cmd.index("-color_trc") + 1] == "bt709"
    cmd = service._ffmpeg_cmd(
        "/movie.mkv", tmp_path, None, None, "0k", "96k", None,
        copy_video=True,
    )
    assert "-vf" not in cmd
    with pytest.raises(TranscodeError):
        service._ffmpeg_cmd(
            "/movie.mkv", tmp_path, 1280, 720, "1800k", "96k", 30,
            tone_map=True, copy_video=True,
        )


def test_missing_hdr_filters_fail_explicitly(monkeypatch):
    from framedeck.video import color
    color.check_tonemap_filters.cache_clear()
    monkeypatch.setattr(color.subprocess, "run",
                        lambda *a, **kw: SimpleNamespace(
                            stdout=" .. zscale V->V\n", returncode=0))
    with pytest.raises(TranscodeError, match="zscale/tonemap"):
        color.check_tonemap_filters("missing-filters")
    color.check_tonemap_filters.cache_clear()


@pytest.mark.parametrize("transfer", ["smpte2084", "arib-std-b67"])
def test_ffmpeg_hdr_filter_graph_decodes_one_frame(transfer):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("FFmpeg unavailable")
    try:
        check_tonemap_filters(ffmpeg)
    except TranscodeError:
        pytest.skip("FFmpeg built without zscale")
    vf = (
        "setparams=color_primaries=bt2020:color_trc="
        + transfer + ":colorspace=bt2020nc,"
        + ",".join(hdr_to_sdr_filters())
    )
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", "testsrc2=size=64x64:rate=1", "-vf", vf,
         "-frames:v", "1", "-f", "null", "-"],
        capture_output=True, timeout=15, check=False,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
