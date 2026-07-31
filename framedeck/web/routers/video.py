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
from ...video.stream import (
    CHUNK_SIZE as FILE_CHUNK_SIZE,
    RangeNotSatisfiable,
    mime_for_video,
    parse_range_header,
)
from ...video.hls_service import HLS_PROFILES
from ...video.profile_service import (
    VideoClientHints,
    classify_network,
    original_profile,
    resolve_direct_play,
    select_video_profile,
)
from ...video.transcode import TranscodeError, video_thumbnail
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
        "transcode_available": services.transcode.available(),
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
    )
    settings = services.settings.as_dict()
    requested = payload.get("requestedProfile")
    direct = resolve_direct_play(info, hints)
    if requested == "original":
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
    return {
        "profile": profile.to_dict(),
        "network": classify_network(hints),
        # 端末の申告を反映した判定(サーバ側の固定テーブルより優先)
        "direct_play": direct.direct_play,
        "direct_play_reason": direct.reason or info.direct_play_reason,
        # 映像を再エンコードせずコンテナだけ入れ替えれば足りるか
        "copy_video": direct.copy_video and profile.transcode,
        "copy_audio": direct.copy_audio and profile.transcode,
        "server_direct_play": info.direct_play,
    }


@router.get("/capabilities/encoders")
def encoder_capabilities(services: Services = Depends(get_services)) -> list[dict]:
    auto_download = bool(services.settings.get("video_ffmpeg_auto_download", True))
    return [cap.__dict__ for cap in services.encoder_capabilities.detect(auto_download)]


@router.get("/{media_id}/hls/master.m3u8")
async def hls_master(media_id: str,
                     profile: str | None = Query(default=None),
                     start: float = Query(default=0.0, ge=0.0),
                     services: Services = Depends(get_services)):
    """HLS生成を開始し、キャッシュキー付きマスターURLへリダイレクトする。

    playlist/segment の相対URLをキー付きパスで解決させるため、
    直接配信ではなくリダイレクトを返す。`start` 指定で途中からの
    再生成(シーク)に対応する。
    """
    item = _resolve_video(services, media_id)
    info = services.video_playback.get_info(item.path, media_id)
    profiles = _hls_profiles(profile)
    try:
        # 旧ジョブの停止待ちを含むため、専用スレッド枠で実行する
        manifest = await anyio.to_thread.run_sync(
            lambda: services.hls.ensure_async(
                item.path, profiles=profiles,
                source_height=info.height, start_seconds=start,
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


@router.get("/{media_id}/hls/{key}/master.m3u8")
async def hls_master_file(media_id: str, key: str,
                          services: Services = Depends(get_services)):
    _resolve_video(services, media_id)
    path = await _hls_wait_file(services, key, "master.m3u8", timeout=5.0)
    return FileResponse(
        path,
        media_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/{media_id}/hls/{key}/{profile}/{segment}")
async def hls_media_file(media_id: str, key: str, profile: str, segment: str,
                         services: Services = Depends(get_services)):
    if profile not in HLS_PROFILES:
        raise HTTPException(status_code=404, detail="HLS profile が見つかりません")
    if "/" in segment or ".." in segment or segment.startswith("."):
        raise HTTPException(status_code=404, detail="HLS segment が見つかりません")
    _resolve_video(services, media_id)
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
    stopped = services.hls.cancel_source(item.path)
    owner = session.strip()[:64]
    if owner and services.transcode.cancel_owner(owner):
        stopped += 1
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
        try:
            queue.put_nowait(None)
        except Full:
            pass


async def _aiter_file_range(path: str, start: int, end: int):
    """Range配信。先読みスレッドを介して読み出しの揺らぎを吸収する。

    直読みだと「NASから読む → クライアントへ送る」が交互になり、共有側で
    一瞬でも遅延するとそのまま再生の途切れになる。先読みしておけば
    数百ms程度のスパイクは在庫で埋められる。
    """
    queue: "Queue" = Queue(maxsize=READAHEAD_CHUNKS)
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
                           smooth_fps: float | None = Query(default=None, ge=20, le=240),
                           services: Services = Depends(get_services)):
    """ブラウザ非対応形式向けのfMP4変換ストリーミング(シークは?start=で再要求)。

    `session` は再生中のタブを識別する任意のID。同じタブからの新しい要求が
    来たら古い変換プロセスを停止し、多重起動によるCPU飽和を防ぐ。
    max_height/max_width 未指定は原寸。`copy_video=1` なら映像を
    再エンコードせずコンテナだけ入れ替える(端末が再生できる形式のとき)。
    """
    item = _resolve_video(services, media_id)
    try:
        stream = services.transcode.open_fmp4(
            item.path, start, max_height=max_height, max_width=max_width,
            owner=session.strip()[:64] or None,
            copy_video=copy_video, copy_audio=copy_audio,
            smooth_fps=smooth_fps,
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
