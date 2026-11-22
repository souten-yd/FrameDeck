"""Playback profile must never silently re-encode HDR into untagged SDR."""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from framedeck.models import VideoInfo
from framedeck.web.routers import video as video_router


def _info(transfer: str, hdr: str) -> VideoInfo:
    return VideoInfo(
        media_id="movie", container="mp4", duration_seconds=60,
        width=3840, height=2160, video_codec="hevc", audio_codec="aac",
        bitrate=10000000, frame_rate=24.0, tracks=(), chapters=(),
        direct_play=True, direct_play_reason="ok", color_transfer=transfer,
        color_primaries="bt2020" if hdr != "sdr" else "bt709",
        color_space="bt2020nc" if hdr != "sdr" else "bt709",
        hdr_format=hdr,
    )


def _decision(monkeypatch, info, *, mode="auto", display_hdr=False):
    monkeypatch.setattr(video_router, "qnap_lite", lambda: True)
    monkeypatch.setattr(video_router, "_resolve_video",
                        lambda *_: SimpleNamespace(path="/test/movie.mp4"))
    monkeypatch.setattr(video_router, "resolve_direct_play", lambda *_: SimpleNamespace(
        direct_play=True, copy_video=False, copy_audio=True, reason="ok"))
    settings = {"video_color_mode": mode, "video_stream_mode": "auto",
                "video_profile_mobile": "auto", "video_profile_desktop": "auto",
                "video_max_resolution": "auto"}
    services = SimpleNamespace(
        settings=SimpleNamespace(as_dict=lambda: settings),
        video_playback=SimpleNamespace(get_info=lambda *_: info),
    )
    return video_router.playback_profile(
        "movie",
        SimpleNamespace(client=SimpleNamespace(host="192.168.1.2")),
        {"uiProfile": "desktop", "displayHdr": display_hdr},
        services,
    )


def test_hdr_to_sdr_auto_downgrades_on_qnap(monkeypatch):
    value = _decision(monkeypatch, _info("smpte2084", "hdr10"), display_hdr=False)
    assert value["tone_map"] is True
    assert value["copy_video"] is False
    assert value["profile"]["name"] == "480p"
    assert value["profile"]["transcode"] is True


def test_hdr_display_keeps_native_video(monkeypatch):
    value = _decision(monkeypatch, _info("smpte2084", "hdr10"), display_hdr=True)
    assert value["tone_map"] is False
    assert value["profile"]["name"] == "original"
    assert value["profile"]["transcode"] is False


def test_manual_sdr_and_original_modes(monkeypatch):
    info = _info("arib-std-b67", "hlg")
    assert _decision(monkeypatch, info, mode="sdr", display_hdr=True)["tone_map"] is True
    original = _decision(monkeypatch, info, mode="original", display_hdr=False)
    assert original["tone_map"] is False
    assert original["profile"]["transcode"] is False


def test_sdr_is_unchanged_even_on_sdr_display(monkeypatch):
    value = _decision(monkeypatch, _info("bt709", "sdr"), display_hdr=False)
    assert value["tone_map"] is False
    assert value["profile"]["transcode"] is False


def test_unrecognized_hdr_transfer_fails_closed(monkeypatch):
    with pytest.raises(HTTPException) as exc:
        _decision(monkeypatch, _info("", "dolby_vision"), display_hdr=False)
    assert exc.value.status_code == 422
