"""動画コンテナ判定のテスト。"""
from framedeck.video.probe import _canonical_container, _direct_play_decision


def test_mp4_alias_list_is_direct_play():
    # ffprobeのmp4は "mov,mp4,m4a,3gp,3g2,mj2" の別名リストで返る
    names = {"mov", "mp4", "m4a", "3gp", "3g2", "mj2"}
    direct, reason = _direct_play_decision(names, "h264", "aac")
    assert direct is True
    assert _canonical_container(names) == "mp4"


def test_mkv_is_not_direct_play():
    names = {"matroska", "webm"}
    direct, reason = _direct_play_decision(names, "h264", "aac")
    assert direct is False
    assert "MKV" in reason
    assert _canonical_container(names) == "matroska"


def test_pure_webm_is_direct_play():
    direct, _ = _direct_play_decision({"webm"}, "vp9", "opus")
    assert direct is True


def test_unsupported_codec_rejected():
    direct, reason = _direct_play_decision(
        {"mov", "mp4"}, "hevc", "aac")
    assert direct is False
    assert "hevc" in reason

    direct, reason = _direct_play_decision(
        {"mov", "mp4"}, "h264", "dts")
    assert direct is False
    assert "dts" in reason


def test_avi_rejected():
    direct, reason = _direct_play_decision({"avi"}, "mpeg4", "mp3")
    assert direct is False
