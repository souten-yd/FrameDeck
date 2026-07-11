"""Static Web UI regression tests."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent



def test_comic_spread_css_has_no_gap():
    css = (ROOT / "framedeck/web/static/css/app.css").read_text()
    assert "#comic-pages {" in css
    assert "gap: 0;" in css
    assert "#comic-pages.two img" in css
    assert "max-width: none;" in css
    assert "display: block;" in css
    assert "#comic-pages.two img + img { margin-left: -2px; }" in css


def test_video_quality_select_exists():
    html = (ROOT / "framedeck/web/templates/index.html").read_text()
    assert 'id="sel-video-quality"' in html
    for value in ["auto", "original", "2160p", "1440p", "1080p", "720p", "480p", "360p"]:
        assert f'value="{value}"' in html


def test_aux_mouse_navigation_is_debounced_and_window_captured():
    js = (ROOT / "framedeck/web/static/js/app.js").read_text()
    assert "AUX_MOUSE_DEBOUNCE_MS = 300" in js
    assert "function normalizeAuxDirection" in js
    assert 'window.addEventListener("mousedown", handleAuxMouseNavigation' in js
    assert 'window.addEventListener("auxclick", handleAuxMouseNavigation' in js
    assert "function navigateComicEntry" in js


def test_comic_image_load_handler_is_registered_before_src():
    js = (ROOT / "framedeck/web/static/js/app.js").read_text()
    handler_pos = js.index('img.onload = layoutComicSpread')
    src_pos = js.index('img.src = comicPageUrl(pageIndex)')
    assert handler_pos < src_pos


def test_video_transcode_path_keeps_progressive_fallback_and_mobile_hls():
    js = (ROOT / "framedeck/web/static/js/app.js").read_text()
    assert "逐次軽量配信" in js
    assert "stream-transcode?start=" in js
    assert "function shouldUseNativeHls" in js
    assert "S.uiProfile === \"mobile\"" in js
    assert "/hls/master.m3u8" in js


def test_client_hints_do_not_force_configured_quality():
    js = (ROOT / "framedeck/web/static/js/app.js").read_text()
    hints_block = js[js.index("function clientMediaHints"):js.index("/* ================= api ================= */")]
    assert "requestedProfile" not in hints_block
    assert 'if (S.video.quality && S.video.quality !== "auto") hints.requestedProfile = S.video.quality;' in js


def test_video_transcode_error_message_mentions_ffmpeg():
    js = (ROOT / "framedeck/web/static/js/app.js").read_text()
    assert "変換ストリーミングの再生に失敗しました" in js
    assert "ffmpegの有無" in js


def test_mobile_comic_controls_use_title_hotspot():
    html = (ROOT / "framedeck/web/templates/index.html").read_text()
    css = (ROOT / "framedeck/web/static/css/app.css").read_text()
    js = (ROOT / "framedeck/web/static/js/app.js").read_text()
    assert 'id="comic-ui-hotspot"' in html
    assert ".comic-ui-hotspot" in css
    assert 'closest("#comic-ui-hotspot, .controls-bar")' in js


def test_comic_ui_revealed_only_by_hotspot_on_all_profiles():
    js = (ROOT / "framedeck/web/static/js/app.js").read_text()
    css = (ROOT / "framedeck/web/static/css/app.css").read_text()
    # 漫画はmousemoveでUIを出さない(左右タップでシークバーが出ない)
    assert 'if (!isComic) viewer.addEventListener("mousemove", show);' in js
    # ホットスポットはトグル動作
    assert "if (viewer.classList.contains(\"show-ui\")) hideNow();" in js
    # PCでも漫画ホットスポットを無効化しない(display:noneの一律指定が無い)
    assert "body.ui-desktop .comic-ui-hotspot" not in css


def test_controls_are_two_rows_on_all_profiles():
    html = (ROOT / "framedeck/web/templates/index.html").read_text()
    css = (ROOT / "framedeck/web/static/css/app.css").read_text()
    # 動画・漫画ともシーク行+ボタン行の2行構成(PC/モバイル共通)
    assert html.count('<div class="seek-row">') == 2
    assert html.count('<div class="button-row">') == 2
    assert 'flex-direction: column' in css
    assert '.controls-bar .seek-row' in css
    assert '.controls-bar .button-row' in css


def test_comic_view_mode_button_is_in_button_row():
    html = (ROOT / "framedeck/web/templates/index.html").read_text()
    comic = html[html.index('id="comic-controls"'):html.index('id="comic-title"')]
    assert 'id="btn-view-mode"' in comic
    assert 'id="comic-seek"' in comic
    assert comic.index('class="seek-row"') < comic.index('class="button-row"')


def test_ui_profile_is_applied_as_body_class():
    js = (ROOT / "framedeck/web/static/js/app.js").read_text()
    css = (ROOT / "framedeck/web/static/css/app.css").read_text()
    html = (ROOT / "framedeck/web/templates/index.html").read_text()
    assert "function applyUiProfile" in js
    assert 'classList.toggle("ui-mobile"' in js
    assert 'classList.toggle("ui-desktop"' in js
    assert "body.ui-mobile .ui-mobile-only" in css
    assert "body.ui-desktop .video-ui-hotspot" in css
    assert 'class="icon-btn ui-mobile-only"' in html  # 回転ロックボタン


def test_video_error_is_retried_while_transcode_pending():
    js = (ROOT / "framedeck/web/static/js/app.js").read_text()
    assert "VIDEO_ERROR_MAX_RETRIES" in js
    assert "変換の準備中です" in js
    assert "S.video.errorRetryCount" in js
    assert "clearVideoErrorRetry()" in js


def test_hls_seek_restarts_generation_and_stops_on_close():
    js = (ROOT / "framedeck/web/static/js/app.js").read_text()
    assert "function hlsMasterUrl" in js
    assert "function requestHlsStop" in js
    assert '/hls/stop' in js
    assert "generatedEnd" in js
    assert 'window.addEventListener("pagehide"' in js


def test_mobile_fullscreen_hotspots_and_fallback_exist():
    html = (ROOT / "framedeck/web/templates/index.html").read_text()
    css = (ROOT / "framedeck/web/static/css/app.css").read_text()
    js = (ROOT / "framedeck/web/static/js/app.js").read_text()
    assert 'id="comic-ui-hotspot"' in html
    assert 'id="video-ui-hotspot"' in html
    assert 'viewer-fullscreen-active' in css
    assert 'function enterViewerFullscreen' in js
    assert 'function exitViewerFullscreen' in js
    assert 'isViewerFullscreen(viewer)' in js


def test_mobile_video_edge_zones_and_hold_speed_exist():
    html = (ROOT / "framedeck/web/templates/index.html").read_text()
    js = (ROOT / "framedeck/web/static/js/app.js").read_text()
    assert 'id="video-zone-left"' in html
    assert 'id="video-zone-right"' in html
    assert 'bindVideoGestureZone("video-zone-left", -1)' in js
    assert 'bindVideoGestureZone("video-zone-right", 1)' in js
    assert 'Math.min(5, videoHoldState.speed + 0.5)' in js
    assert 'if (!wasHold) videoSeekBy(direction * 10)' in js


def test_pip_is_guarded_separately_from_fullscreen_on_mobile():
    js = (ROOT / "framedeck/web/static/js/app.js").read_text()
    css = (ROOT / "framedeck/web/static/css/app.css").read_text()
    assert '$("btn-video-full").onclick = () => toggleFullscreen($("video-player"));' in js
    assert 'detectUiProfile() === "mobile" || !document.pictureInPictureEnabled' in js
    assert '#btn-pip { display: none; }' in css

def test_video_seek_uses_pointer_position_and_pending_seek():
    js = (ROOT / "framedeck/web/static/js/app.js").read_text()
    assert "function seekableDuration" in js
    assert "S.video.info?.duration_seconds" in js
    assert "function sliderValueFromPointer" in js
    assert "S.video.pendingSeekSeconds = seconds" in js
    assert "function videoDisplayPosition" in js
    assert 'video.addEventListener("seeked"' in js
    assert 'bindVideoSeekSlider($("video-seek"))' in js


def test_video_seek_slider_is_shared_between_profiles():
    html = (ROOT / "framedeck/web/templates/index.html").read_text()
    js = (ROOT / "framedeck/web/static/js/app.js").read_text()
    # モバイル専用の複製シークバーは廃止(判定ズレの温床だった)
    assert "video-seek-mobile" not in html
    assert "video-seek-mobile" not in js
    assert html.count('id="video-seek"') == 1


def test_mobile_comic_tap_zones_do_not_reveal_controls():
    js = (ROOT / "framedeck/web/static/js/app.js").read_text()
    assert "function handleComicTapZone" in js
    assert 'for (const tapZone of [$("comic-tap-left"), $("comic-tap-right")])' in js
    assert '"pointerdown", "pointerup", "touchstart", "touchend"' in js
    assert 'tapZone.addEventListener(eventName, (e) => e.stopPropagation()' in js


def test_mobile_video_gesture_zones_prevent_selection():
    js = (ROOT / "framedeck/web/static/js/app.js").read_text()
    css = (ROOT / "framedeck/web/static/css/app.css").read_text()
    assert '"contextmenu", "selectstart", "dragstart"' in js
    assert 'zone.addEventListener("touchstart", (e) =>' in js
    assert '{ passive: false }' in js
    assert '-webkit-user-select: none;' in css
    assert '-webkit-touch-callout: none;' in css
    assert '-webkit-tap-highlight-color: transparent;' in css


def test_mobile_video_orientation_lock_is_stateful_with_css_fallback():
    html = (ROOT / "framedeck/web/templates/index.html").read_text()
    js = (ROOT / "framedeck/web/static/js/app.js").read_text()
    css = (ROOT / "framedeck/web/static/css/app.css").read_text()
    assert 'id="btn-orientation-lock"' in html
    assert 'S.video.orientationLockMode = currentOrientationMode();' in js
    assert 'const mode = S.video.orientationLockMode || currentOrientationMode();' in js
    assert 'screen.orientation?.lock?.(mode)' in js
    assert 'orientation-lock-active.orientation-lock-landscape' in css
    assert 'orientation-lock-active.orientation-lock-portrait' in css
    assert '@media (orientation: portrait)' in css
    assert '@media (orientation: landscape)' in css

