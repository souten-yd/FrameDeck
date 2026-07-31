"""Adaptive media delivery and comic image processing tests."""
from __future__ import annotations

from PIL import Image, ImageDraw

from framedeck.comic.crop_detector import detect_crop_box
from framedeck.comic.spread_detector import detect_spread
from framedeck.comic.virtual_pages import split_virtual_pages
from framedeck.config import resolve_comic_reader_settings
from framedeck.models import VideoInfo
from framedeck.video.job_manager import TranscodeJobManager, TranscodeKey
from framedeck.video.profile_service import VideoClientHints, select_video_profile


def _video_info(width=1920, height=1080, bitrate=8_000_000):
    return VideoInfo(
        media_id="v1",
        container="mp4",
        duration_seconds=120,
        width=width,
        height=height,
        video_codec="h264",
        audio_codec="aac",
        bitrate=bitrate,
        frame_rate=30,
        tracks=(),
        chapters=(),
        direct_play=True,
        direct_play_reason="direct",
    )


def test_mobile_and_desktop_comic_settings_are_independent():
    values = {
        "reading_direction": "rtl",
        "cover_as_single_page": True,
        "comic_auto_crop": True,
        "comic_delivery_mode": "auto",
        "comic_output_format": "auto",
        "comic_crop_white": True,
        "comic_crop_gray": True,
        "comic_crop_black": True,
        "comic_crop_tolerance": 18,
        "comic_crop_safety_margin": 4,
        "comic_spread_detection": True,
        "comic_split_spread_in_single_mode": True,
        "comic_spread_display_behavior": "auto",
        "comic_client_enhancement": "auto",
        "comic_desktop_view_mode": "spread",
        "comic_desktop_delivery_profile": "high",
        "comic_desktop_page_fit": "height",
        "comic_desktop_split_spread": False,
        "comic_desktop_client_enhancement": "off",
        "comic_mobile_view_mode": "single",
        "comic_mobile_delivery_profile": "mobile",
        "comic_mobile_page_fit": "width",
        "comic_mobile_split_spread": True,
        "comic_mobile_client_enhancement": "auto",
    }
    desktop = resolve_comic_reader_settings(values, "desktop")
    mobile = resolve_comic_reader_settings(values, "mobile")
    assert desktop["view_mode"] == "spread"
    assert desktop["delivery_profile"] == "high"
    assert desktop["client_enhancement"] == "off"
    assert mobile["view_mode"] == "single"
    assert mobile["delivery_profile"] == "mobile"
    assert mobile["split_spread"] is True


def test_video_save_data_selects_480p():
    profile = select_video_profile(
        {"video_stream_mode": "auto"},
        _video_info(),
        VideoClientHints(save_data=True, viewport_width=390, viewport_height=844),
        ui_profile="mobile",
    )
    assert profile.name == "480p"
    assert profile.transcode is True
    assert profile.height == 480


def test_video_profile_does_not_upscale():
    profile = select_video_profile(
        {"video_stream_mode": "transcode", "video_profile_mobile": "1080p"},
        _video_info(width=640, height=360),
        VideoClientHints(),
        ui_profile="mobile",
    )
    assert profile.name == "1080p"
    assert profile.height == 360


def test_same_transcode_job_is_reused():
    manager = TranscodeJobManager()
    key = TranscodeKey.from_profile("v1", 10, 200, {"height": 720})
    first, created_first = manager.get_or_create(key)
    second, created_second = manager.get_or_create(key)
    assert created_first is True
    assert created_second is False
    assert first is second


def test_crop_white_gray_black_borders():
    for color, expected in [((255, 255, 255), "white"), ((128, 128, 128), "gray"), ((0, 0, 0), "black")]:
        img = Image.new("RGB", (120, 160), color)
        draw = ImageDraw.Draw(img)
        draw.rectangle((18, 20, 101, 139), fill=(40, 40, 40) if expected != "black" else (220, 220, 220))
        detected = detect_crop_box(img, tolerance=20, safety_margin=2)
        assert detected.border_type == expected
        assert detected.crop_box is not None
        assert detected.crop_box[0] <= 18
        assert detected.crop_box[2] >= 101


def test_does_not_crop_content_near_edge():
    img = Image.new("RGB", (120, 160), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    draw.rectangle((0, 10, 80, 100), fill=(20, 20, 20))
    detected = detect_crop_box(img, tolerance=18, safety_margin=2)
    assert detected.crop_box is None or detected.crop_box[0] == 0


def test_detect_spread_with_white_gutter():
    img = Image.new("RGB", (1800, 1200), (240, 240, 240))
    draw = ImageDraw.Draw(img)
    draw.rectangle((50, 50, 850, 1150), fill=(80, 80, 80))
    draw.rectangle((950, 50, 1750, 1150), fill=(90, 90, 90))
    draw.rectangle((890, 0, 910, 1200), fill=(255, 255, 255))
    detected = detect_spread(img)
    assert detected.is_spread is True
    assert detected.split_x is not None
    assert 880 <= detected.split_x <= 920


def test_landscape_illustration_not_split_when_center_crosses():
    img = Image.new("RGB", (1800, 1200), (120, 120, 120))
    draw = ImageDraw.Draw(img)
    draw.line((0, 600, 1800, 600), fill=(255, 255, 255), width=80)
    draw.line((900, 0, 900, 1200), fill=(20, 20, 20), width=70)
    detected = detect_spread(img)
    assert detected.is_spread is False


def test_rtl_ltr_virtual_page_order():
    rtl = split_virtual_pages(3, (1000, 1400), 520, "rtl")
    ltr = split_virtual_pages(3, (1000, 1400), 520, "ltr")
    assert [page.side for page in rtl] == ["right", "left"]
    assert [page.side for page in ltr] == ["left", "right"]


def test_hls_manifest_does_not_upscale(tmp_path):
    from framedeck.video.hls_service import HlsService

    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    service = HlsService(tmp_path / "hls", segment_duration=4)
    manifest = service.manifest_for(str(source), profiles=["1080p"], source_height=360)
    assert manifest.variants[0].height == 360
    assert manifest.variants[0].bandwidth > 0
    assert manifest.master.name == "master.m3u8"


def test_hls_resolve_rejects_path_escape(tmp_path):
    from framedeck.video.hls_service import HlsService
    from framedeck.video.transcode import TranscodeError

    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    service = HlsService(tmp_path / "hls", segment_duration=4)
    manifest = service.manifest_for(str(source), profiles=["480p"], source_height=480)
    manifest.cache_dir.mkdir(parents=True)
    try:
        service.resolve_file(str(source), "../outside.m3u8", profiles=["480p"], source_height=480)
    except TranscodeError:
        pass
    else:
        raise AssertionError("path escape should be rejected")


def test_default_video_quality_is_network_adaptive(tmp_path):
    from framedeck.config import Settings, ensure_runtime_directories, resolve_app_paths

    paths = resolve_app_paths(tmp_path / "home")
    ensure_runtime_directories(paths)
    settings = Settings(paths)
    assert settings.get("video_max_resolution") == "auto"
    assert settings.get("video_profile_desktop") == "auto"
    assert settings.get("video_profile_mobile") == "auto"
    assert settings.get("video_cellular_max_resolution") == "1080p"


def test_settings_migrate_old_video_defaults_to_auto(tmp_path):
    import json

    from framedeck.config import Settings, ensure_runtime_directories, resolve_app_paths

    paths = resolve_app_paths(tmp_path / "home")
    ensure_runtime_directories(paths)
    paths.settings_file.write_text(json.dumps({
        "video_stream_mode": "original",
        "video_profile_desktop": "1080p",
        "video_profile_mobile": "720p",
        "video_max_resolution": "1080p",
        "comic_cache_max_mb": 250,
    }), "utf-8")
    settings = Settings(paths)
    assert settings.get("video_profile_desktop") == "auto"
    assert settings.get("video_profile_mobile") == "auto"
    assert settings.get("video_stream_mode") == "auto"
    assert settings.get("comic_cache_max_mb") == 250  # 他の設定は保持する
    # 明示的に選んだ値は移行後も維持される
    paths.settings_file.write_text(json.dumps({
        "settings_version": 2, "video_profile_desktop": "720p",
    }), "utf-8")
    assert Settings(paths).get("video_profile_desktop") == "720p"


def test_lan_client_gets_original_and_cellular_is_capped():
    lan = select_video_profile(
        {"video_stream_mode": "auto"},
        _video_info(width=3840, height=2160),
        VideoClientHints(connection_type="wifi", viewport_width=1920,
                         viewport_height=1080),
        ui_profile="mobile",
    )
    assert lan.name == "original"
    assert lan.transcode is False

    cellular = select_video_profile(
        {"video_stream_mode": "auto"},
        _video_info(width=3840, height=2160),
        VideoClientHints(connection_type="cellular", viewport_width=390,
                         viewport_height=844),
        ui_profile="mobile",
    )
    assert cellular.name == "1080p"
    assert cellular.transcode is True
    assert cellular.height == 1080


def test_private_ip_is_treated_as_lan_without_connection_api():
    from framedeck.web.routers.video import is_local_network_client

    assert is_local_network_client("192.168.1.20") is True
    assert is_local_network_client("127.0.0.1") is True
    assert is_local_network_client("8.8.8.8") is False
    profile = select_video_profile(
        {"video_stream_mode": "auto"},
        _video_info(width=3840, height=2160),
        VideoClientHints(local_network=True),
        ui_profile="mobile",
    )
    assert profile.name == "original"


def _unplayable(info, codec="hevc", container="mp4", audio="aac"):
    return type(info)(**{**info.__dict__, "direct_play": False,
                         "direct_play_reason": f"{codec}はサーバ判定では非対応",
                         "video_codec": codec, "container": container,
                         "audio_codec": audio})


def test_client_declared_codec_support_avoids_needless_transcode():
    """端末がHEVCを再生できるなら、サーバ判定が非対応でも変換しない。

    4K60のHEVCを実時間で再エンコードするのは不可能で、これを行うと
    再生が激しくカクつく(報告された不具合の原因)。
    """
    info = _unplayable(_video_info(width=3840, height=2160))
    hints = VideoClientHints(
        local_network=True,
        video_codecs=("h264", "hevc", "av1", "vp9"),
        audio_codecs=("aac", "opus"),
        containers=("mp4", "webm"),
    )
    profile = select_video_profile({"video_stream_mode": "auto"}, info, hints,
                                   ui_profile="desktop")
    assert profile.name == "original"
    assert profile.transcode is False


def test_container_only_mismatch_is_remuxed_not_reencoded():
    """MKVのH.264などは映像を再エンコードせずコンテナだけ入れ替える。"""
    from framedeck.video.profile_service import resolve_direct_play

    info = _unplayable(_video_info(), codec="h264", container="matroska",
                       audio="ac3")
    hints = VideoClientHints(
        local_network=True,
        video_codecs=("h264", "hevc"), audio_codecs=("aac",),
        containers=("mp4", "webm"),
    )
    decision = resolve_direct_play(info, hints)
    assert decision.direct_play is False
    assert decision.copy_video is True
    assert decision.copy_audio is False        # AC3は変換が必要
    profile = select_video_profile({"video_stream_mode": "auto"}, info, hints,
                                   ui_profile="desktop")
    assert profile.name == "original"
    assert profile.transcode is True
    assert profile.reason.startswith("remux")


def test_real_reencode_is_capped_to_a_realtime_resolution():
    """端末が本当に再生できない4K素材は、実時間で変換できる上限まで下げる。"""
    info = _unplayable(_video_info(width=3840, height=2160))
    hints = VideoClientHints(local_network=True,
                             video_codecs=("h264",), audio_codecs=("aac",),
                             containers=("mp4",))
    profile = select_video_profile({"video_stream_mode": "auto"}, info, hints,
                                   ui_profile="desktop")
    assert profile.name == "1080p"
    assert profile.transcode is True
    assert profile.height == 1080
    assert profile.reason.startswith("encode-limited")


def test_original_transcodes_when_browser_cannot_direct_play():
    """1080p以下なら原寸のまま(縮小なしで)変換する。"""
    info = _unplayable(_video_info())
    hints = VideoClientHints(local_network=True,
                             video_codecs=("h264",), audio_codecs=("aac",),
                             containers=("mp4",))
    profile = select_video_profile({"video_stream_mode": "auto"}, info, hints,
                                   ui_profile="desktop")
    assert profile.name == "original"
    assert profile.transcode is True
    assert profile.height is None  # 縮小せずコーデック変換のみ


def test_remux_command_copies_video_stream():
    from framedeck.video.transcode import build_fmp4_transcode_cmd

    cmd = build_fmp4_transcode_cmd("/tmp/a.mkv", copy_video=True, copy_audio=False)
    assert ["-c:v", "copy"] == cmd[cmd.index("-c:v"):cmd.index("-c:v") + 2]
    assert "libx264" not in cmd
    assert "-vf" not in cmd                    # スケーリングもしない
    assert ["-c:a", "aac"] == cmd[cmd.index("-c:a"):cmd.index("-c:a") + 2]
    both = build_fmp4_transcode_cmd("/tmp/a.mkv", copy_video=True, copy_audio=True)
    assert ["-c:a", "copy"] == both[both.index("-c:a"):both.index("-c:a") + 2]


def test_resolution_2160p_and_portrait_box():
    from framedeck.video.profile_service import resolve_video_profile, resolution_box, scale_filter_for_box

    profile = resolve_video_profile("2160p", source_height=2160, source_width=3840)
    assert profile.width == 3840
    assert profile.height == 2160
    portrait = resolution_box("1080p", source_width=1080, source_height=1920)
    assert portrait == (1080, 1920)
    assert "force_original_aspect_ratio=decrease" in scale_filter_for_box(*portrait)


def test_resolution_is_part_of_hls_cache_key(tmp_path):
    from framedeck.video.hls_service import HlsService

    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    service = HlsService(tmp_path / "hls", segment_duration=4)
    key_720 = service.cache_key(str(source), ["720p"], source_height=1080)
    key_1080 = service.cache_key(str(source), ["1080p"], source_height=1080)
    assert key_720 != key_1080


def test_hls_cache_key_includes_start_offset(tmp_path):
    from framedeck.video.hls_service import HlsService

    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    service = HlsService(tmp_path / "hls", segment_duration=4)
    key_head = service.cache_key(str(source), ["720p"], start_seconds=0.0)
    key_seek = service.cache_key(str(source), ["720p"], start_seconds=90.0)
    assert key_head != key_seek


def test_hls_manifest_requires_complete_marker(tmp_path):
    from framedeck.video.hls_service import HlsService

    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    service = HlsService(tmp_path / "hls", segment_duration=4)
    manifest = service.manifest_for(str(source), profiles=["480p"])
    manifest.cache_dir.mkdir(parents=True)
    manifest.master.write_text("#EXTM3U\n", "utf-8")
    # master があってもマーカーが無ければ未完成扱い
    assert service.manifest_for(str(source), profiles=["480p"]).ready is False
    (manifest.cache_dir / "complete").write_text("{}", "utf-8")
    assert service.manifest_for(str(source), profiles=["480p"]).ready is True


def test_hls_prune_removes_incomplete_and_enforces_limit(tmp_path):
    import os
    from framedeck.video.hls_service import HlsService

    cache_root = tmp_path / "hls"
    cache_root.mkdir()
    service = HlsService(cache_root, segment_duration=4)

    stale = cache_root / ("a" * 64)
    stale.mkdir()
    (stale / "master.m3u8").write_text("#EXTM3U\n", "utf-8")

    old = cache_root / ("b" * 64)
    old.mkdir()
    (old / "master.m3u8").write_text("#EXTM3U\n", "utf-8")
    (old / "data.m4s").write_bytes(b"x" * 1024)
    (old / "complete").write_text("{}", "utf-8")
    os.utime(old / "complete", (1000, 1000))

    new = cache_root / ("c" * 64)
    new.mkdir()
    (new / "master.m3u8").write_text("#EXTM3U\n", "utf-8")
    (new / "data.m4s").write_bytes(b"x" * 1024)
    (new / "complete").write_text("{}", "utf-8")

    service.prune(max_bytes=1500)
    assert not stale.exists()      # 未完成(マーカー無し)は削除
    assert not old.exists()        # 上限超過分は古い順に削除
    assert new.exists()


def test_hls_wait_for_file_rejects_bad_key_and_missing(tmp_path):
    import pytest
    from framedeck.video.hls_service import HlsService
    from framedeck.video.transcode import TranscodeError

    service = HlsService(tmp_path / "hls", segment_duration=4)
    with pytest.raises(TranscodeError):
        service.wait_for_file("../etc", "master.m3u8", timeout=0)
    key = "d" * 64
    with pytest.raises(FileNotFoundError):
        # ジョブも完成マーカーも無ければ待たずに失敗する
        service.wait_for_file(key, "master.m3u8", timeout=5)


def test_spread_crop_uses_shared_vertical_ratio():
    from framedeck.comic.image_analysis import ComicImageAnalysis
    from framedeck.comic.spread_crop_normalizer import SpreadCropNormalizer

    left = ComicImageAnalysis(1000, 1500, "white", (40, 30, 980, 1490), 0.9, False, 0, None, None, False)
    right = ComicImageAnalysis(1000, 1500, "white", (10, 90, 960, 1440), 0.9, False, 0, None, None, False)
    result = SpreadCropNormalizer().normalize(left, right, mode="shared_vertical")
    assert result.left_crop_box[1] == 30
    assert result.right_crop_box[1] == 30
    assert result.left_crop_box[3] == 1490
    assert result.right_crop_box[3] == 1490
    assert result.left_crop_box[0] == 40
    assert result.right_crop_box[0] == 10


def test_spread_output_heights_are_equal_and_width_fits():
    from framedeck.comic.spread_crop_normalizer import plan_spread_layout

    plan = plan_spread_layout((900, 1400), (800, 1200), 1000, 900)
    assert plan.left_page.output_height == plan.right_page.output_height
    assert plan.total_width <= 1000
    assert plan.left_page.output_width > 0
    assert plan.right_page.output_width > 0


def test_desktop_auto_direct_play_does_not_compress():
    profile = select_video_profile(
        {
            "video_stream_mode": "auto",
            "video_profile_desktop": "1080p",
            "video_max_resolution": "1080p",
        },
        _video_info(),
        VideoClientHints(viewport_width=1920, viewport_height=1080),
        ui_profile="desktop",
    )
    assert profile.name == "original"
    assert profile.transcode is False
    assert profile.reason.startswith("direct-play-fits")


def test_ffmpeg_auto_download_setting_defaults_enabled(tmp_path):
    from framedeck.config import Settings, ensure_runtime_directories, resolve_app_paths

    paths = resolve_app_paths(tmp_path / "home")
    ensure_runtime_directories(paths)
    settings = Settings(paths)
    assert settings.get("video_ffmpeg_auto_download") is True


def test_progressive_fmp4_command_is_mobile_compatible():
    from framedeck.video.transcode import build_fmp4_transcode_cmd

    cmd = build_fmp4_transcode_cmd(
        "/tmp/source.mkv", max_width=1280, max_height=720, ffmpeg_bin="/opt/ffmpeg"
    )
    assert cmd[0] == "/opt/ffmpeg"
    assert ["-profile:v", "baseline"] == cmd[cmd.index("-profile:v"):cmd.index("-profile:v") + 2]
    assert ["-pix_fmt", "yuv420p"] == cmd[cmd.index("-pix_fmt"):cmd.index("-pix_fmt") + 2]
    assert ["-tag:v", "avc1"] == cmd[cmd.index("-tag:v"):cmd.index("-tag:v") + 2]
    assert "+frag_keyframe+empty_moov+default_base_moof" in cmd
    assert "force_original_aspect_ratio=decrease" in cmd[cmd.index("-vf") + 1]


def test_transcode_stream_is_replaced_per_client_session():
    """同じタブからの新しい要求は、古い変換プロセスを止めてから始まる。"""
    import subprocess

    from framedeck.video.transcode import TranscodeService

    service = TranscodeService()
    started = []

    class FakeProcess:
        def __init__(self):
            self.returncode = None
            self.stdout = None
            self.stderr = None
            self.terminated = False

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True
            self.returncode = -15

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            self.returncode = -9

    def fake_popen(cmd, **kwargs):
        process = FakeProcess()
        started.append(process)
        return process

    original_popen = subprocess.Popen
    subprocess.Popen = fake_popen
    try:
        first = service.open_fmp4("/tmp/a.mkv", owner="tab1")
        second = service.open_fmp4("/tmp/a.mkv", start_seconds=30, owner="tab1")
        other = service.open_fmp4("/tmp/a.mkv", owner="tab2")
    finally:
        subprocess.Popen = original_popen

    assert started[0].terminated is True     # 同一タブの古い変換は停止
    assert started[1].terminated is False
    assert started[2].terminated is False    # 別タブは影響を受けない
    second.close()
    assert started[1].terminated is True
    assert service.cancel_owner("tab2") is True
    assert started[2].terminated is True
    assert other._closed is True
    assert first._closed is True


def test_original_transcode_command_does_not_scale():
    from framedeck.video.transcode import build_fmp4_transcode_cmd

    cmd = build_fmp4_transcode_cmd("/tmp/source.mkv", max_width=None, max_height=None)
    assert cmd[cmd.index("-vf") + 1] == "scale=iw:ih"


def test_hls_start_is_serialized_per_source(tmp_path, monkeypatch):
    """シーク連打で同一ソースのHLSジョブが多重起動しないこと。"""
    import threading

    from framedeck.video.hls_service import HlsService

    source = tmp_path / "movie.mp4"
    source.write_bytes(b"x" * 1024)
    service = HlsService(tmp_path / "cache", segment_duration=4)
    monkeypatch.setattr(service, "available", lambda: True)
    monkeypatch.setattr(service, "_generate",
                        lambda *a, **k: threading.Event().wait(0.5))

    threads = [threading.Thread(target=service.ensure_async,
                                args=(str(source), ["720p"], 1080, float(i)))
               for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    with service._lock:
        alive = [job for job in service._jobs.values() if not job.cancelled]
    assert len(alive) <= 1   # 停止要求されていない生成は常に1本以下
    service.shutdown()
