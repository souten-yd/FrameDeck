"""The QPKG selects light defaults without changing the Linux profile."""
import asyncio
import threading
from types import SimpleNamespace

import pytest

from framedeck.config import Settings, resolve_app_paths
from framedeck.runtime_profile import QNAP_DEFAULTS
from framedeck.video.hls_service import HlsService
from framedeck.models import VideoInfo
from framedeck.video.transcode import TranscodeError, TranscodeService
from framedeck.web.routers import video as video_router


def test_qnap_defaults_and_linux_defaults_are_independent(tmp_path, monkeypatch):
    monkeypatch.setenv("FRAMEDECK_PROFILE", "qnap-lite")
    qnap = Settings(resolve_app_paths(tmp_path / "qnap"))
    for key, value in QNAP_DEFAULTS.items():
        assert qnap.get(key) == value
    qnap.update({"prefetch_ahead": 2})
    assert Settings(resolve_app_paths(tmp_path / "qnap")).get("prefetch_ahead") == 2

    monkeypatch.delenv("FRAMEDECK_PROFILE")
    linux = Settings(resolve_app_paths(tmp_path / "linux"))
    assert linux.get("prefetch_ahead") == 8
    assert linux.get("video_profile_mobile") == "1080p"


def test_qnap_hls_never_generates_high_resolution(tmp_path, monkeypatch):
    monkeypatch.setenv("FRAMEDECK_PROFILE", "qnap-lite")
    source = tmp_path / "source.mp4"
    source.write_bytes(b"sample")
    hls = HlsService(tmp_path / "cache")
    selected = []

    def ensure(path, **kwargs):
        selected.extend(kwargs["profiles"])
        return SimpleNamespace(key="a" * 64)

    monkeypatch.setattr(hls, "ensure_async", ensure)
    monkeypatch.setattr(video_router, "_resolve_video", lambda *_: SimpleNamespace(path=str(source)))
    services = SimpleNamespace(hls=hls, video_playback=SimpleNamespace(
        get_info=lambda *_: SimpleNamespace(height=2160)))
    asyncio.run(video_router.hls_master("movie", profile="2160p", session="", services=services))
    assert selected == ["480p"]
    hls.shutdown()


def test_qnap_fmp4_limits_resolution_and_threads():
    from framedeck.video.transcode import build_fmp4_transcode_cmd

    cmd = build_fmp4_transcode_cmd("movie.mkv", max_width=854,
                                    max_height=480, encode_threads=2)
    assert ["-threads", "2"] == cmd[cmd.index("-threads"):cmd.index("-threads") + 2]
    assert "min(iw,854)" in cmd[cmd.index("-vf") + 1]
    assert "min(ih,480)" in cmd[cmd.index("-vf") + 1]
    remux = build_fmp4_transcode_cmd("movie.mkv", copy_video=True, encode_threads=2)
    assert "-vf" not in remux and "copy" in remux


def test_qnap_profile_prefers_direct_and_caps_unsupported_video(tmp_path, monkeypatch):
    monkeypatch.setenv("FRAMEDECK_PROFILE", "qnap-lite")
    settings = Settings(resolve_app_paths(tmp_path / "qnap"))
    info = VideoInfo(
        media_id="movie", container="mp4", duration_seconds=60,
        width=3840, height=2160, video_codec="h264", audio_codec="aac",
        bitrate=8000000, frame_rate=30, tracks=(), chapters=(),
        direct_play=True, direct_play_reason="direct",
    )
    monkeypatch.setattr(video_router, "_resolve_video", lambda *_: SimpleNamespace(path="movie.mp4"))
    services = SimpleNamespace(settings=settings, video_playback=SimpleNamespace(
        get_info=lambda *_: info))
    request = SimpleNamespace(client=SimpleNamespace(host="192.168.1.10"))
    hints = {"uiProfile": "mobile", "videoCodecs": ["h264"],
             "audioCodecs": ["aac"], "containers": ["mp4"]}
    direct = video_router.playback_profile("movie", request, payload=hints, services=services)
    assert direct["profile"]["transcode"] is False
    assert direct["direct_play"] is True

    # The same 4K source cannot be decoded by this client.
    hints["videoCodecs"] = ["vp9"]
    converted = video_router.playback_profile("movie", request, payload=hints, services=services)
    assert converted["profile"]["name"] == "480p"
    assert converted["copy_video"] is False


def test_shared_video_process_gate_rejects_second_fmp4(monkeypatch):
    gate = threading.BoundedSemaphore(1)
    service = TranscodeService(max_active_streams=1, process_gate=gate)
    monkeypatch.setattr("framedeck.video.transcode.resolve_ffmpeg",
                        lambda _: SimpleNamespace(path="ffmpeg", error=None))

    class Process:
        def __init__(self):
            from io import BytesIO
            self.stdout = BytesIO()
            self.returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = -15

        def wait(self, timeout=None):
            return self.returncode

    monkeypatch.setattr("framedeck.video.transcode.subprocess.Popen",
                        lambda *args, **kwargs: Process())
    first = service.open_fmp4("first.mp4", owner="tab-1")
    with pytest.raises(TranscodeError, match="別の動画"):
        service.open_fmp4("second.mp4", owner="tab-2")
    first.close()
    second = service.open_fmp4("second.mp4", owner="tab-2")
    second.close()
