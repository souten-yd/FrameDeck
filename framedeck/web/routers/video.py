"""動画API。Range Request直接配信と変換ストリーミング。"""
from __future__ import annotations

import os

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from ...core.security import PathValidationError
from ...core.services import Services
from ...models import MediaItem
from ...video.stream import (
    RangeNotSatisfiable,
    iter_file_range,
    mime_for_video,
    parse_range_header,
)
from ...video.hls_service import HLS_PROFILES
from ...video.profile_service import VideoClientHints, select_video_profile
from ...video.transcode import TranscodeError, video_thumbnail
from ..dependencies import get_services

router = APIRouter(prefix="/api/videos", tags=["video"])


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
def playback_profile(media_id: str, payload: dict = Body(default={}),
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
    )
    settings = services.settings.as_dict()
    requested = payload.get("requestedProfile")
    if requested and requested != "auto":
        settings = dict(settings)
        if requested == "original":
            settings["video_stream_mode"] = "original"
        else:
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
        "direct_play": info.direct_play,
        "direct_play_reason": info.direct_play_reason,
    }


@router.get("/capabilities/encoders")
def encoder_capabilities(services: Services = Depends(get_services)) -> list[dict]:
    auto_download = bool(services.settings.get("video_ffmpeg_auto_download", True))
    return [cap.__dict__ for cap in services.encoder_capabilities.detect(auto_download)]


@router.get("/{media_id}/hls/master.m3u8")
def hls_master(media_id: str,
               profile: str | None = Query(default=None),
               services: Services = Depends(get_services)):
    item = _resolve_video(services, media_id)
    info = services.video_playback.get_info(item.path, media_id)
    profiles = _hls_profiles(profile)
    try:
        manifest = services.hls.ensure_async(item.path, profiles=profiles, source_height=info.height)
    except TranscodeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return FileResponse(
        manifest.master,
        media_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/{media_id}/hls/{profile}/playlist.m3u8")
def hls_playlist(media_id: str, profile: str,
                 services: Services = Depends(get_services)):
    item = _resolve_video(services, media_id)
    info = services.video_playback.get_info(item.path, media_id)
    try:
        path = services.hls.resolve_file(
            item.path,
            f"{profile}/playlist.m3u8",
            profiles=[profile],
            source_height=info.height,
        )
    except FileNotFoundError:
        raise HTTPException(
            status_code=503, detail="HLS playlist 生成中です",
            headers={"Retry-After": "1"},
        )
    except TranscodeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return FileResponse(
        path,
        media_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-store", "Retry-After": "1"},
    )


@router.get("/{media_id}/hls/{profile}/{segment}")
def hls_segment(media_id: str, profile: str, segment: str,
                services: Services = Depends(get_services)):
    if "/" in segment or ".." in segment:
        raise HTTPException(status_code=404, detail="HLS segment が見つかりません")
    item = _resolve_video(services, media_id)
    info = services.video_playback.get_info(item.path, media_id)
    try:
        path = services.hls.resolve_file(
            item.path,
            f"{profile}/{segment}",
            profiles=[profile],
            source_height=info.height,
        )
    except FileNotFoundError:
        raise HTTPException(
            status_code=503, detail="HLS segment 生成中です",
            headers={"Retry-After": "1"},
        )
    except TranscodeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return FileResponse(
        path,
        media_type=_hls_media_type(segment),
        headers={"Cache-Control": "private, max-age=86400"},
    )


@router.api_route("/{media_id}/stream", methods=["GET", "HEAD"])
def stream(media_id: str, request: Request,
           services: Services = Depends(get_services)):
    item = _resolve_video(services, media_id)
    path = item.path
    try:
        file_size = os.path.getsize(path)
    except OSError:
        raise HTTPException(status_code=404, detail="ファイルにアクセスできません")
    mime = mime_for_video(path)

    base_headers = {
        "Accept-Ranges": "bytes",
        "Content-Type": mime,
    }
    try:
        byte_range = parse_range_header(request.headers.get("range"),
                                        file_size)
    except RangeNotSatisfiable:
        return Response(
            status_code=416,
            headers={"Content-Range": f"bytes */{file_size}"},
        )

    if request.method == "HEAD":
        return Response(headers={**base_headers,
                                 "Content-Length": str(file_size)})

    if byte_range is None:
        return StreamingResponse(
            iter_file_range(path, 0, file_size - 1),
            media_type=mime,
            headers={**base_headers, "Content-Length": str(file_size)},
        )

    start, end = byte_range
    return StreamingResponse(
        iter_file_range(path, start, end),
        status_code=206,
        media_type=mime,
        headers={
            **base_headers,
            "Content-Length": str(end - start + 1),
            "Content-Range": f"bytes {start}-{end}/{file_size}",
        },
    )


@router.get("/{media_id}/stream-transcode")
def stream_transcode(media_id: str,
                     start: float = Query(default=0.0, ge=0.0),
                     max_height: int = Query(default=1080, ge=144, le=4320),
                     max_width: int | None = Query(default=None, ge=144, le=7680),
                     services: Services = Depends(get_services)):
    """ブラウザ非対応形式向けのfMP4変換ストリーミング(シークは?start=で再要求)。"""
    item = _resolve_video(services, media_id)
    try:
        generator = services.transcode.stream_fmp4(item.path, start, max_height=max_height, max_width=max_width)
    except TranscodeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return StreamingResponse(
        generator, media_type="video/mp4",
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
