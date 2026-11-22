"""動画API。Range Request直接配信と変換ストリーミング。

配信系エンドポイントはすべて async で実装する。同期defにすると生成待ち
(HLSセグメント待ち)やストリーム送出がスレッドプールを占有し、枯渇すると
アプリ全体が無応答になるため。
"""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import logging
import os
import threading
import time
from email.utils import formatdate
from queue import Empty, Full, Queue

import anyio
import anyio.to_thread
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse

from ...core.security import PathValidationError
from ...core.services import Services
from ...models import MediaItem
from ...video.color import HDR_TRANSFERS
from ...runtime_profile import QNAP_TRANSCODE_HEIGHT, QNAP_TRANSCODE_WIDTH, qnap_lite
from ...video.stream import (
    CHUNK_SIZE as FILE_CHUNK_SIZE,
    RangeNotSatisfiable,
    mime_for_video,
    parse_range_header,
)
from ...video.hls_service import HLS_PROFILES
from ...video.profile_service import (
    VideoClientHints,
    canonical_video_profile,
    classify_network,
    original_profile,
    resolve_direct_play,
    resolve_video_profile,
    select_video_profile,
)
from ...video.transcode import TranscodeError, video_thumbnail
from ...worker_hls import (
    hls_eligible, open_remote_hls_file, start_remote_hls,
)
from ...worker_offload import (
    WorkerOffloadError, offload_enabled, owner_source_ref,
    remote_eligible, open_remote_fmp4,
)
from ..dependencies import get_services

logger = logging.getLogger("framedeck")

router = APIRouter(prefix="/api/videos", tags=["video"])

#: 配信・変換用の専用スレッド枠。既定のスレッドプールを使うと、長時間の
#: ストリーム送出や生成待ちが他のAPI(一覧・設定・漫画ページ)を待たせる。
_STREAM_LIMITER = anyio.CapacityLimiter(24)

def _hls_profiles(profile: str | None) -> list[str]:
    if profile and profile in HLS_PROFILES:
        return [profile]
    return list(HLS_PROFILES)


def _hls_media_type(path: str) -> str:
    if path.endswith(".m3u8"):
        return "application/vnd.apple.mpegurl"
    if path.endswith(".mp4") or path.endswith(".m4s"):
        return "video/mp4"
    return "application/octet-stream"


def is_local_network_client(host: str | None) -> bool:
    """クライアントが同一LAN(プライベート帯)からのアクセスかどうか。

    Network Information API 非対応ブラウザ(Safari等)で回線種別を
    推定するための代替シグナル。
    """
    if not host:
        return False
    if host in ("localhost", "testclient"):
        return True
    try:
        address = ipaddress.ip_address(host.split("%")[0])
    except ValueError:
        return False
    return bool(address.is_private or address.is_loopback or address.is_link_local)


def _codec_list(payload: dict, key: str) -> tuple[str, ...]:
    """ブラウザが申告した再生可能コーデック/コンテナ。"""
    values = payload.get(key) or []
    if not isinstance(values, list):
        return ()
    return tuple(str(v).lower() for v in values if v)


def _resolve_video(services: Services, media_id: str) -> MediaItem:
    item = services.library.get_item(media_id)
    if item is None or item.media_type != "video":
        raise HTTPException(status_code=404, detail="動画が見つかりません")
    try:
        services.library.validate_path(item.path)
    except PathValidationError as e:
        raise HTTPException(status_code=403, detail=str(e))
    return item


def _worker_ready(services, path: str, *, hls: bool = False) -> bool:
    try:
        root, _ = owner_source_ref(path, services.library.list_roots())
        ready = remote_eligible(services.cluster, root, services.settings)
        return ready and (not hls or (
            services.remote_hls.eligible() and
            services.cluster.status().get("capabilities", {}).get("remote_hls") is True))
    except (WorkerOffloadError, OSError, ValueError):
        return False


@router.get("/{media_id}")
def video_detail(media_id: str,
                 services: Services = Depends(get_services)) -> dict:
    item = _resolve_video(services, media_id)
    info = services.video_playback.get_info(item.path, media_id)
    progress = services.video_playback.get_progress(media_id)
    resume = 0.0
    if services.settings.get("resume_playback", True):
        resume = services.video_playback.resume_position(media_id)
    services.library.mark_opened(item)
    remote_available = False
    if offload_enabled(services.settings):
        try:
            root, _ = owner_source_ref(item.path, services.library.list_roots())
            remote_available = remote_eligible(services.cluster, root, services.settings)
        except WorkerOffloadError:
            pass
    return {
        "item": {
            "id": item.id,
            "display_name": item.display_name,
            "rating": item.rating,
            "size": item.size,
        },
        "info": info.to_dict(),
        "progress": progress,
        "resume_position": resume,
        "transcode_available": remote_available or services.transcode.available(),
        "worker_transcode_available": remote_available,
        "worker_hls_available": _worker_ready(services, item.path, hls=True),
        "ffmpeg_status": services.transcode.ffmpeg_status(),
    }


@router.post("/{media_id}/playback-profile")
def playback_profile(media_id: str, request: Request,
                     payload: dict = Body(default={}),
                     services: Services = Depends(get_services)) -> dict:
    item = _resolve_video(services, media_id)
    info = services.video_playback.get_info(item.path, media_id)
    hints = VideoClientHints(
        effective_type=payload.get("effectiveType"),
        downlink_mbps=payload.get("downlink"),
        save_data=bool(payload.get("saveData", False)),
        viewport_width=int(payload.get("viewportWidth") or 0),
        viewport_height=int(payload.get("viewportHeight") or 0),
        device_pixel_ratio=float(payload.get("devicePixelRatio") or 1.0),
        measured_mbps=payload.get("measuredMbps"),
        connection_type=payload.get("connectionType"),
        local_network=is_local_network_client(
            request.client.host if request.client else None),
        video_codecs=_codec_list(payload, "videoCodecs"),
        audio_codecs=_codec_list(payload, "audioCodecs"),
        containers=_codec_list(payload, "containers"),
        prefer_aac_audio=bool(payload.get("preferAacAudio", False)),
    )
    settings = services.settings.as_dict()
    requested = payload.get("requestedProfile")
    ui = payload.get("uiProfile") or "desktop"
    configured = settings.get("video_profile_mobile" if ui == "mobile" else "video_profile_desktop", "auto")
    if configured == "auto":
        configured = settings.get("video_max_resolution", "auto")
    worker_required = (requested if requested and requested != "auto" else configured) == "1080p-ext"
    if worker_required and not _worker_ready(services, item.path, hls=ui == "mobile"):
        raise HTTPException(status_code=409, detail="1080p (ext) のLinuxワーカーを利用できません。別の画質を選んでください")
    direct = resolve_direct_play(info, hints)
    if worker_required:
        profile = resolve_video_profile("1080p", info.height, info.width)
    elif qnap_lite():
        # Auto means original on every connection, including mobile/saveData.
        # Only a chosen quality or "always transcode" may resize by choice.
        ui = payload.get("uiProfile") or "desktop"
        configured = settings.get("video_profile_mobile" if ui == "mobile" else "video_profile_desktop", "auto")
        if configured == "auto":
            configured = settings.get("video_max_resolution", "auto")
        selected = canonical_video_profile(requested if requested and requested != "auto" else configured)
        if selected == "auto" and settings.get("video_stream_mode") == "transcode":
            selected = "720p"
        if selected in {"2160p", "1080p"}:
            selected = "720p"
        if selected in {"720p", "480p", "360p"}:
            profile = resolve_video_profile(selected, info.height, info.width)
        elif direct.direct_play:
            profile = original_profile(False, "qnap/original-direct")
        elif direct.copy_video:
            profile = original_profile(True, "qnap/original-remux")
        else:
            # An unplayable codec needs a bounded software encode. This is
            # the sole automatic resize; a 4K source is too costly at 720p.
            profile = resolve_video_profile("480p", info.height, info.width)
    elif requested == "original":
        # 手動で原寸を選んだ場合は回線判定より優先する
        profile = original_profile(not direct.direct_play, "requested=original")
    else:
        if requested and requested != "auto":
            settings = dict(settings)
            if (payload.get("uiProfile") or "desktop") == "mobile":
                settings["video_profile_mobile"] = requested
            else:
                settings["video_profile_desktop"] = requested
            settings["video_stream_mode"] = "transcode"
        profile = select_video_profile(
            settings,
            info,
            hints,
            ui_profile=payload.get("uiProfile") or "desktop",
        )
    # A browser can report HDR output only, not exact display calibration.
    # Preserve compatible native/remux HDR; all HDR re-encodes must become BT.709.
    hdr_source = info.hdr_format in {"hdr10", "hlg", "dolby_vision"}
    color_mode = settings.get("video_color_mode", "auto")
    hdr_display = payload.get("displayHdr") is True
    must_encode = profile.transcode and not (
        profile.name == "original" and direct.copy_video
    )
    tone_map = hdr_source and (
        color_mode == "sdr" or
        (color_mode == "auto" and not hdr_display) or
        must_encode or (not direct.direct_play and not direct.copy_video)
    )
    if tone_map:
        if info.color_transfer not in HDR_TRANSFERS:
            raise HTTPException(
                status_code=422,
                detail="HDRの転送関数が不明です。Dolby Vision単独素材は安全にSDR変換できません",
            )
        if not profile.transcode or profile.name == "original":
            profile = resolve_video_profile(
                "480p" if qnap_lite() else "1080p",
                info.height, info.width,
            )
        # Even if a codec is copyable, tone mapping requires decoding.
    copy_video = direct.copy_video and profile.transcode and profile.name == "original" and not tone_map
    return {
        "profile": profile.to_dict(),
        "worker_required": worker_required,
        "processing_node": "linux-worker" if worker_required else None,
        "network": classify_network(hints),
        # 端末の申告を反映した判定(サーバ側の固定テーブルより優先)
        "direct_play": direct.direct_play,
        "direct_play_reason": direct.reason or info.direct_play_reason,
        # 映像を再エンコードせずコンテナだけ入れ替えれば足りるか
        "copy_video": copy_video,
        "tone_map": bool(tone_map),
        "color_mode": color_mode,
        "display_hdr": hdr_display,
        "copy_audio": direct.copy_audio and profile.transcode,
        "server_direct_play": info.direct_play,
    }


@router.get("/capabilities/encoders")
def encoder_capabilities(services: Services = Depends(get_services)) -> list[dict]:
    auto_download = bool(services.settings.get("video_ffmpeg_auto_download", True))
    return [cap.__dict__ for cap in services.encoder_capabilities.detect(auto_download)]


@router.get("/{media_id}/hls/master.m3u8")
async def hls_master(media_id: str,
                     worker_only: bool = False,
                     profile: str | None = Query(default=None),
                     tone_map: bool = Query(default=False),
                     start: float = Query(default=0.0, ge=0.0),
                     session: str = Query(default=""),
                     services: Services = Depends(get_services)):
    """HLS生成を開始し、キャッシュキー付きマスターURLへリダイレクトする。

    playlist/segment の相対URLをキー付きパスで解決させるため、
    直接配信ではなくリダイレクトを返す。`start` 指定で途中からの
    再生成(シーク)に対応する。
    """
    item = _resolve_video(services, media_id)
    info = services.video_playback.get_info(item.path, media_id)
    if tone_map and info.color_transfer not in HDR_TRANSFERS:
        raise HTTPException(status_code=422, detail="HDR素材ではないか色メタデータが不足しています")
    if tone_map and profile == "original-aac":
        raise HTTPException(status_code=422, detail="音声のみ変換とHDR→SDRは同時指定できません")
    if worker_only:
        if profile != "1080p" or not _worker_ready(services, item.path, hls=True):
            raise HTTPException(status_code=503, detail="1080p (ext) のLinuxワーカーを利用できません")
        profiles = ["1080p"]
    elif profile == "original-aac":
        # Compatible video is copied unchanged; only the soundtrack is AAC.
        profiles = ["original-aac"]
    else:
        profiles = ([profile] if profile in {"360p", "480p", "720p"} else ["720p"]) if qnap_lite() else _hls_profiles(profile)
    # Desktop and iPad use the SAME browser-facing A origin. Native HLS
    # (including original-aac audio repair) can be generated by Linux B,
    # provided B has explicitly mapped the source read-only and was paired.
    # Never delegate direct Range playback, and avoid using a slow B for
    # new sessions after measured segment-delivery failures.
    if (getattr(services, "remote_hls", None) is not None and
            services.remote_hls.eligible() and
            offload_enabled(getattr(services, "settings", None))):
        try:
            source_root, relative = owner_source_ref(
                item.path, services.library.list_roots(),
            )
            if hls_eligible(services.cluster, source_root, services.settings):
                # Ordinary profiles retain local caps. External 1080p is
                # worker-only and refuses unsafe NAS fallback below.
                worker_profiles = profiles
                worker_key = await anyio.to_thread.run_sync(
                    lambda: start_remote_hls(
                        services.cluster, source_root, relative,
                        worker_profiles, info.height, start,
                        session.strip()[:64], tone_map,
                    ),
                    limiter=_STREAM_LIMITER,
                )
                proxy = services.remote_hls.add(
                    worker_key, media_id, item.path, worker_profiles,
                    info.height, start, session.strip()[:64], tone_map,
                )
                return RedirectResponse(
                    f"/api/videos/{media_id}/hls/{proxy.proxy_key}/master.m3u8",
                    status_code=302,
                    headers={"Cache-Control": "no-store",
                             "X-FrameDeck-Processing-Node": "linux-worker"},
                )
        except (WorkerOffloadError, OSError, ValueError, TimeoutError) as exc:
            services.cluster.mark_unhealthy()
            logger.info("Linux HLS start failed; use QNAP: %s",
                        type(exc).__name__)

    if worker_only:
        raise HTTPException(status_code=503, detail="Linuxワーカーでの1080p変換に失敗しました")
    try:
        # 旧ジョブの停止待ちを含むため、専用スレッド枠で実行する
        manifest = await anyio.to_thread.run_sync(
            lambda: services.hls.ensure_async(
                item.path, profiles=profiles,
                source_height=info.height, start_seconds=start,
                owner=session.strip()[:64] or None, tone_map=tone_map,
            ),
            limiter=_STREAM_LIMITER,
        )
    except TranscodeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return RedirectResponse(
        f"/api/videos/{media_id}/hls/{manifest.key}/master.m3u8",
        status_code=302,
        headers={"Cache-Control": "no-store"},
    )


async def _hls_wait_file(services: Services, key: str, relative: str,
                         timeout: float) -> "os.PathLike[str]":
    """生成待ちをイベントループ側で行う(スレッドプールを占有しない)。"""
    deadline = asyncio.get_running_loop().time() + max(0.0, timeout)
    while True:
        try:
            return services.hls.wait_for_file(key, relative, timeout=0.0)
        except FileNotFoundError:
            raise HTTPException(status_code=404,
                                detail="HLSキャッシュが見つかりません")
        except TimeoutError:
            pass
        except TranscodeError as e:
            raise HTTPException(status_code=404, detail=str(e))
        if asyncio.get_running_loop().time() >= deadline:
            raise HTTPException(
                status_code=503, detail="HLS生成中です",
                headers={"Retry-After": "1"},
            )
        await asyncio.sleep(0.15)


async def _aiter_remote_hls(response, controller, remote_hls, relative: str,
                           since: float, segment_seconds: int):
    size = 0
    complete = False
    try:
        reader = getattr(response, "read1", response.read)
        while True:
            chunk = await anyio.to_thread.run_sync(
                reader, 128 * 1024, abandon_on_cancel=True,
                limiter=_STREAM_LIMITER,
            )
            if not chunk:
                complete = True
                break
            size += len(chunk)
            yield chunk
    except Exception as exc:
        controller.mark_unhealthy()
        logger.warning("Remote HLS interrupted; next request uses NAS: %s",
                       type(exc).__name__)
    finally:
        response.close()
        if complete and relative.endswith(".m4s"):
            remote_hls.record_segment(
                size, time.monotonic()-since, float(segment_seconds),
            )


async def _serve_hls_worker_or_fallback(
        services: Services, media_id: str, proxy_key: str,
        relative: str, *, wait_timeout: float):
    item = services.remote_hls.get(media_id, proxy_key)
    if item is None:
        return None  # Ordinary HLS key from the NAS local HlsService.

    if services.cluster.verified_peer() is not None:
        since = time.monotonic()
        try:
            stream = await anyio.to_thread.run_sync(
                lambda: open_remote_hls_file(
                    services.cluster, item.worker_key, relative,
                ),
                limiter=_STREAM_LIMITER,
            )
            return StreamingResponse(
                _aiter_remote_hls(
                    stream, services.cluster, services.remote_hls, relative,
                    since, services.hls.segment_duration,
                ),
                media_type=_hls_media_type(relative),
                headers={
                    "Cache-Control": "no-store",
                    "X-FrameDeck-Processing-Node": "linux-worker",
                },
            )
        except (WorkerOffloadError, OSError, ValueError, TimeoutError) as exc:
            services.cluster.mark_unhealthy()
            logger.info("Remote HLS segment failed, using local HLS: %s",
                        type(exc).__name__)

    if qnap_lite() and any(p in {"1080p", "2160p"} for p in item.profiles):
        raise HTTPException(status_code=503, detail="外部1080pワーカーが切断されました。720p以下を選んでください")

    # Reconstruct the SAME profile / start time locally. This keeps the
    # browser playlist URL stable when B powers down; ffmpeg on A is still
    # governed by the QNAP-lite process gate and resource limits.
    try:
        local = await anyio.to_thread.run_sync(
            lambda: services.hls.ensure_async(
                item.source_path, profiles=list(item.profiles),
                source_height=item.height, start_seconds=item.start,
                owner=item.owner or None, tone_map=item.tone_map,
            ),
            limiter=_STREAM_LIMITER,
        )
        path = await _hls_wait_file(
            services, local.key, relative, timeout=wait_timeout,
        )
    except TranscodeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return FileResponse(
        path, media_type=_hls_media_type(relative),
        headers={"Cache-Control": "no-store",
                 "X-FrameDeck-Processing-Node": "qnap-fallback"},
    )


@router.get("/{media_id}/hls/{key}/master.m3u8")
async def hls_master_file(media_id: str, key: str,
                          services: Services = Depends(get_services)):
    _resolve_video(services, media_id)
    remote = await _serve_hls_worker_or_fallback(
        services, media_id, key, "master.m3u8", wait_timeout=5.0,
    )
    if remote is not None:
        return remote
    path = await _hls_wait_file(services, key, "master.m3u8", timeout=5.0)
    return FileResponse(
        path,
        media_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/{media_id}/hls/{key}/{profile}/{segment}")
async def hls_media_file(media_id: str, key: str, profile: str, segment: str,
                         services: Services = Depends(get_services)):
    if profile not in HLS_PROFILES and profile != "original-aac":
        raise HTTPException(status_code=404, detail="HLS profile が見つかりません")
    if "/" in segment or ".." in segment or segment.startswith("."):
        raise HTTPException(status_code=404, detail="HLS segment が見つかりません")
    _resolve_video(services, media_id)
    remote = await _serve_hls_worker_or_fallback(
        services, media_id, key, f"{profile}/{segment}", wait_timeout=10.0,
    )
    if remote is not None:
        return remote
    path = await _hls_wait_file(services, key, f"{profile}/{segment}", timeout=10.0)
    if segment.endswith(".m3u8"):
        headers = {"Cache-Control": "no-store"}
    else:
        headers = {"Cache-Control": "private, max-age=86400"}
    return FileResponse(path, media_type=_hls_media_type(segment), headers=headers)


@router.post("/{media_id}/hls/stop")
def hls_stop(media_id: str, session: str = Query(default=""),
             services: Services = Depends(get_services)) -> dict:
    """視聴終了時に変換を停止する(未完成キャッシュは削除される)。

    HLS生成ジョブと、同一タブのfMP4変換プロセスの両方を止める。
    """
    item = _resolve_video(services, media_id)
    owner = session.strip()[:64]
    # URLが示す旧sourceを即時停止する。動画切替時にこの要求が遅れて届いても、
    # 新sourceの生成を巻き添えにしない。owner全体の旧要求回収は、新しい
    # ensure_async() が _start_lock 内で必ず行う。
    stopped = services.hls.cancel_source(
        item.path, owner=owner or None, min_age=0.0)
    if owner and services.transcode.cancel_owner(owner):
        stopped += 1
    if owner:
        services.remote_hls.remove_owner(media_id, owner)
    return {"stopped": stopped}


#: 1チャンクの読み出しがこれを超えたら記録する(NAS側のスパイク検知)。
#: 不定期なカクつきは、こうした一時的な読み遅延が原因のことが多い。
SLOW_READ_WARN_MS = 400.0


#: 先読みしておくチャンク数(1MB×8 ≒ 8MB)。NAS(SMB/NFS)側の一時的な
#: 応答遅延を吸収し、再生側へ届く前に埋め合わせるための余裕。
READAHEAD_CHUNKS = 8


def _read_ahead(path: str, start: int, end: int, queue: "Queue", stop: threading.Event):
    """別スレッドで先読みする。読み出しの遅延と送出を重ねるため。"""
    remaining = end - start + 1
    try:
        with open(path, "rb") as handle:
            handle.seek(start)
            while remaining > 0 and not stop.is_set():
                began = time.monotonic()
                chunk = handle.read(min(FILE_CHUNK_SIZE, remaining))
                elapsed_ms = (time.monotonic() - began) * 1000
                if elapsed_ms > SLOW_READ_WARN_MS:
                    logger.warning(
                        "配信元の読み出しが遅延しました: %.0fms (%s @ %.1fMB) — "
                        "共有フォルダ側の応答が原因の可能性があります",
                        elapsed_ms, os.path.basename(path),
                        (end - remaining + 1) / 1048576,
                    )
                if not chunk:
                    break
                remaining -= len(chunk)
                # 送出側が止まった(切断された)場合にここで永久に待たない
                while not stop.is_set():
                    try:
                        queue.put(chunk, timeout=0.5)
                        break
                    except Full:
                        continue
    except OSError as exc:
        logger.warning("配信元の読み出しに失敗しました: %s", exc)
    finally:
        # 終端通知が満杯のqueueで失われると、送出側は永久にqueue.getで待つ。
        # 接続が切れた場合だけstopで抜け、通常終了時は必ず終端を届ける。
        while not stop.is_set():
            try:
                queue.put(None, timeout=0.2)
                break
            except Full:
                continue


async def _aiter_file_range(path: str, start: int, end: int):
    """Range配信。先読みスレッドを介して読み出しの揺らぎを吸収する。

    直読みだと「NASから読む → クライアントへ送る」が交互になり、共有側で
    一瞬でも遅延するとそのまま再生の途切れになる。先読みしておけば
    数百ms程度のスパイクは在庫で埋められる。
    """
    queue: "Queue" = Queue(maxsize=4 if qnap_lite() else READAHEAD_CHUNKS)
    stop = threading.Event()
    reader = threading.Thread(
        target=_read_ahead, args=(path, start, end, queue, stop),
        name="framedeck-readahead", daemon=True,
    )
    reader.start()
    try:
        while True:
            chunk = await anyio.to_thread.run_sync(
                queue.get, abandon_on_cancel=True, limiter=_STREAM_LIMITER,
            )
            if chunk is None:
                break
            yield chunk
    finally:
        stop.set()
        # 先読みスレッドが put() で待っている場合に備えて捌けさせる
        try:
            while not queue.empty():
                queue.get_nowait()
        except Empty:
            pass


async def _aiter_transcode(stream):
    """変換ストリームを送出し、切断・完了時に必ずffmpegを終了させる。"""
    try:
        while True:
            chunk = await anyio.to_thread.run_sync(
                stream.read, abandon_on_cancel=True, limiter=_STREAM_LIMITER,
            )
            if not chunk:
                break
            yield chunk
    finally:
        stream.close()


async def _aiter_worker_fmp4(response, controller, initial: bytes):
    """Bridge B -> A -> browser without buffering a complete video.

    Browser origin and every URL remain on A.  A lost B connection marks the
    worker unhealthy so the existing client playback watchdog will retry via
    the local QNAP route, accepting a short interruption.
    """
    try:
        if initial:
            yield initial
        while True:
            # HTTPResponse.read(n) may wait until n bytes arrive, producing
            # needless buffering. read1(n) yields as soon as data is ready.
            reader = getattr(response, "read1", response.read)
            chunk = await anyio.to_thread.run_sync(
                reader, 128 * 1024,
                abandon_on_cancel=True, limiter=_STREAM_LIMITER,
            )
            if not chunk:
                break
            yield chunk
    except Exception as exc:
        controller.mark_unhealthy()
        logger.warning("Linux worker stream interrupted; next retry uses QNAP: %s",
                       type(exc).__name__)
    finally:
        response.close()


def file_validators(stat: os.stat_result) -> tuple[str, str]:
    """ETag と Last-Modified を作る。

    これが無いとブラウザのメディアキャッシュが取得済みの範囲を再利用できず、
    シークのたびに取り直しになる(先読みが痩せてカクつきやすくなる)。
    """
    etag = '"' + hashlib.md5(
        f"{stat.st_mtime_ns}-{stat.st_size}".encode()).hexdigest() + '"'
    return etag, formatdate(stat.st_mtime, usegmt=True)


@router.api_route("/{media_id}/stream", methods=["GET", "HEAD"])
async def stream(media_id: str, request: Request,
                 services: Services = Depends(get_services)):
    item = _resolve_video(services, media_id)
    path = item.path
    try:
        stat = os.stat(path)
    except OSError:
        raise HTTPException(status_code=404, detail="ファイルにアクセスできません")
    file_size = stat.st_size
    mime = mime_for_video(path)
    etag, last_modified = file_validators(stat)

    base_headers = {
        "Accept-Ranges": "bytes",
        "Content-Type": mime,
        "ETag": etag,
        "Last-Modified": last_modified,
        # 中身が変わればETagも変わるので、キャッシュ再利用を許可してよい
        "Cache-Control": "private, max-age=86400",
    }
    try:
        byte_range = parse_range_header(request.headers.get("range"),
                                        file_size)
    except RangeNotSatisfiable:
        return Response(
            status_code=416,
            headers={"Content-Range": f"bytes */{file_size}"},
        )

    # If-Range: 検証子が変わっていたら部分応答ではなく全体を返す
    if_range = request.headers.get("if-range")
    if if_range and if_range not in (etag, last_modified):
        byte_range = None

    if request.method == "HEAD":
        return Response(headers={**base_headers,
                                 "Content-Length": str(file_size)})

    if byte_range is None and request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=base_headers)

    if byte_range is None:
        return StreamingResponse(
            _aiter_file_range(path, 0, file_size - 1),
            media_type=mime,
            headers={**base_headers, "Content-Length": str(file_size)},
        )

    start, end = byte_range
    return StreamingResponse(
        _aiter_file_range(path, start, end),
        status_code=206,
        media_type=mime,
        headers={
            **base_headers,
            "Content-Length": str(end - start + 1),
            "Content-Range": f"bytes {start}-{end}/{file_size}",
        },
    )


@router.get("/{media_id}/stream-transcode")
async def stream_transcode(media_id: str,
                           start: float = Query(default=0.0, ge=0.0),
                           max_height: int | None = Query(default=None, ge=144, le=4320),
                           max_width: int | None = Query(default=None, ge=144, le=7680),
                           session: str = Query(default=""),
                           copy_video: bool = Query(default=False),
                           copy_audio: bool = Query(default=False),
                           tone_map: bool = Query(default=False),
                           smooth_fps: float | None = Query(default=None, ge=20, le=240),
                           force_local: bool = Query(default=False),
                           worker_only: bool = False,
                           services: Services = Depends(get_services)):
    """ブラウザ非対応形式向けのfMP4変換ストリーミング(シークは?start=で再要求)。

    `session` は再生中のタブを識別する任意のID。同じタブからの新しい要求が
    来たら古い変換プロセスを停止し、多重起動によるCPU飽和を防ぐ。
    max_height/max_width 未指定は原寸。`copy_video=1` なら映像を
    再エンコードせずコンテナだけ入れ替える(端末が再生できる形式のとき)。
    """
    item = _resolve_video(services, media_id)
    if tone_map:
        info = services.video_playback.get_info(item.path, media_id)
        if info.color_transfer not in HDR_TRANSFERS or copy_video:
            raise HTTPException(status_code=422, detail="HDR→SDR変換の指定が不正です")

    # Direct Range playback stays on the owner. Conversion can read either
    # a configured mount or a scoped owner HTTP source, negotiated by health.
    if not force_local and offload_enabled(services.settings):
        try:
            source_root, relative = owner_source_ref(
                item.path, services.library.list_roots(),
            )
            if remote_eligible(services.cluster, source_root, services.settings):
                def connect():
                    return open_remote_fmp4(
                        services.cluster, source_root, relative,
                        settings=services.settings, start=start, max_height=max_height, max_width=max_width,
                        session=session.strip()[:64], copy_video=copy_video,
                        copy_audio=copy_audio, smooth_fps=smooth_fps,
                        tone_map=tone_map,
                    )
                remote = await anyio.to_thread.run_sync(
                    connect, limiter=_STREAM_LIMITER,
                )
                initial = b""
                try:
                    first_chunk = getattr(remote, "read1", remote.read)
                    initial = await anyio.to_thread.run_sync(
                        first_chunk, 4096, limiter=_STREAM_LIMITER,
                    )
                    if initial:
                        return StreamingResponse(
                            _aiter_worker_fmp4(remote, services.cluster, initial),
                            media_type="video/mp4",
                            headers={"Cache-Control": "no-store",
                                     "X-FrameDeck-Processing-Node": "linux-worker"},
                        )
                    services.cluster.mark_unhealthy()
                finally:
                    if not initial:
                        remote.close()
        except (WorkerOffloadError, OSError, ValueError, TimeoutError) as exc:
            services.cluster.mark_unhealthy()
            logger.info("Worker unavailable before stream; using NAS fMP4: %s",
                        type(exc).__name__)

    if worker_only:
        raise HTTPException(status_code=503, detail="Linuxワーカーを利用できません。別の画質を選んでください")

    # Hardware and CPU safety caps still apply whenever QNAP encodes locally.
    if qnap_lite() and not copy_video:
        max_height = min(max_height or QNAP_TRANSCODE_HEIGHT, QNAP_TRANSCODE_HEIGHT)
        max_width = min(max_width or QNAP_TRANSCODE_WIDTH, QNAP_TRANSCODE_WIDTH)
        smooth_fps = None
    try:
        stream = services.transcode.open_fmp4(
            item.path, start, max_height=max_height, max_width=max_width,
            owner=session.strip()[:64] or None,
            copy_video=copy_video, copy_audio=copy_audio,
            smooth_fps=smooth_fps, tone_map=tone_map,
        )
    except TranscodeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except OSError as e:
        raise HTTPException(status_code=503, detail=f"変換を開始できません: {e}")
    return StreamingResponse(
        _aiter_transcode(stream), media_type="video/mp4",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/{media_id}/thumbnail")
def thumbnail(media_id: str,
              services: Services = Depends(get_services)) -> Response:
    item = _resolve_video(services, media_id)
    cache_file = (services.paths.thumbnail_cache /
                  f"video_{media_id}_{int(item.modified_at)}.jpg")
    if cache_file.exists():
        return Response(cache_file.read_bytes(), media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=86400"})
    if qnap_lite():
        # ffmpeg would seek and decode a full frame on every cold listing.
        return Response(status_code=204)
    info = services.video_playback.get_info(item.path, media_id)
    at = min(10.0, max(0.5, info.duration_seconds * 0.1))
    try:
        data = video_thumbnail(item.path, at)
    except TranscodeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    try:
        cache_file.write_bytes(data)
    except OSError:
        pass
    return Response(data, media_type="image/jpeg",
                    headers={"Cache-Control": "private, max-age=86400"})


@router.get("/{media_id}/tracks")
def tracks(media_id: str,
           services: Services = Depends(get_services)) -> list[dict]:
    item = _resolve_video(services, media_id)
    info = services.video_playback.get_info(item.path, media_id)
    return [t.to_dict() for t in info.tracks]


@router.get("/{media_id}/chapters")
def chapters(media_id: str,
             services: Services = Depends(get_services)) -> list[dict]:
    item = _resolve_video(services, media_id)
    info = services.video_playback.get_info(item.path, media_id)
    return [c.to_dict() for c in info.chapters]


def _payload_float(payload: dict, key: str, default: float = 0.0) -> float:
    value = payload.get(key, default)
    if value is None:
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if parsed != parsed or parsed in (float("inf"), float("-inf")):
        return default
    return parsed


@router.post("/{media_id}/progress")
def save_progress(media_id: str, payload: dict = Body(...),
                  services: Services = Depends(get_services)) -> dict:
    _resolve_video(services, media_id)
    position = _payload_float(payload, "position_seconds", 0.0)
    duration = _payload_float(payload, "duration_seconds", 0.0)
    speed = _payload_float(payload, "playback_speed", 1.0)
    services.video_playback.save_progress(
        media_id, position, duration, speed=speed,
        audio_track=payload.get("audio_track"),
        subtitle_track=payload.get("subtitle_track"),
    )
    return {"saved": True}
