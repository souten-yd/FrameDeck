"""Adaptive video delivery profiles and playback decisions."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..models import VideoInfo

VIDEO_RESOLUTION_PROFILES: dict[str, dict[str, int]] = {
    "2160p": {"max_width": 3840, "max_height": 2160},
    "1440p": {"max_width": 2560, "max_height": 1440},
    "1080p": {"max_width": 1920, "max_height": 1080},
    "720p": {"max_width": 1280, "max_height": 720},
    "480p": {"max_width": 854, "max_height": 480},
    "360p": {"max_width": 640, "max_height": 360},
}

VIDEO_PROFILES: dict[str, dict[str, Any]] = {
    "original": {"transcode": False},
    "2160p": {"resolution": "2160p", "video_bitrate": "16000k", "audio_bitrate": "192k", "fps_limit": None, "codec": "h264"},
    "1440p": {"resolution": "1440p", "video_bitrate": "9000k", "audio_bitrate": "160k", "fps_limit": None, "codec": "h264"},
    "1080p": {"resolution": "1080p", "video_bitrate": "5000k", "audio_bitrate": "160k", "fps_limit": None, "codec": "h264"},
    "720p": {"resolution": "720p", "video_bitrate": "1800k", "audio_bitrate": "96k", "fps_limit": 30, "codec": "h264"},
    "480p": {"resolution": "480p", "video_bitrate": "850k", "audio_bitrate": "64k", "fps_limit": 30, "codec": "h264"},
    "360p": {"resolution": "360p", "video_bitrate": "450k", "audio_bitrate": "48k", "fps_limit": 24, "codec": "h264"},
}

_LEGACY_PROFILE_ALIASES = {
    "wifi_high": "1080p",
    "mobile_balanced": "720p",
    "mobile_low": "480p",
    "data_saver": "360p",
}
_PROFILE_ORDER = ["360p", "480p", "720p", "1080p", "1440p", "2160p"]


@dataclass(frozen=True)
class VideoClientHints:
    effective_type: str | None = None
    downlink_mbps: float | None = None
    save_data: bool = False
    viewport_width: int = 0
    viewport_height: int = 0
    device_pixel_ratio: float = 1.0
    measured_mbps: float | None = None
    #: navigator.connection.type ("wifi" / "ethernet" / "cellular" など)
    connection_type: str | None = None
    #: クライアントIPがプライベート帯(=同一LAN)かどうか
    local_network: bool = False
    #: ブラウザが実際に再生できるコーデック/コンテナ(canPlayTypeの申告)。
    #: 空ならサーバ側の推定にフォールバックする。
    video_codecs: tuple[str, ...] = ()
    audio_codecs: tuple[str, ...] = ()
    containers: tuple[str, ...] = ()


_WIRED_CONNECTION_TYPES = {"wifi", "ethernet", "wimax", "mixed", "other"}


def classify_network(hints: VideoClientHints) -> str:
    """回線種別を判定する: lan | cellular | slow | unknown。

    LAN(Wi-Fi/有線)は原寸配信、モバイル回線は上限付き配信に使う。
    ブラウザがNetwork Information APIを実装しない場合(Safari等)は
    クライアントIPがプライベート帯かどうかで代替判定する。
    """
    connection_type = (hints.connection_type or "").lower()
    if connection_type == "cellular":
        return "cellular"
    if hints.effective_type in {"slow-2g", "2g"}:
        return "slow"
    if connection_type in _WIRED_CONNECTION_TYPES:
        return "lan"
    if hints.effective_type == "3g":
        return "cellular"
    if hints.local_network:
        return "lan"
    return "unknown"


@dataclass(frozen=True)
class ResolvedVideoProfile:
    name: str
    transcode: bool
    height: int | None = None
    width: int | None = None
    resolution: str | None = None
    video_bitrate: str | None = None
    audio_bitrate: str | None = None
    fps_limit: int | None = None
    codec: str = "h264"
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "transcode": self.transcode,
            "height": self.height,
            "width": self.width,
            "resolution": self.resolution,
            "video_bitrate": self.video_bitrate,
            "audio_bitrate": self.audio_bitrate,
            "fps_limit": self.fps_limit,
            "codec": self.codec,
            "reason": self.reason,
        }


def canonical_video_profile(name: str | None) -> str:
    if not name or name == "auto":
        return "auto"
    return _LEGACY_PROFILE_ALIASES.get(name, name)


def resolution_box(profile: str, source_width: int = 0, source_height: int = 0) -> tuple[int | None, int | None]:
    profile = canonical_video_profile(profile)
    if profile == "original":
        return None, None
    base = VIDEO_RESOLUTION_PROFILES[profile]
    max_width = base["max_width"]
    max_height = base["max_height"]
    if source_width > 0 and source_height > source_width:
        max_width, max_height = max_height, max_width
    if source_width > 0:
        max_width = min(max_width, source_width)
    if source_height > 0:
        max_height = min(max_height, source_height)
    return max_width, max_height


def scale_filter_for_box(max_width: int | None, max_height: int | None) -> str:
    if not max_width or not max_height:
        return "scale=iw:ih"
    return (
        f"scale=w='min(iw,{max_width})':h='min(ih,{max_height})':"
        "force_original_aspect_ratio=decrease:force_divisible_by=2"
    )


def _bitrate_kbps(profile_name: str) -> int:
    profile_name = canonical_video_profile(profile_name)
    value = VIDEO_PROFILES[profile_name].get("video_bitrate", "0k")
    return int(str(value).rstrip("k"))


def resolve_video_profile(
    name: str,
    source_height: int | None = None,
    source_width: int | None = None,
) -> ResolvedVideoProfile:
    name = canonical_video_profile(name)
    if name == "auto":
        name = "1080p"
    profile = VIDEO_PROFILES.get(name)
    if not profile:
        raise ValueError(f"Unknown video profile: {name}")
    if profile.get("transcode") is False:
        return ResolvedVideoProfile(name=name, transcode=False, reason="original", resolution="original")
    max_width, max_height = resolution_box(name, source_width or 0, source_height or 0)
    return ResolvedVideoProfile(
        name=name,
        transcode=True,
        height=max_height,
        width=max_width,
        resolution=name,
        video_bitrate=profile["video_bitrate"],
        audio_bitrate=profile["audio_bitrate"],
        fps_limit=profile["fps_limit"],
        codec=profile["codec"],
    )


@dataclass(frozen=True)
class DirectPlayDecision:
    """クライアントの申告を踏まえた直接再生の可否。"""
    direct_play: bool
    #: 映像を再エンコードせずコンテナだけ入れ替えれば再生できる(remux)
    copy_video: bool = False
    #: 音声もそのまま流せる
    copy_audio: bool = False
    reason: str = ""


def resolve_direct_play(info: VideoInfo,
                        hints: VideoClientHints | None = None) -> DirectPlayDecision:
    """このクライアントで直接再生できるかを判定する。

    コーデックを再生できるかは本来クライアント固有の情報で、サーバ側の
    固定テーブルでは実態と食い違う(例: Chrome/SafariはHEVCを再生できるが
    サーバ側テーブルは非対応扱い → 不要な4K変換で再生がカクつく)。
    ブラウザからの申告がある場合はそれを優先する。
    """
    hints = hints or VideoClientHints()
    if not hints.video_codecs:
        # 申告なし(旧クライアント等)はサーバ側の推定を使う
        return DirectPlayDecision(info.direct_play, reason=info.direct_play_reason)

    video_ok = not info.video_codec or info.video_codec in hints.video_codecs
    audio_ok = not info.audio_codec or info.audio_codec in hints.audio_codecs
    container_ok = bool(info.container) and info.container in hints.containers

    if container_ok and video_ok and audio_ok:
        return DirectPlayDecision(True, reason="client-supported")
    if not video_ok:
        return DirectPlayDecision(
            False, reason=f"動画コーデック {info.video_codec} はこの端末で再生できません")
    # 映像はそのまま使える → コンテナ入れ替え(+必要なら音声のみ変換)で足りる
    detail = "コンテナ" if not container_ok else "音声"
    return DirectPlayDecision(
        False, copy_video=True, copy_audio=audio_ok,
        reason=f"{detail}のみ変換します(映像は再エンコードしません)",
    )


def original_profile(transcode: bool, reason: str = "") -> ResolvedVideoProfile:
    """原寸プロファイル。transcode=Trueでも解像度は変えない(コーデック変換のみ)。"""
    return ResolvedVideoProfile(
        name="original", transcode=transcode, resolution="original",
        video_bitrate=None, audio_bitrate="192k" if transcode else None,
        fps_limit=None, codec="h264", reason=reason,
    )


def network_resolution_limit(settings: dict[str, Any], network: str,
                             ui_profile: str = "desktop") -> str:
    """回線種別ごとの上限解像度。LAN(Wi-Fi/有線)は原寸。"""
    if network == "slow":
        return "480p"
    if network == "cellular":
        return canonical_video_profile(
            settings.get("video_cellular_max_resolution", "1080p")) or "1080p"
    if network == "lan":
        return "original"
    # 回線を判別できない外部アクセス: PCは原寸、モバイルはモバイル回線扱い
    if ui_profile == "mobile":
        return canonical_video_profile(
            settings.get("video_cellular_max_resolution", "1080p")) or "1080p"
    return "original"


def _fits_within(name: str, info: VideoInfo) -> bool:
    """ソースが上限解像度に収まっている(縮小不要)か。"""
    if name == "original":
        return True
    box = VIDEO_RESOLUTION_PROFILES[name]
    long_edge = max(info.width or 0, info.height or 0)
    short_edge = min(info.width or 0, info.height or 0)
    if not long_edge:
        return False
    return (long_edge <= box["max_width"] and short_edge <= box["max_height"])


def select_video_profile(
    settings: dict[str, Any],
    info: VideoInfo,
    hints: VideoClientHints | None = None,
    ui_profile: str = "desktop",
) -> ResolvedVideoProfile:
    hints = hints or VideoClientHints()
    mode = settings.get("video_stream_mode", "auto")
    network = classify_network(hints)
    limit = network_resolution_limit(settings, network, ui_profile)
    direct = resolve_direct_play(info, hints)

    configured = canonical_video_profile(settings.get(
        "video_profile_mobile" if ui_profile == "mobile" else "video_profile_desktop",
        "auto",
    ))
    if configured == "auto":
        configured = canonical_video_profile(settings.get("video_max_resolution", "auto"))

    if mode == "transcode":
        # 手動指定が最優先。未指定(auto)なら回線に応じた上限で変換する。
        target = configured if configured != "auto" else limit
        if target == "original":
            return original_profile(True, "mode=transcode/original")
        profile = resolve_video_profile(target, info.height, info.width)
        return ResolvedVideoProfile(**{**profile.to_dict(), "reason": "mode=transcode"})

    if hints.save_data:
        profile = resolve_video_profile("480p", info.height, info.width)
        return ResolvedVideoProfile(**{**profile.to_dict(), "reason": "saveData"})

    if mode == "original":
        # 軽量配信を無効にしていても、モバイル回線だけは上限を適用する
        target = limit if network in {"cellular", "slow"} else "original"
        reason = "mode=original" if target == "original" else f"mode=original/{network}"
    elif configured != "auto":
        target = configured                      # 明示指定(手動)を尊重する
        reason = "configured"
    else:
        target = limit
        reason = f"network={network}"

    if target == "original":
        if direct.direct_play:
            return original_profile(False, f"original/{reason}")
        if direct.copy_video:
            # コンテナ(と音声)だけ入れ替える。映像は再エンコードしない
            return original_profile(True, f"remux/{reason}")
        # 端末が映像コーデックを再生できない → 実際に再エンコードが要る。
        # 4K60などを原寸で実時間変換するのは不可能なので上限を掛ける
        return _fallback_encode_profile(info, reason)
    if direct.direct_play and _fits_within(target, info) and ui_profile != "mobile":
        # 上限内の動画をわざわざ再変換しない(直接再生が最も軽く高画質)。
        # モバイルは除く: 原寸の直接再生がiOSで安定しないため、上限を
        # 設定している間はセグメント配信(HLS)経路に寄せる
        return original_profile(False, f"direct-play-fits/{reason}")
    profile = resolve_video_profile(target, info.height, info.width)
    return ResolvedVideoProfile(**{**profile.to_dict(), "reason": reason})


#: 実時間で再エンコードできる現実的な上限(これを超える原寸変換は破綻する)
REALTIME_ENCODE_MAX = "1080p"


def _fallback_encode_profile(info: VideoInfo, reason: str) -> ResolvedVideoProfile:
    if _fits_within(REALTIME_ENCODE_MAX, info):
        return original_profile(True, f"encode/{reason}")
    profile = resolve_video_profile(REALTIME_ENCODE_MAX, info.height, info.width)
    return ResolvedVideoProfile(
        **{**profile.to_dict(), "reason": f"encode-limited/{reason}"}
    )


def scale_filter_for_height(max_height: int, source_height: int) -> str:
    target = min(max_height, source_height) if source_height > 0 else max_height
    return f"scale=-2:'min({target},ih)'"
