# FrameDeck 2.0 実装メモ

改修指示書に基づく全面改修の実装記録。**指示書と異なる判断をした箇所**と
その理由をここに記録する。

## 起動方法

```bash
python3 FrameDeck.py
```

- `FrameDeck.py` 冒頭の `APP_MODE` で起動モードを切替:
  - `"web"`(既定): Webサーバのみ。`http://127.0.0.1:9000`
  - `"web_desktop"`: Webサーバ + Tkinter UI 同時起動
- 依存パッケージは初回起動時に `FrameDeck_venv/` へ自動インストール。
  失敗時は無限再起動せず、手動コマンドを表示して終了する。

## テスト

```bash
FrameDeck_venv/bin/python -m pytest tests/
```

57件(シーケンス構築 / 漫画ナビゲーション / アーカイブ安全性 /
Range配信 / ストレージ / Web API / コンテナ判定)。

## 構成

```
FrameDeck.py              ランチャー(venvブートストラップ + 起動設定)
framedeck/
├── bootstrap.py          起動シーケンス(web / web_desktop)
├── config.py             設定・パス管理(settings.json)
├── models/               MediaItem / ComicEntry / ComicSession / VideoInfo…
├── core/                 storage(SQLite) / library / rating / security / services
├── comic/                archive_backend / nested_cache / source /
│                         sequence_builder / reader_engine / image_pipeline
├── video/                probe(ffprobe) / stream(Range) / transcode(fMP4) /
│                         playback_service / mpv_controller(JSON IPC)
├── web/                  FastAPIアプリ + routers + static Web UI(PWA)
└── desktop/              Tkinter UI(共通サービスへ接続)
tests/                    pytest一式
FrameDeck_legacy.py.bak   旧実装のバックアップ(参照用)
```

永続データはすべて `FrameDeck_venv/` 配下:
`config/settings.json`、`data/framedeck.db`、`cache/`(comic_pages /
nested_archives / thumbnails / transcodes)、`logs/framedeck.log`、`runtime/mpv`。

---

## 指示書からの逸脱と理由

### 1. ReadingSequence は「遅延実体化」(指示書7.3の変形)

指示書どおりルート直下全体を即時に一次元化すると、実ライブラリ
(/mnt/Download/Manga、726項目・NAS上)で**シーケンス構築に2分以上**
かかることが実測で判明した(全アーカイブを開いて子アーカイブを検出する
ため)。そこで:

- ルート直下の**トップ項目の順序だけを先に確定**(listdir + 自然順)
- 各トップ項目内の `ComicEntry` 列挙は**必要時に展開してキャッシュ**
- 現在位置は `(top_index, sub_index)` カーソルで管理

読書順の定義(自然順・親直接画像→子アーカイブ)は完全実体化と同一。
`ComicViewState.entry_index / entry_count` は「現在のライブラリ項目内」
の値になる(例: フォルダ内28冊の3冊目 → 3/28)。セッション作成は
実測 0.35秒 に短縮。

### 2. 非対応動画は HLS ではなく fMP4 プログレッシブ変換(指示書15.4)

指示書はHLS生成を第一候補としているが、hls.js の同梱が必要になり
「ビルド不要のフロントエンド」方針と衝突するため、初期実装は
ffmpeg による **フラグメントMP4のパイプ配信**
(`/api/videos/{id}/stream-transcode?start=秒`)とした。
シークはクライアントが `start` を付けて再要求する方式で実現済み。
HLS・完全変換キャッシュは将来拡張として `transcodes/` ディレクトリを
確保してある。

### 3. 内部IDは「評価タグを除いた正規化パス」のハッシュ

`media_id = sha1(canonical_path)`(`{zpi$r=N}` を除去したパス)とした。
これにより**評価の変更(リネーム)でIDが変わらず**、読書位置・再生位置が
評価操作で失われない。`ComicEntry.id` も同様に正規化して計算する。

### 4. mpv のnavファイル150msポーリングを廃止(指示書15.5どおり)

Luaスクリプトは `script-message framedeck-nav prev/next` を送り、
JSON IPC の `client-message` イベントとして受信する。プロパティ購読
(`time-pos` / `duration` / `pause` 等)で再生位置を保存する。
Windows named pipe は未実装(既存機能もLinux前提のため)。

### 5. RARバックエンドの実状

このマシンでは bsdtar / 7z が無く `rarfile + unrar` が使われる
(優先順位 bsdtar → 7z → rarfile は実装済み)。パスワード付き
アーカイブは明示エラー。アーカイブ内エントリ名は Zip Slip 検査
(`../`・絶対パス・ドライブ・NUL を拒否)を通過したものだけを扱う。

### 6. P1機能の未実装分(見かけ上のダミーは作っていない)

- 縦スクロール / 横連続表示モード(単ページ・見開きのみ実装)
- 字幕トラックのWeb側切替(トラック情報の取得・表示APIはあり)
- 外部字幕読み込み、A-Bリピート、シークプレビュー、ページブックマークUI
- PDF対応(`ComicSource` プロトコルで拡張可能な構造のみ)
- AIアップスケール(未着手。パイプラインは差し込み可能な構造)

### 7. セキュリティ

- Web APIは登録済み `library_roots` 配下のみアクセス可
  (resolve + is_relative_to、シンボリックリンク脱出も拒否)
- 削除は「確認トークン発行 → トークン付きDELETE」の2段階。
  既定は send2trash によるゴミ箱移動
- `web_pin` 設定時、localhost以外からのアクセスにPINを要求
- クライアントへ絶対パスは返さない(内部IDとルート相対表示のみ)

## 実データでの検証結果(2026-07-11)

- 漫画: 726項目のライブラリでセッション作成0.35秒、ページ配信38ms、
  漫画間移動14ms。ETag/304、リサイズ配信、サムネイル動作確認
- 動画: 4.4GBのMP4でRange(206/416/HEAD)動作確認。
  MKVはfMP4変換ストリーミングへフォールバック
- 評価: 実ファイルで `{zpi$r=N}` リネーム往復・ID安定を確認
- ポート9000競合時は明確なエラーで終了(exit 1)
- Tkinter UIは web_desktop 相当の経路で起動確認(726項目読込)

---

## Web UI・漫画ナビゲーション・モバイルUI改修メモ

参照指示書: `FRAMEDECK_WEB_UI_MANGA_MOBILE_TASK.md`

### 実装した主な変更

- 漫画APIを `next-spread` / `previous-spread` と `next-page` / `previous-page` に分離。
- Web UIの通常送りを見開き移動、補助ボタンとShift+左右を1ページ調整に接続。
- 漫画末尾・先頭の再入力移動を `boundaryIntent` とホイールロックで制御。
- マウス戻る/進むボタンをセッションの `previous-entry` / `next-entry` に接続し、表示状態と一覧同期を共通化。
- `ComicViewState` レスポンスに `root_folder_id` を補完し、別フォルダへ移動した漫画も左一覧を再同期できるようにした。
- モバイルのライブラリ一覧をバックドロップ付きフローティングドロワーへ変更。
- 漫画・動画別の複数ライブラリルート選択、localStorageでのモード別選択保持、設定画面の漫画/動画セクション分割を追加。
- `library_roots` を `(path, kind)` 重複制約に変更し、同じパスを漫画・動画それぞれへ登録できるようにした。表示名は `display_name` として永続化する。

### 検証状況

この環境では `python3` / `node` / `pytest` 実行が `bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted` で起動前に停止するため、構文チェック・自動テスト・9000番ポート起動確認は未実行。未確認項目は最終報告で確認済み扱いにしない。


### 追加調整

- `library_roots.display_name` を追加し、ルート表示名の作成・変更を永続化。
- `PATCH /api/library/roots/{root_id}` を追加し、設定画面から既存ルート名を変更可能にした。
- 同じ物理パスを漫画・動画の両方に登録した場合でも、`/api/library/items` の `folder.root_id` が現在モードのルートIDを返すようにした。
- 漫画状態の `root_folder_id` 推定では comic kind のルートを優先する。

- legacyの `kind=any` ルートとモード別ルートが同じパスにある場合、現在モードに一致するルートIDを優先して返すようにした。
- フォルダ項目IDから一覧を開く場合も、現在modeに属さないルート配下なら404にする。


### 検証結果更新

- `FrameDeck_venv/bin/python -m py_compile ...`: 成功。
- `node --check framedeck/web/static/js/app.js`: 成功。
- `FrameDeck_venv/bin/python -m pytest tests/ -q`: 63 passed, 1 warning。
- Webサーバ9000番起動: `http://127.0.0.1:9000/api/health` が `{"status":"ok"}` を返すことを確認。
- ブラウザ上の漫画操作・モバイルドロワー・マウス補助ボタン実機操作は未確認。



## Adaptive Media Delivery Phase 1

Added `FRAMEDECK_ADAPTIVE_MEDIA_DELIVERY_TASK.md` as the project-level instruction for adaptive video delivery, comic image optimization, PC/mobile reader settings, margin cropping, and spread splitting.

Implemented the first integration slice:

- Added adaptive media settings to `framedeck/config.py`, including video stream profiles, comic delivery profiles, crop/split flags, and separate desktop/mobile comic reader settings.
- Added cache directory roots for `video_variants`, `video_segments`, `comic_variants`, `comic_analysis`, and `device_models`.
- Added `resolve_comic_reader_settings()` so desktop/mobile comic settings resolve through common settings without sharing mutable keys.
- Added video profile selection in `framedeck/video/profile_service.py`, including save-data handling, measured/downlink bandwidth selection, viewport fallback, and no-upscale height limiting.
- Added `TranscodeJobManager` and `EncoderCapabilityService` foundations for reusing transcode jobs and detecting ffmpeg encoders.
- Added `/api/videos/{media_id}/playback-profile` and `/api/videos/capabilities/encoders`. Existing direct Range streaming and fMP4 transcode streaming remain intact.
- Added comic image analysis models plus white/gray/black crop detection, conservative spread detection, and virtual page ordering helpers.
- Extended `ImagePipeline` with analysis caching and adaptive variant rendering with target viewport size, DPR cap, output format, quality, auto-crop, and split-side handling.
- Added `/api/comics/session/{session_id}/page/{page_index}/analysis` and extended the existing page endpoint with optional `width`, `height`, `dpr`, `profile`, `format`, `quality`, `auto_crop`, and `split_side` query parameters. Existing `w`/`h` behavior remains compatible.

Verified:

- `FrameDeck_venv/bin/python -m pytest tests/ -q`: 74 passed, 357 warnings.
- `node --check framedeck/web/static/js/app.js`: passed.
- `FrameDeck_venv/bin/python -m py_compile` on changed Python modules: passed.
- Web server on port 9000 started successfully and `/api/health` returned `{"status":"ok"}`.

Known remaining work from `FRAMEDECK_ADAPTIVE_MEDIA_DELIVERY_TASK.md`:

- Full multi-quality HLS/fMP4 segment generation, playlist serving, progress events, and non-blocking first-segment-first workflow are not complete.
- The frontend has not yet been changed to send client network hints or use optimized comic page query parameters by default.
- Canvas/WebGL/WebGPU comic enhancement is not implemented; only server-side resize/crop/format optimization is in place.
- Virtual pages are modeled and tested, but `ComicReaderEngine` still counts physical pages for normal navigation. Full physical/virtual position compatibility remains to be integrated.
- Crop and spread detection are intentionally conservative and covered by synthetic tests; they still need broader real-image manual validation.


## Adaptive Media Delivery Phase 2

Extended the Phase 1 server-side foundation into the Web UI path:

- The Web UI now fixes a `uiProfile` at startup using pointer/viewport detection, so comic/video delivery can distinguish desktop and mobile behavior without flipping during a reading session.
- Video opening now posts client media hints to `/api/videos/{media_id}/playback-profile` and uses the resolved profile to choose direct playback or lightweight fMP4 transcoding. The transcode stream accepts `max_height` and the UI shows the selected lightweight profile badge.
- Comic page image URLs now include viewport width, height, DPR, desktop/mobile delivery profile, output format, and auto-crop settings when lightweight delivery is enabled. `comic_delivery_mode=original` keeps the original page URL.
- Comic sessions now apply desktop/mobile reader options after session creation, so `comic_desktop_view_mode` and `comic_mobile_view_mode` affect actual viewer state instead of only being stored settings.
- Crop detection now honors allowed border types, and the comic page API passes the white/gray/black crop settings into variant rendering.
- Settings UI now exposes the main adaptive comic and video delivery controls, including comic delivery mode, output format, crop toggles, spread detection toggle, desktop/mobile comic profiles, video stream mode, desktop/mobile video profiles, codec, bitrate, resolution, segment duration, and cache size.

Verified after Phase 2:

- `FrameDeck_venv/bin/python -m pytest tests/ -q`: 74 passed, 357 warnings.
- `node --check framedeck/web/static/js/app.js`: passed.
- `FrameDeck_venv/bin/python -m py_compile` on changed Python modules: passed.
- Web server on port 9000 started successfully and `/api/health` returned `{"status":"ok"}`.

Remaining gaps:

- Multi-quality HLS playlists and segment generation are still not implemented. Current lightweight video playback uses the existing progressive fMP4 transcode path with profile-selected height.
- Bandwidth measurement and hysteresis-based quality switching are not implemented; the current client sends browser hints only.
- Client-side Canvas/WebGL/WebGPU comic enhancement remains unimplemented. The UI exposes the setting, but no enhancer pipeline runs yet.
- Full virtual-page integration into `ComicReaderEngine` navigation remains incomplete; virtual page helpers are present but physical page count still drives normal sessions.
- The settings UI is sectioned in the existing modal, not yet a full tabbed settings redesign.


## Adaptive Media Delivery Phase 3

Added the first cached HLS delivery slice:

- Added `framedeck/video/hls_service.py` with cache-keyed HLS manifest generation based on source path, mtime, size, selected profiles, source height, segment duration, and pipeline version.
- Added HLS fMP4 ffmpeg command generation for `data_saver`, `mobile_low`, `mobile_balanced`, and `wifi_high` profiles. The generated variants avoid upscaling by clamping profile height to source height.
- Added `HlsService` to the shared service container and keep its segment duration in sync with settings changes.
- Added HLS endpoints:
  - `GET /api/videos/{media_id}/hls/master.m3u8`
  - `GET /api/videos/{media_id}/hls/{profile}/playlist.m3u8`
  - `GET /api/videos/{media_id}/hls/{profile}/{segment}`
- Added path-escape protection for HLS cached file resolution.
- Updated the Web UI video path to use native HLS when the browser can play `application/vnd.apple.mpegurl`; otherwise it falls back to the existing progressive fMP4 transcode path.
- Added tests for HLS manifest profile clamping, path escape rejection, cached master/playlist/segment delivery, and existing adaptive media behavior.

Verified after Phase 3:

- `FrameDeck_venv/bin/python -m pytest tests/ -q`: 77 passed, 357 warnings.
- `node --check framedeck/web/static/js/app.js`: passed.
- `FrameDeck_venv/bin/python -m py_compile` on changed Python modules: passed.
- Web server on port 9000 started successfully and `/api/health` returned `{"status":"ok"}`.

Remaining gaps:

- HLS generation currently happens synchronously on first playlist request. The final design still needs background job generation, progress events, and first-segment-first playback so full conversion completion is not required before playback starts.
- Adaptive HLS quality switching is limited to browser-native HLS behavior where available; explicit measured-throughput hysteresis is not implemented.
- Non-Safari browsers without native HLS still use progressive fMP4 fallback because hls.js is not bundled.


## Spread Layout, Aux Mouse, And Video Resolution Update

Added the follow-up instruction to `FRAMEDECK_ADAPTIVE_MEDIA_DELIVERY_TASK.md` and implemented the first pass of the requested fixes:

- Removed the visible spread gap in CSS by setting `#comic-pages` gap to `0` and making two-page images block-level, fixed-size flex items with no max-width subtraction.
- Added Web UI spread layout calculation so loaded left/right images are rendered at a common integer height and combined width is clamped to the comic stage. Layout is recomputed on image load, resize, and fullscreen changes.
- Added `framedeck/comic/spread_crop_normalizer.py` with `SpreadCropNormalizer`, `SpreadCropResult`, `RenderedPageGeometry`, `SpreadRenderPlan`, and pure spread layout planning helpers.
- Reworked comic entry navigation into `navigateComicEntry(delta, source)` and made buttons, keyboard callers, and aux mouse navigation use the same session-based `next-entry` / `previous-entry` path.
- Replaced aux mouse handling with captured `window` listeners for `mousedown`, `auxclick`, and `mouseup`, including button/buttons normalization and 300ms debounce.
- Added `entry` to optimized comic page URLs so entry changes do not accidentally reuse a stale browser image URL.
- Changed video resolution defaults to `1080p` desktop/common and `720p` mobile.
- Added selectable video resolution profiles: `auto`, `original`, `2160p`, `1440p`, `1080p`, `720p`, `480p`, `360p`. Legacy names are still accepted as aliases.
- Updated adaptive video profile selection to return resolution profiles and width/height boxes, with portrait orientation support and no-upscale clamping.
- Updated HLS and fMP4 ffmpeg scale generation to use aspect-ratio-preserving boxes with `force_original_aspect_ratio=decrease` and `force_divisible_by=2`.
- Added a video quality selector to the player controls. Quality changes preserve position, speed, volume, and mute state as far as the current player path allows.
- Added tests for default 1080p settings, resolution boxes, HLS cache key separation by resolution, spread crop shared vertical normalization, spread output height equality, static no-gap CSS, video quality select presence, and aux mouse debounce wiring.

Verified after this update:

- `FrameDeck_venv/bin/python -m pytest tests/ -q`: 86 passed, 357 warnings.
- `node --check framedeck/web/static/js/app.js`: passed.
- `FrameDeck_venv/bin/python -m py_compile` on changed Python modules: passed.
- Web server on port 9000 started successfully and `/api/health` returned `{"status":"ok"}`.

Remaining gaps:

- Real browser measurements for left/right `boundingClientRect` contact, Chrome/Firefox aux mouse button values, and actual manga spread visual quality are still manual verification items.
- `SpreadCropNormalizer` is implemented and tested as a pure normalizer, but full paired-server rendering is not yet wired into the normal comic page API; the Web UI currently normalizes final visual size.
- HLS background generation, progress events, first-segment-first generation, and explicit measured-throughput hysteresis remain incomplete.


## Follow-up Fixes: Spread Rendering And Local Video Playback

User confirmed aux mouse forward/back navigation is OK. Follow-up fixes focused on remaining spread display and video delivery issues:

- Fixed a race where comic image `src` was assigned before `onload`, which could skip spread relayout for cached images and leave mismatched heights. The load handler is now registered before `src`, and cached complete images schedule layout on the next frame.
- Added a central overlap fallback for two-page spreads: `#comic-pages.two img + img { margin-left: -2px; }`. This hides 1px/rounding black lines from the black viewer background.
- Set two-page spread images to `object-fit: fill` while assigning exact aspect-preserving dimensions from JS, avoiding inner letterboxing in the image content box.
- Desktop auto playback now avoids unnecessary compression when the browser can directly play the original video. The profile resolver returns `original` with reason `desktop-direct-play` for local-PC-style direct playback.
- The Web UI no longer sends a configured resolution as a manual `requestedProfile` during normal auto playback. A requested profile is sent only when the session quality selector is explicitly changed away from `auto`.
- HLS master requests no longer synchronously block to generate HLS. If cached HLS is absent, the endpoint starts background generation and returns `202` instead of delaying playback.
- The default compressed playback path remains progressive fMP4 (`stream-transcode`) so playback can start while ffmpeg is still transcoding, minimizing display delay.

Verified after these fixes:

- `FrameDeck_venv/bin/python -m pytest tests/ -q`: 90 passed, 357 warnings.
- `node --check framedeck/web/static/js/app.js`: passed.
- `FrameDeck_venv/bin/python -m py_compile` on changed Python modules: passed.
- Web server on port 9000 started successfully and `/api/health` returned `{"status":"ok"}`.

Still requiring manual/browser verification:

- Real comic spreads should be rechecked visually for remaining black line artifacts on the user's display/browser.
- Real local PC direct-play behavior should be confirmed with an actual browser-playable video.
- HLS background generation is non-blocking now, but progress notifications and first-segment-first HLS playback are still not fully implemented.


## Follow-up Fixes: Mobile Transcode ffmpeg Resolution

User confirmed spread rendering is now visually correct, while mobile progressive video playback still reported a playback error. The likely causes are either ffmpeg missing in the actual runtime environment or a fragmented MP4 output that is too loose for mobile browser decoders. Local PC playback can succeed via direct play and therefore does not prove that ffmpeg was used.

Implemented fixes:

- Added `framedeck/video/ffmpeg.py` to resolve ffmpeg in this order: system `ffmpeg`, then optional `imageio-ffmpeg` bundled binary.
- Added `video_ffmpeg_auto_download` setting, default `true`. When system ffmpeg is missing, startup/service detection can install `imageio-ffmpeg` into the current Python environment and use its bundled ffmpeg without modifying OS-level paths.
- Wired the resolver into progressive transcode, HLS generation, encoder capability detection, and system info. `/api/system/info` now reports `ffmpeg_source` and `ffmpeg_error`.
- Updated progressive fMP4 transcode command for mobile compatibility: H.264 baseline, level 4.0, `yuv420p`, `avc1` tag, no B-frames, fixed keyframe cadence, AAC audio, and fragmented MP4 movflags.
- Changed ffmpeg stderr handling from discard to logging on stream termination, so server logs can show real encoder/input errors.
- Updated HLS variant commands with the same baseline/yuv420p/avc1 compatibility settings.
- Improved Web UI playback error text for transcode failures so it points at ffmpeg availability, input format, or mobile output compatibility instead of a generic playback error.

Verified after these fixes:

- `FrameDeck_venv/bin/python -m pytest tests/ -q`: 93 passed, 357 warnings.
- `node --check framedeck/web/static/js/app.js`: passed.
- `FrameDeck_venv/bin/python -m py_compile` on changed Python modules: passed.
- Current development machine resolves ffmpeg as `FfmpegResolution(path='/usr/bin/ffmpeg', source='system', error=None)`.

Remaining manual verification:

- Retest mobile playback against the actual FrameDeck runtime. If it still fails with the mobile-compatible fMP4 command, the next practical fallback is native-HLS-first for mobile Safari/iOS and optional hls.js for non-native HLS browsers.
- Confirm whether the production/runtime machine has system ffmpeg or is using the `imageio-ffmpeg` bundled binary via `/api/system/info`.


## Follow-up Fixes: Mobile HLS Fallback And Comic Mobile Controls

User reported that mobile playback still failed with the progressive transcode error message, while comic spread rendering was visually correct. The progressive fMP4 path is therefore not sufficient for the tested mobile browser.

Implemented fixes:

- Mobile/native-HLS-capable browsers now prefer HLS for transcoded playback instead of progressive fMP4. Progressive fMP4 remains as the fallback for browsers without native HLS support.
- HLS master generation now writes `master.m3u8` immediately when background generation starts, instead of returning JSON `202` to the video element. This allows native HLS clients to begin polling playlists/segments while ffmpeg is still generating.
- HLS playlist/segment misses during generation now return `503` with `Retry-After: 1`, making the state explicit while generation catches up.
- HLS ffmpeg output no longer forces `-hls_playlist_type vod`; playlists can update while conversion is still running.
- HLS variant commands keep the mobile-compatible H.264 baseline/yuv420p/avc1 settings.
- Added a mobile comic UI hotspot over the title area. On mobile comic viewer, touching the image/body no longer shows the title/seek controls; controls are shown only from the title-area hotspot or the controls bar itself. Left/right tap zones and swipes remain available for page navigation.

Verified after these fixes:

- `FrameDeck_venv/bin/python -m pytest tests/ -q`: 94 passed, 357 warnings.
- `node --check framedeck/web/static/js/app.js`: passed.
- `FrameDeck_venv/bin/python -m py_compile` on changed Python modules: passed.

Remaining manual verification:

- Retest on the actual mobile browser. Native-HLS-capable browsers should now avoid the failing progressive fMP4 path.
- Non-native-HLS mobile browsers still use progressive fMP4 unless hls.js or a server-side HLS-to-MSE strategy is added.
- Confirm that the title-area-only comic controls feel reachable on the target mobile viewport.


## Follow-up Fixes: Null Video Duration Progress Crash

User provided server logs showing repeated 500 errors from `POST /api/videos/{media_id}/progress`: `float() argument must be a string or a real number, not 'NoneType'`.

Root cause:

- Mobile/HLS playback can report an unknown duration while metadata or playlists are still loading.
- JavaScript `JSON.stringify()` converts `NaN` values to `null`.
- The progress endpoint called `float(payload.get("duration_seconds", 0.0))`, which fails when the key exists with value `null`.

Implemented fixes:

- Added safe `_payload_float()` parsing in `framedeck/web/routers/video.py`; `None`, invalid strings, NaN, and infinities now fall back to defaults.
- Added frontend `finiteSeconds()` normalization before progress payload creation so `position_seconds` and `duration_seconds` are finite numbers.
- Added regression test for null progress payloads.

Verified after this fix:

- `FrameDeck_venv/bin/python -m pytest tests/ -q`: 95 passed, 357 warnings.
- `node --check framedeck/web/static/js/app.js`: passed.
- `FrameDeck_venv/bin/python -m py_compile` on changed Python modules: passed.


## Implementation Plan: Mobile Video Controls And Fullscreen Gestures

Requested follow-up scope:

1. Make the mobile video seek bar easier to tap by rendering mobile controls in two rows: a wider seek row above the existing button row.
2. Support fullscreen on mobile for both comic and video viewers.
3. In fullscreen, tapping the upper title/file-name area exits fullscreen for both comic and video.
4. Keep Picture-in-Picture separate from fullscreen and avoid showing the PiP error when the user pressed fullscreen.
5. Make video seeking work by tapping the seek bar directly.
6. Add video edge tap zones: left edge seeks -10s, right edge seeks +10s. Playback toggle is disabled in those edge zones.
7. Add edge long-press speed ramp: holding an edge zone starts rewind/fast-forward behavior by stepping playback speed up to 5x.

Implementation approach:

- HTML: add mobile-specific video seek row and video edge gesture zones; add a title/fullscreen hotspot for the video viewer matching the existing comic hotspot concept.
- CSS: mobile-only two-row video controls, larger range target, full-screen safe layout, visible hit areas kept transparent.
- JS: unify fullscreen helpers, make title hotspots exit fullscreen when active, keep normal UI reveal behavior outside fullscreen, implement direct seek calculations from pointer coordinates, and add long-press speed ramp for left/right video zones.
- Tests: static tests for two-row controls, fullscreen hotspot wiring, PiP/fullscreen separation, edge gestures, and finite progress payloads.


## Follow-up Implementation: Mobile Seek, Fullscreen, And Edge Gestures

Implemented the requested mobile viewer controls.

Changes:

- Added a mobile-only two-row video control layout. The top row contains a wide `video-seek-mobile` range input and mobile time label; the lower row keeps playback, quality, volume, fullscreen, and navigation controls.
- Added seek synchronization between desktop and mobile video seek sliders. Pointer/tap release on either slider seeks immediately.
- Added mobile video edge gesture zones: left edge seeks -10 seconds, right edge seeks +10 seconds. The center video tap remains play/pause; edge zones do not toggle play/pause.
- Added edge long-press behavior. Right long-press ramps playback speed up to 5x while held; left long-press repeatedly seeks backward with increasing step behavior. Releasing restores the previous playback rate.
- Added fullscreen helper functions with CSS fallback for mobile browsers that do not support element fullscreen.
- Added video title hotspot matching the comic hotspot. In fullscreen, tapping the title/hotspot exits fullscreen for both comic and video. Outside fullscreen it reveals controls.
- Kept Picture-in-Picture separate from fullscreen. PiP is hidden on mobile and guarded by capability checks, so pressing fullscreen no longer routes into PiP behavior.

Verified:

- `node --check framedeck/web/static/js/app.js`: passed.
- `FrameDeck_venv/bin/python -m pytest tests/test_web_static.py -q`: 12 passed.
- `FrameDeck_venv/bin/python -m pytest tests/ -q`: 99 passed, 357 warnings.

Manual verification still needed:

- Confirm mobile browser tap accuracy for the enlarged seek bar.
- Confirm fullscreen fallback behavior on the target mobile browser/OS.
- Confirm long-press speed ramp feels appropriate; ramp interval is currently 600ms and caps at 5x.


## Detailed Design: Mobile Seek Fix, Comic Tap UI Suppression, Hold Zones, Orientation Lock

User reported four mobile regressions after the mobile controls update.

Design decisions:

1. Video mobile seek bar
   - Root cause hypothesis: the lower desktop `video-seek` remains in the DOM and is synchronized with `video-seek-mobile`; HLS/native metadata can also report `duration` as 0/Infinity during load. Either can cause the UI value to snap back.
   - Fix: mobile uses only `video-seek-mobile` for input. The desktop seek remains for desktop layout but is ignored on mobile and hidden/disabled in CSS.
   - Seek math uses `seekableDuration()`: prefer `totalDuration()` when finite, otherwise fall back to `S.video.duration` from ffprobe/API metadata.
   - On pointer down/move/up, seek is computed directly from pointer X in the slider bounding box, so tapping any point jumps to that position.

2. Comic mobile tap zones
   - Root cause: tap-zone click/touch events bubble to the viewer auto-hide handler and reveal UI.
   - Fix: stop propagation on comic tap zones; mobile comic controls are revealed only by `#comic-ui-hotspot` or the controls bar.

3. Video edge long press
   - Root cause: transparent edge zones can still trigger browser text/element selection, context menu, or touch callout behavior on mobile.
   - Fix: apply `user-select: none`, `-webkit-user-select: none`, `-webkit-touch-callout: none`, `touch-action: none`, and prevent `contextmenu`, `selectstart`, and `dragstart` on gesture zones.

4. Mobile orientation lock
   - Add a lock button to the mobile video controls.
   - Store lock state in `S.video.orientationLocked`.
   - When enabled, use `screen.orientation.lock(current orientation)` when available.
   - If unavailable or rejected, apply a CSS fallback class that keeps the video player fixed in the current viewport orientation. Full OS-level rotation prevention is browser/OS dependent, so the fallback is best-effort.
   - Unlock calls `screen.orientation.unlock()` when available and removes fallback classes.

Planned verification:

- Static tests for no lower mobile seek, direct pointer seek logic, comic tap propagation suppression, gesture selection prevention, and orientation lock wiring.
- `node --check framedeck/web/static/js/app.js`.
- Full pytest suite.

## 改修計画: ビューワUI統一(2行コントロール) + HLSシーク/生成管理 (2026-07-11)

ユーザー報告5件への対応計画。

### 課題と原因分析

1. **モバイル横向きで動画コントロールが1行になる**
   原因: CSSのモバイル判定が `@media (max-width: 760px)` のみ。JS側の
   `detectUiProfile()` は `pointer: coarse` も見るため、横向きスマホ
   (幅>760px)でJSは mobile / CSSは desktop と判定が割れる。
2. **初回再生時に圧縮動画未生成でエラー**
   原因: ネイティブHLS再生時、playlist.m3u8 未生成だと即503を返し、
   `<video>` が error イベントで即エラー表示。リトライなし。
3. **HLS変換中のシーク不可・キャッシュ肥大**
   原因: HLSは常に先頭から逐次生成。生成済み範囲外へのシーク手段がなく、
   途中開始(-ss)もない。生成ジョブの中断APIがなく、視聴をやめても
   最後まで変換が走る。`video_variant_cache_gb` 設定は存在するが
   prune未実装で溜まる一方。
4. **漫画コントロールのボタンがあふれて表示切替ボタンが見えない**
   原因: 1行バーに全ボタン+シークバーを詰めており overflow-x: auto で隠れる。
5. **左右タップでシークバー/ファイル名が出る**
   原因: setupAutoHide が viewer 全体の mousemove/touchstart でUI表示。

### 方針

**UI (①④⑤)**
- JSが `body.ui-mobile` / `body.ui-desktop` クラスを付与(初期化+resize時)。
  ビューワのコントロール表示はメディアクエリでなく本クラスで分岐し、
  JSとCSSの判定を一致させる(→①横向きでも2行+回転ロックボタン)。
- 動画/漫画コントロールバーをPC・モバイル共通の2行構成に統一:
  1行目=時刻/ページラベル+シークバー、2行目=ボタン列。
  動画のモバイル専用シークバー複製(video-seek-mobile)は廃止し一本化。
- 漫画UI表示は上部ホットスポット(ファイル名エリア周辺)のタップ/クリックで
  トグル。漫画ビューワの mousemove 表示は廃止(PC/モバイル同一動作)。
  左右タップゾーン・下端は 페ージ送り専用。動画はPCのmousemove表示を維持。
- 全画面中のホットスポットタップは「全画面解除」から「UIトグル」へ変更
  (解除は⛶ボタン/ダブルクリック/Fキー)。

**HLS (②③)**
- `HlsService` に開始オフセット対応: cache_key に start を含め、
  ffmpeg に `-ss start` を付与。マスターURLは
  `/hls/master.m3u8?profile=&start=` → 302 で `/hls/{key}/master.m3u8` へ
  リダイレクトし、以降の playlist/segment は `/hls/{key}/...` で解決
  (相対URL解決のため。stat再計算も不要になる)。
- 生成ジョブ管理: key→(source, Popen, cancelled) を保持。
  同一ソースへ別 start/profile の要求が来たら旧ジョブを停止し
  未完成キャッシュを削除(=「途中からの再圧縮」)。
  `POST /api/videos/{id}/hls/stop` を追加し、クライアントは
  stopVideo/pagehide 時に sendBeacon で停止要求(→溜め込み防止)。
- 完了マーカー(`complete`ファイル)導入: master.m3u8 は生成開始時に
  書くため ready 判定に使えない。マーカー無しディレクトリは
  再利用せず削除して再生成(クラッシュ残骸対策)。
- playlist/segment 配信はファイル出現まで最大~10秒ポーリングしてから
  503(Retry-After)を返す(→②初回の生成待ち)。
- prune実装: `video_variant_cache_gb` を上限に、完了済みを古い順に削除。
  マーカー無し+非アクティブは無条件削除。起動時+生成完了時に実行。
- クライアント: HLS再生は S.video.offset を持ち、シークは
  生成済み範囲(video.seekable)内なら currentTime、範囲外なら
  start付きでmaster再読込。videoエラーは HLS/変換中なら最大4回、
  2.5秒間隔で自動リトライ(スピナー表示)し、その後にエラー表示。

### 影響ファイル
- framedeck/video/hls_service.py (ジョブ管理/start/marker/prune/wait)
- framedeck/web/routers/video.py (master redirect, keyed配信, stop API)
- framedeck/core/services.py (キャッシュ上限設定の反映+起動時prune)
- framedeck/web/templates/index.html, static/css/app.css, static/js/app.js
- tests/test_api.py, tests/test_adaptive_media.py, tests/test_web_static.py
  (旧エンドポイント/モバイル専用シークバー前提のテストを更新、新規追加)

### 検証計画
- pytest全件 + node --check app.js
- ffmpeg実機で実動画を生成し、master→playlist→segment の生成待ち、
  start付きシーク、stopでのジョブ停止・削除、pruneをAPI経由で確認

### 実装結果と検証 (2026-07-11)

実装は計画どおり。計画からの主な決定事項:

- HLSの playlist/segment 配信はキー付きURL(`/hls/{key}/...`)へ移行し、
  旧 `/hls/{profile}/...` エンドポイントは削除した(クライアントは必ず
  master のリダイレクト経由でキーを得るため後方互換は不要)。
- 全画面中のホットスポットタップは「全画面解除」から「UIトグル」へ変更。
  解除は⛶ボタン/ダブルクリック/Fキーで行う(タップで突然全画面が
  解除される事故を防ぐ)。
- 動画シークバーのモバイル複製(video-seek-mobile)は廃止し一本化。
  つまみの拡大は body.ui-mobile スコープのCSSで行う。
- HLSの生成待ちポーリングは master 5秒 / playlist・segment 10秒。
  加えてクライアント側で videoエラー時に 2.5秒×最大4回の自動再試行。

検証(pytest 113件全通過 + ffmpeg実機):

- 初回master要求: 302→キー付きmaster 200、playlist はファイル出現待ちの
  後 0.4秒で 200(②の生成待ち動作)。
- 20分動画の生成途中に start=600 でシーク: 旧ジョブ停止・旧未完成
  キャッシュ削除・新キーで生成開始を確認(③)。
- `POST /hls/stop`: 実行中ジョブ停止(stopped:1)、ffmpegプロセス消滅、
  未完成キャッシュ削除、完了済みキャッシュは保持を確認。
- Playwright(chromium)で PC(1400x900)/モバイル縦(390x844)/
  モバイル横(844x390, タッチ)を実測:
  - 3プロファイルすべてでコントロールが2行(flex-direction: column)。
  - モバイル横でも ui-mobile 判定になり回転ロックボタン表示(①)。
  - 漫画: 左右タップでページ移動してもUIが出ない、mousemoveでも出ない、
    上部ホットスポットでトグル表示(⑤)。表示切替ボタンが390px幅でも
    画面内に収まる(④)。
  - videoエラー自動再試行のトースト表示を確認(②)。

Manual verification still needed:

- iOS Safari 実機でのネイティブHLS再生(初回生成待ち・途中シーク・
  回転ロック)。chromiumはHLSを最後まで再生できないため実機必須。
- 実運用での video_variant_cache_gb 上限による prune 挙動。

補足: テスト実行のため FrameDeck_venv へ pytest / httpx / playwright を
インストールした(実行時依存には影響なし)。

### 追補: 漫画ナビボタンを見た目の方向基準へ (2026-07-11)

ユーザー指摘: シークバーは綴じ方向(RTLで右→左)に追従するが、ボタンは
論理配置(進むが常に右側)のため方向が逆に見える。

対応: ボタンIDを視覚方向基準(btn-comic-entry-left / spread-left /
page-left / page-right / spread-right / entry-right)へ変更し、
タップゾーンと同じく綴じ方向で動作を割り当てる。RTLでは左側ボタンが
「進む」(左ページボタンのラベルは +1)になり、ラベル・ツールチップ・
エントリボタンの無効状態も綴じ方向に応じて切替える。

検証: Playwrightで RTL/LTR 両方向についてページ/見開きボタンの
増減方向がシークバーと一致することを確認。pytest 114件全通過。
