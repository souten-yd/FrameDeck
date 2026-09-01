/* FrameDeck Web UI */
"use strict";

/* ================= state ================= */
const S = {
  mode: "comic",
  roots: [],
  activeRootIds: { comic: null, video: null },
  folderId: null,
  folderInfo: null,
  items: [],
  selectedId: null,
  readingItemId: null,
  history: [],
  histIndex: -1,
  settings: {},
  uiProfile: "desktop",
  selectMode: false,
  checked: new Set(),
  comic: {
    state: null,
    boundaryIntent: null,
    boundaryTimer: null,
    wheelLockedUntil: 0,
    entryNavigationBusy: false,
  },
  video: {
    item: null, info: null, transcode: false, hls: false, hlsProfile: null,
    offset: 0,
    saveTimer: null, duration: 0, quality: "auto",
    pendingSeekSeconds: null,
    errorRetryCount: 0, errorRetryTimer: null,
    orientationLocked: false, orientationLockMode: null,
    lastSeenOrientation: null, orientationRevealTimer: null,
    // 読み込みの世代番号。連続操作で古い応答が新しい再生を壊さないよう、
    // openVideo/シークのたびに増やして応答の鮮度を判定する
    loadToken: 0,
    reloadTimer: null,
    maxHeight: null, maxWidth: null,
    copyVideo: false, copyAudio: false, transcodeFallback: false,
    mobileStableOriginal: false, desktopStableOriginal: false,
    syncRate: 1, syncInfo: null,
    starveTimes: [], adapting: false, qualityIsManual: false, hlsFallback: false,
    watchdogTimer: null, watchdogPosition: 0, watchdogStrikes: 0,
    recovering: false,
    pauseStopTimer: null, pausedConversionStopped: false,
  },
};

/* このタブを識別するID。サーバ側で「同じタブの古い変換」を止めるために使う */
const CLIENT_SESSION_ID = (() => {
  const key = "framedeck.sessionId";
  let value = sessionStorage.getItem(key);
  if (!value) {
    value = (crypto.randomUUID?.() || `s${Date.now()}${Math.random()}`).slice(0, 40);
    sessionStorage.setItem(key, value);
  }
  return value;
})();

const $ = (id) => document.getElementById(id);

function detectUiProfile() {
  const coarse = matchMedia?.("(pointer: coarse)").matches;
  const narrow = window.innerWidth <= 760;
  return coarse || narrow ? "mobile" : "desktop";
}

/* CSS側もJSと同じUIプロファイルで分岐できるようbodyへクラスを付与する
   (横向きスマホは幅>760pxになるためメディアクエリだけでは判定が割れる) */
function applyUiProfile() {
  S.uiProfile = detectUiProfile();
  document.body.classList.toggle("ui-mobile", S.uiProfile === "mobile");
  document.body.classList.toggle("ui-desktop", S.uiProfile !== "mobile");
}

function videoSupportsNativeHls() {
  return Boolean(video?.canPlayType?.("application/vnd.apple.mpegurl"));
}

function configuredVideoQuality() {
  const sessionQuality = S.video.quality || "auto";
  if (sessionQuality !== "auto") return sessionQuality;
  const key = S.uiProfile === "mobile" ? "video_profile_mobile" : "video_profile_desktop";
  return S.settings[key] || S.settings.video_max_resolution || "auto";
}

function hlsProfileName(profile) {
  const allowed = new Set(["2160p", "1440p", "1080p", "720p", "480p", "360p"]);
  if (allowed.has(profile)) return profile;
  return S.uiProfile === "mobile" ? "720p" : "1080p";
}

function hlsMasterUrl(itemId, profile, startSeconds) {
  const start = Math.max(0, Number(startSeconds) || 0);
  return `/api/videos/${itemId}/hls/master.m3u8?profile=${encodeURIComponent(profile)}` +
    `&start=${start.toFixed(2)}&session=${encodeURIComponent(CLIENT_SESSION_ID)}`;
}

/* モバイルで変換が必要な場合はHLSを使う。
   逐次fMP4はRange要求に応えられない(chunkedの200を返す)ため、
   iOS Safariのように「まずRangeで問い合わせる」実装では再生できない。 */
function shouldUseNativeHls() {
  return S.uiProfile === "mobile" && videoSupportsNativeHls();
}

/* 素材の解像度に収まる最大のHLSプロファイルを選ぶ */
function hlsProfileForSource(height) {
  for (const [name, h] of [["1080p", 1080], ["720p", 720], ["480p", 480]]) {
    if ((height || 1080) >= h) return name;
  }
  return "360p";
}

/* iOS Safariは再生可能なMP4でも、Wi-Fi越しの長時間Range配信で
   短い再取得を繰り返し、瞬間ビットレートの高い素材だけバッファが
   尽きることがある。1080p以下の「原寸」は寸法を変えず、Safariが
   先行取得しやすい2秒HLSにして配信を平準化する。 */
function shouldStabilizeMobileOriginal(info) {
  if (!shouldUseNativeHls() || configuredVideoQuality() !== "original") return false;
  const width = Number(info?.width) || 0;
  const height = Number(info?.height) || 0;
  if (!width || !height) return false;
  return Math.max(width, height) <= 1920 && Math.min(width, height) <= 1088;
}

/* PCで明示的に「原寸」を選ぶ場合は、ビットストリームを無変換で
   fMP4へ再多重化する。自動のDirect Playは維持しつつ、安定性を
   明示した原寸再生ではブラウザの細かいRange再取得を避ける。 */
function shouldStabilizeDesktopOriginal() {
  return S.uiProfile !== "mobile" && configuredVideoQuality() === "original";
}

/* この端末が実際に再生できるコーデック/コンテナを調べてサーバへ申告する。
   サーバ側の固定テーブルでは、HEVCを再生できる端末でも「非対応」と
   判定して不要な変換が走り、4K60などでは再生がカクついてしまうため。 */
/* 判定は canPlayType のみを使う。MediaSource.isTypeSupported は
   MSE経由での可否であって <video src> の直接再生とは一致しない
   (例: Chromeはmpeg-tsをMSEでは扱えるがファイル直再生はできない)。 */
const CODEC_PROBES = {
  video: [
    ["h264", 'video/mp4; codecs="avc1.640028"'],
    ["hevc", 'video/mp4; codecs="hvc1.1.6.L93.B0"'],
    ["hevc", 'video/mp4; codecs="hev1.2.4.L153.B0"'],
    ["av1", 'video/mp4; codecs="av01.0.08M.08"'],
    ["vp9", 'video/webm; codecs="vp9"'],
    ["vp8", 'video/webm; codecs="vp8"'],
  ],
  audio: [
    ["aac", 'video/mp4; codecs="mp4a.40.2"'],
    ["mp3", "audio/mpeg"],
    ["opus", 'video/webm; codecs="opus"'],
    ["vorbis", 'video/webm; codecs="vorbis"'],
    ["flac", "audio/flac"],
    ["ac3", 'video/mp4; codecs="ac-3"'],
    ["eac3", 'video/mp4; codecs="ec-3"'],
  ],
  // コンテナはコーデック付きで問い合わせる(裸のMIMEは"maybe"しか返らない)
  container: [
    ["mp4", 'video/mp4; codecs="avc1.640028"'],
    ["webm", 'video/webm; codecs="vp9"'],
    ["matroska", 'video/x-matroska; codecs="avc1.640028"'],
    ["mov", 'video/quicktime; codecs="avc1.640028"'],
    ["mpegts", 'video/mp2t; codecs="avc1.640028"'],
  ],
};

let cachedCodecSupport = null;
function clientCodecSupport() {
  if (cachedCodecSupport) return cachedCodecSupport;
  const probe = document.createElement("video");
  const collect = (entries) => [
    ...new Set(
      entries
        .filter(([, type]) => probe.canPlayType(type) === "probably")
        .map(([name]) => name)
    ),
  ];
  cachedCodecSupport = {
    videoCodecs: collect(CODEC_PROBES.video),
    audioCodecs: collect(CODEC_PROBES.audio),
    containers: collect(CODEC_PROBES.container),
  };
  return cachedCodecSupport;
}

/* ================= 表示同期 (display sync) =================
   映像のfpsとディスプレイのリフレッシュレートが整数比でないと、
   1フレームあたりの表示回数が 3,3,2,3,3,2… のように揺れて
   「カクつき(judder)」として見える。60Hzで24fpsを出す時の3:2プルダウンが典型。

   対策としてサーバ側でfpsを変換する案は採らない。ffmpegのfpsフィルタは
   同じ不均等パターンでフレームを複製するだけで judder は消えず、
   実時間エンコードのCPU負荷と通信量だけが増えるため。
   代わりに mpv の --video-sync=display-resample と同じ考え方で、
   再生速度をごく僅かに補正して「fps×補正 = リフレッシュレート÷整数」に
   合わせる。補正が小さい場合(既定1.2%以内)だけ自動適用する。 */
/* 許容する速度補正の上限。auto は体感できない範囲(±1.2%)まで。
   strong は PAL変換相当(±5%)まで許し、49.99Hz+24fps のように
   auto では届かない組み合わせを均等表示にできる(音程はブラウザが保持)。 */
const DISPLAY_SYNC_TOLERANCES = { auto: 0.012, strong: 0.05 };

function displaySyncTolerance() {
  const mode = S.settings.video_display_sync || "auto";
  return DISPLAY_SYNC_TOLERANCES[mode] ?? DISPLAY_SYNC_TOLERANCES.auto;
}

let cachedRefreshHz = null;
let refreshHzMeasuredAt = 0;
const REFRESH_HZ_TTL_MS = 60000;

/* 別のディスプレイへ移動したり、端末の可変リフレッシュが切り替わると
   実測値は変わる。ウィンドウサイズ変更・復帰時に測り直す。 */
function invalidateRefreshHz() {
  cachedRefreshHz = null;
  refreshHzMeasuredAt = 0;
}
window.addEventListener("resize", invalidateRefreshHz);
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") invalidateRefreshHz();
});

function measureDisplayRefreshHz({ force = false } = {}) {
  const fresh = cachedRefreshHz &&
    (performance.now() - refreshHzMeasuredAt) < REFRESH_HZ_TTL_MS;
  if (fresh && !force) return Promise.resolve(cachedRefreshHz);
  // 非表示タブでは requestAnimationFrame が絞られ、正しく測れない
  if (document.visibilityState !== "visible") return Promise.resolve(null);
  return new Promise((resolve) => {
    const stamps = [];
    const step = (t) => {
      stamps.push(t);
      if (stamps.length < 90) { requestAnimationFrame(step); return; }
      const gaps = [];
      for (let i = 1; i < stamps.length; i++) gaps.push(stamps[i] - stamps[i - 1]);
      gaps.sort((a, b) => a - b);
      const median = gaps[Math.floor(gaps.length / 2)];
      cachedRefreshHz = median > 0 ? Math.round((1000 / median) * 100) / 100 : null;
      refreshHzMeasuredAt = performance.now();
      resolve(cachedRefreshHz);
    };
    requestAnimationFrame(step);
  });
}

function computeDisplaySync(fps, hz) {
  if (!fps || !hz || fps <= 0 || hz <= 0) return null;
  const ratio = hz / fps;
  if (ratio < 1) {
    // 映像のほうが速い(120fps素材を60Hzで見る等)。間引きは避けられない
    return { ratio, repeats: 1, rate: 1, deviation: 0, ok: false, tooFast: true };
  }
  const repeats = Math.max(1, Math.round(ratio));
  const rate = ratio / repeats;
  const deviation = Math.abs(rate - 1);
  return { ratio, repeats, rate, deviation, ok: deviation <= displaySyncTolerance() };
}

/* 速度補正では均等化できない組み合わせ(60Hzで23.976fpsなど)のとき、
   サーバ側で画面のリフレッシュレートに合わせて中間フレームを作る。
   単純なフレーム複製では同じ不均等パターンが残るため、ブレンド補間
   (ffmpegのframerateフィルタ)を使う。動きは滑らかになるが速い動きでは
   輪郭が柔らかくなるため、既定では無効の任意機能とする。 */
function smoothMotionTarget() {
  if ((S.settings.video_smooth_motion || "off") === "off") return null;
  const info = S.video.syncInfo;
  const fps = Number(S.video.info?.frame_rate) || 0;
  const height = Number(S.video.info?.height) || 0;
  if (!info || info.ok || info.tooFast) return null;
  if (info.deviation < 0.02) return null;
  // 実時間で変換できる範囲に限る(4K等は間に合わないため対象外)
  if (!fps || height > 1088 || fps > 31) return null;
  return Math.round(info.hz * 100) / 100;
}

function syncedRate(base) {
  const factor = S.video.syncRate || 1;
  return Math.max(0.0625, Math.min(16, base * factor));
}

/* 再生速度は必ずここを通す(ユーザー指定速度 × 表示同期の補正) */
function applyPlaybackRate() {
  const base = Number($("sel-speed").value) || 1;
  video.playbackRate = syncedRate(base);
}

/* 開いた動画のfpsと実測リフレッシュレートから、補正するか判断する。
   判定結果は設定画面の診断に出すだけで、通知は出さない。 */
async function updateDisplaySync({ silent = true } = {}) {
  S.video.syncRate = 1;
  S.video.syncInfo = null;
  if (S.settings.video_display_sync === "off") return null;
  const fps = Number(S.video.info?.frame_rate) || 0;
  if (!fps) return null;
  const hz = await measureDisplayRefreshHz();
  if (!hz) return null;
  const sync = computeDisplaySync(fps, hz);
  if (!sync) return null;
  S.video.syncInfo = { ...sync, fps, hz };
  if (sync.ok && sync.deviation > 0.00005) {
    S.video.syncRate = sync.rate;
    applyPlaybackRate();
  }
  return S.video.syncInfo;
}

/* ================= 再生の乱れの記録 =================
   不定期なカクつきは再現しづらいので、起きた事実を残して後から見られる
   ようにする。バッファ待ち(供給不足)と表示落ち(描画が間に合わない)は
   原因が違うため区別して数える。 */
const PLAYBACK_GLITCH_LIMIT = 40;
const playbackGlitches = [];

function recordGlitch(kind, detail) {
  playbackGlitches.push({ at: Date.now(), kind, detail });
  if (playbackGlitches.length > PLAYBACK_GLITCH_LIMIT) playbackGlitches.shift();
}

function glitchSummary() {
  if (!playbackGlitches.length) return "直近の再生で乱れは記録されていません。";
  const counts = {};
  for (const g of playbackGlitches) counts[g.kind] = (counts[g.kind] || 0) + 1;
  const last = playbackGlitches[playbackGlitches.length - 1];
  const label = { buffer: "バッファ待ち", frames: "表示落ち", reload: "読み直し" };
  const parts = Object.entries(counts)
    .map(([k, n]) => `${label[k] || k} ${n}回`).join(" / ");
  return `直近の乱れ: ${parts} — 最後 ${new Date(last.at).toLocaleTimeString()}` +
    (last.detail ? ` (${last.detail})` : "");
}

/* 設定画面に出す診断文。端末ごとに実測値が違うため、その場で測って見せる */
function buildDisplaySyncHint() {
  const info = S.video.syncInfo;
  const hz = info?.hz || cachedRefreshHz;
  if (!hz) {
    measureDisplayRefreshHz();
    return "この画面のリフレッシュレートを測定し、映像のfpsと整数比にならない場合だけ" +
      "再生速度をごく僅か(±1.2%以内)補正します。端末ごとに自動判定します。";
  }
  const base = `この画面: 約 ${hz.toFixed(1)}Hz`;
  if (!info) return `${base} — 動画を開くと映像fpsとの相性を判定します。`;
  const cadence = `映像 ${info.fps.toFixed(2)}fps → 1コマあたり ${info.ratio.toFixed(2)} 回表示`;
  if (info.ok) {
    const applied = Math.abs((S.video.syncRate || 1) - 1) > 0.00005;
    return `${base} / ${cadence} — ${applied
      ? `速度を ${((S.video.syncRate - 1) * 100).toFixed(2)}% 補正して均等表示にしています`
      : "整数比なので補正は不要です"}`;
  }
  return `${base} / ${cadence} — 整数比にならないため表示間隔が不均等になります。` +
    "「強め」にすると±5%まで速度補正を許可します。それでも届かない場合は" +
    "下の「なめらか変換」で中間フレームを生成できます。";
}

function clientMediaHints() {
  const connection = navigator.connection || navigator.mozConnection || navigator.webkitConnection;
  return {
    effectiveType: connection?.effectiveType || null,
    // Wi-Fi/有線は原寸、モバイル回線は上限付き配信。type非対応の
    // ブラウザではサーバ側がクライアントIP(LANかどうか)で補完する
    connectionType: connection?.type || null,
    downlink: connection?.downlink || null,
    saveData: Boolean(connection?.saveData),
    viewportWidth: window.innerWidth,
    viewportHeight: window.innerHeight,
    devicePixelRatio: window.devicePixelRatio || 1,
    uiProfile: S.uiProfile,
    ...clientCodecSupport(),
  };
}

/* ================= api ================= */
async function api(path, options = {}) {
  const opts = { ...options };
  if (opts.json !== undefined) {
    opts.method = opts.method || "POST";
    opts.headers = { "Content-Type": "application/json" };
    opts.body = JSON.stringify(opts.json);
    delete opts.json;
  }
  const response = await fetch(path, opts);
  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try { detail = (await response.json()).detail || detail; } catch (e) {}
    throw new Error(detail);
  }
  if (response.status === 204) return null;
  return response.json();
}

/* ================= toast / modal ================= */
let toastTimer = null;
function toast(message, isError = false) {
  const el = $("toast");
  el.textContent = message;
  el.classList.toggle("error", isError);
  el.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), 2600);
}

function showModal(title, bodyEl, actions) {
  $("modal-title").textContent = title;
  const body = $("modal-body");
  body.innerHTML = "";
  body.appendChild(bodyEl);
  const actionsEl = $("modal-actions");
  actionsEl.innerHTML = "";
  for (const action of actions) {
    const btn = document.createElement("button");
    btn.className = "modal-btn" + (action.kind ? ` ${action.kind}` : "");
    btn.textContent = action.label;
    btn.onclick = () => action.onClick();
    actionsEl.appendChild(btn);
  }
  $("modal-backdrop").classList.remove("hidden");
}
function closeModal() { $("modal-backdrop").classList.add("hidden"); }
$("modal-backdrop").addEventListener("click", (e) => {
  if (e.target === $("modal-backdrop")) closeModal();
});

/* ================= library ================= */
function rootsForMode(mode = S.mode) {
  return S.roots.filter((root) => root.kind === mode || root.kind === "any");
}

function activeRootStorageKey(mode) {
  return `framedeck.activeRoot.${mode}`;
}
function saveActiveRootId(mode, id) {
  if (id) localStorage.setItem(activeRootStorageKey(mode), id);
  else localStorage.removeItem(activeRootStorageKey(mode));
}
function loadActiveRootId(mode) {
  return localStorage.getItem(activeRootStorageKey(mode));
}

function activeRootForMode(mode = S.mode) {
  const roots = rootsForMode(mode);
  const id = S.activeRootIds[mode];
  return roots.find((root) => root.id === id) || roots[0] || null;
}

function initializeActiveRoots() {
  for (const mode of ["comic", "video"]) {
    const roots = rootsForMode(mode);
    const saved = loadActiveRootId(mode);
    const root = roots.find((candidate) => candidate.id === saved) || roots[0] || null;
    S.activeRootIds[mode] = root ? root.id : null;
  }
}

async function loadRoots() {
  S.roots = await api("/api/library/roots");
  initializeActiveRoots();
  renderRootSelectors();
}

function renderRootSelectorInto(select) {
  if (!select) return;
  select.innerHTML = "";
  const roots = rootsForMode();
  if (!roots.length) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = S.mode === "comic"
      ? "漫画フォルダが未登録"
      : "動画フォルダが未登録";
    select.appendChild(option);
    select.disabled = true;
    return;
  }
  select.disabled = false;
  for (const root of roots) {
    const option = document.createElement("option");
    option.value = root.id;
    option.textContent = root.display_name;
    select.appendChild(option);
  }
  const active = activeRootForMode();
  select.value = active ? active.id : roots[0].id;
}

function renderRootSelectors() {
  renderRootSelectorInto($("sel-library-root"));
  renderRootSelectorInto($("sel-library-root-mobile"));
}

function resetNavigationState() {
  S.selectedId = null;
  S.readingItemId = null;
  S.folderId = null;
  S.folderInfo = null;
  S.items = [];
  S.history = [];
  S.histIndex = -1;
}

function pushHistory(folderId) {
  if (S.history[S.histIndex] === folderId) return;
  S.history = S.history.slice(0, S.histIndex + 1);
  S.history.push(folderId);
  S.histIndex = S.history.length - 1;
  updateNavButtons();
}

function setDisabled(id, disabled) {
  const el = $(id);
  if (el) el.disabled = disabled;
}

function updateNavButtons() {
  const backDisabled = S.histIndex <= 0;
  const forwardDisabled = S.histIndex >= S.history.length - 1;
  const upDisabled = !(S.folderInfo && S.folderInfo.parent_id);
  setDisabled("btn-folder-back", backDisabled);
  setDisabled("btn-folder-forward", forwardDisabled);
  setDisabled("btn-folder-up", upDisabled);
  setDisabled("btn-mobile-back", backDisabled);
  setDisabled("btn-mobile-forward", forwardDisabled);
  setDisabled("btn-mobile-up", upDisabled);
}

const SORT_STORAGE_KEY = "framedeck.sort";
const FILTER_STORAGE_KEY = "framedeck.filter";

function restoreListPreferences() {
  const sort = localStorage.getItem(SORT_STORAGE_KEY);
  const filter = localStorage.getItem(FILTER_STORAGE_KEY);
  if (sort && [...$("sel-sort").options].some((o) => o.value === sort)) {
    $("sel-sort").value = sort;
  }
  if (filter && [...$("sel-filter").options].some((o) => o.value === filter)) {
    $("sel-filter").value = filter;
  }
}

async function loadFolder(folderId, { remember = true } = {}) {
  if (!folderId) return;
  const sort = $("sel-sort").value;
  const filter = $("sel-filter").value;
  localStorage.setItem(SORT_STORAGE_KEY, sort);
  localStorage.setItem(FILTER_STORAGE_KEY, filter);
  const query = ($("library-search")?.value || "").trim();
  const params = new URLSearchParams({
    folder_id: folderId,
    mode: S.mode,
    sort,
    filter,
  });
  if (query) params.set("query", query);
  try {
    const data = await api(`/api/library/items?${params.toString()}`);
    const sameFolder = S.folderId === folderId;
    S.folderId = folderId;
    S.folderInfo = data.folder;
    S.items = data.items;
    // 移動したら選択は破棄、同じフォルダの再読込では残す
    const available = new Set(S.items.map((i) => i.id));
    S.checked = new Set(
      sameFolder ? [...S.checked].filter((id) => available.has(id)) : []
    );
    if (remember) pushHistory(folderId);
    renderBreadcrumb();
    renderList();
    updateNavButtons();
  } catch (e) {
    toast(`フォルダを読めません: ${e.message}`, true);
  }
}

function renderBreadcrumb() {
  const info = S.folderInfo;
  // relative_path は末尾がフォルダ名なので、それだけで階層が分かる
  const text = info ? (info.relative_path || info.display_name) : "";
  $("breadcrumb").textContent = text;
  if ($("breadcrumb-mobile")) $("breadcrumb-mobile").textContent = text;
}

function itemIcon(item) {
  if (item.media_type === "folder") return "📁";
  if (item.media_type === "video") return "🎬";
  return "📦";
}

function renderList() {
  const list = $("item-list");
  list.innerHTML = "";
  $("library-empty").classList.toggle("hidden", S.items.length > 0);
  for (const item of S.items) {
    const li = document.createElement("li");
    li.dataset.id = item.id;
    if (item.id === S.selectedId) li.classList.add("selected");
    if (item.id === S.readingItemId) li.classList.add("reading");
    if (S.selectMode && S.checked.has(item.id)) li.classList.add("checked");

    if (S.selectMode) {
      const check = document.createElement("input");
      check.type = "checkbox";
      check.className = "item-check";
      check.checked = S.checked.has(item.id);
      check.setAttribute("aria-label", `${item.display_name} を選択`);
      check.onclick = (e) => {
        e.stopPropagation();
        toggleChecked(item.id, check.checked);
      };
      li.appendChild(check);
    }

    const icon = document.createElement("span");
    icon.className = "item-icon";
    icon.textContent = itemIcon(item);
    const name = document.createElement("span");
    name.className = "item-name";
    name.textContent = item.display_name;
    name.title = item.display_name;
    const stars = document.createElement("span");
    stars.className = "item-stars" + (item.rating ? "" : " none");
    stars.textContent = item.stars;
    stars.title = "タップして評価";
    stars.onclick = (e) => {
      e.stopPropagation();
      openRatingSheet(item);
    };

    li.append(icon, name, stars);
    li.onclick = () => {
      if (S.selectMode) toggleChecked(item.id, !S.checked.has(item.id));
      else activateItem(item);
    };
    li.ondblclick = () => { if (S.selectMode) activateItem(item); };
    list.appendChild(li);
  }
  updateLibraryCount();
  updateBulkBar();
}

function updateLibraryCount() {
  const label = $("library-count");
  if (!label) return;
  const folders = S.items.filter((i) => i.media_type === "folder").length;
  const files = S.items.length - folders;
  label.textContent = S.items.length
    ? `${folders ? `📁${folders} · ` : ""}${files} 件`
    : "";
}

function selectItem(id) {
  S.selectedId = id;
  renderList();
  updateStarBar();
}

async function activateItem(item) {
  selectItem(item.id);
  if (item.media_type === "folder") {
    await loadFolder(item.id);
    return;
  }
  closeMobileDrawer();
  if (item.media_type === "comic") await openComic(item);
  else if (item.media_type === "video") await openVideo(item);
}

function clearCurrentViewer() {
  clearComicBoundaryState();
  S.comic.state = null;
  stopVideo();
  $("comic-viewer").classList.add("hidden");
  $("video-player").classList.add("hidden");
  $("viewer-placeholder").classList.remove("hidden");
}

function showMissingLibraryRoot(mode) {
  resetNavigationState();
  renderList();
  renderBreadcrumb();
  updateNavButtons();
  clearCurrentViewer();
  $("placeholder-icon").textContent = mode === "comic" ? "📖" : "▶";
  $("placeholder-text").textContent = mode === "comic"
    ? "漫画フォルダを設定してください"
    : "動画フォルダを設定してください";
}

async function switchLibraryRoot(rootId, { closeDrawer = true } = {}) {
  const root = rootsForMode().find((candidate) => candidate.id === rootId);
  if (!root) {
    toast("ライブラリが見つかりません", true);
    return;
  }
  S.activeRootIds[S.mode] = root.id;
  saveActiveRootId(S.mode, root.id);
  resetNavigationState();
  clearCurrentViewer();
  renderRootSelectors();
  await loadFolder(root.id, { remember: true });
  if (closeDrawer) closeMobileDrawer();
  toast(`ライブラリを切り替えました: ${root.display_name}`);
}

/* 前回開いていたフォルダ(サーバ保存)から再開する。
   設定が「ルート」のとき、または保存先が消えている場合は null。 */
async function resumeFolderForMode(mode = S.mode) {
  try {
    const result = await api(`/api/library/start-folder?mode=${encodeURIComponent(mode)}`);
    return result?.folder_id ? result : null;
  } catch (e) {
    return null;
  }
}

async function switchToActiveRoot({ announceResume = false } = {}) {
  const root = activeRootForMode();
  renderRootSelectors();
  if (!root) {
    showMissingLibraryRoot(S.mode);
    return;
  }
  const resume = await resumeFolderForMode();
  if (resume && rootsForMode().some((r) => r.id === resume.root_id)) {
    if (resume.root_id !== root.id) {
      S.activeRootIds[S.mode] = resume.root_id;
      saveActiveRootId(S.mode, resume.root_id);
      renderRootSelectors();
    }
    resetNavigationState();
    clearCurrentViewer();
    await loadFolder(resume.folder_id, { remember: true });
    if (announceResume && resume.relative_path) {
      toast(`前回の場所から再開: ${resume.relative_path}`);
    }
    return;
  }
  await switchLibraryRoot(root.id, { closeDrawer: false });
}

/* ================= star rating ================= */
function fillStarBar(bar, rating, onPick) {
  bar.innerHTML = "";
  for (let n = 1; n <= 5; n++) {
    const star = document.createElement("span");
    star.className = "star" + (n <= (rating || 0) ? " on" : "");
    star.textContent = "★";
    star.dataset.n = n;
    star.setAttribute("role", "button");
    star.setAttribute("aria-label", `★${n}`);
    star.onclick = () => onPick(n);
    bar.appendChild(star);
  }
  const clear = document.createElement("span");
  clear.className = "star-clear";
  clear.textContent = "✕";
  clear.title = "評価を解除";
  clear.setAttribute("role", "button");
  clear.setAttribute("aria-label", "評価を解除");
  clear.onclick = () => onPick(null);
  bar.appendChild(clear);
}

function buildStarBar() {
  for (const id of ["star-bar", "star-bar-mobile"]) {
    const bar = $(id);
    if (bar) fillStarBar(bar, 0, (rating) => applyRating(rating));
  }
}

function updateStarBar() {
  const item = S.items.find((i) => i.id === S.selectedId);
  const rating = item ? item.rating || 0 : 0;
  for (const id of ["star-bar", "star-bar-mobile"]) {
    const bar = $(id);
    if (!bar) continue;
    for (const star of bar.querySelectorAll(".star")) {
      star.classList.toggle("on", Number(star.dataset.n) <= rating);
    }
  }
}

async function setItemRating(itemId, rating) {
  await api(`/api/library/items/${itemId}/rating`, { json: { rating } });
  await loadFolder(S.folderId, { remember: false });
  updateStarBar();
}

async function applyRating(rating) {
  if (!S.selectedId) { toast("項目を選択してください"); return; }
  try {
    await setItemRating(S.selectedId, rating);
  } catch (e) {
    toast(`評価の設定に失敗: ${e.message}`, true);
  }
}

/* 一覧の★をタップして、その項目を開かずに評価する(モバイル対応の要) */
function openRatingSheet(item) {
  const wrap = document.createElement("div");
  wrap.className = "rating-sheet";
  const name = document.createElement("div");
  name.className = "sheet-name";
  name.textContent = item.display_name;
  const bar = document.createElement("div");
  bar.className = "star-bar";
  const apply = async (rating) => {
    closeModal();
    try {
      await setItemRating(item.id, rating);
      toast(rating ? `★${rating} を設定しました` : "評価を解除しました");
    } catch (e) {
      toast(`評価の設定に失敗: ${e.message}`, true);
    }
  };
  fillStarBar(bar, item.rating, apply);
  wrap.append(name, bar);
  showModal("評価", wrap, [{ label: "閉じる", onClick: closeModal }]);
}

/* ================= 複数選択 / 一括操作 ================= */
function setSelectMode(enabled) {
  S.selectMode = Boolean(enabled);
  if (!S.selectMode) S.checked.clear();
  $("btn-select-mode")?.classList.toggle("active", S.selectMode);
  $("library-bulk-bar")?.classList.toggle("hidden", !S.selectMode);
  renderList();
}

function toggleChecked(id, checked) {
  if (checked) S.checked.add(id);
  else S.checked.delete(id);
  const row = $("item-list").querySelector(`[data-id="${CSS.escape(id)}"]`);
  if (row) {
    row.classList.toggle("checked", checked);
    const box = row.querySelector(".item-check");
    if (box) box.checked = checked;
  }
  updateBulkBar();
}

function updateBulkBar() {
  const count = S.checked.size;
  const label = $("bulk-count");
  if (label) label.textContent = `${count} 件を選択中`;
  const button = $("btn-bulk-delete");
  if (button) {
    button.disabled = count === 0;
    button.textContent = count ? `選択した ${count} 件を削除` : "選択を削除";
  }
}

function replaceSelection(ids) {
  S.checked = new Set(ids.filter((id) => S.items.some((i) => i.id === id)));
  renderList();
}

function selectLowRatedItems() {
  const targets = S.items.filter((i) => i.rating && i.rating <= 2);
  if (!targets.length) {
    toast("★2以下の項目はありません");
    return;
  }
  replaceSelection(targets.map((i) => i.id));
  toast(`★2以下を ${targets.length} 件選択しました`);
}

function formatDate(seconds) {
  if (!seconds) return "";
  const d = new Date(seconds * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}/${pad(d.getMonth() + 1)}/${pad(d.getDate())}`;
}

async function selectDuplicates() {
  if (!S.folderId) return;
  const params = new URLSearchParams({
    folder_id: S.folderId,
    mode: S.mode,
    filter: $("sel-filter").value,
  });
  const query = ($("library-search")?.value || "").trim();
  if (query) params.set("query", query);
  let data;
  try {
    data = await api(`/api/library/duplicates?${params.toString()}`);
  } catch (e) {
    toast(`重複を抽出できません: ${e.message}`, true);
    return;
  }
  if (!data.groups.length) {
    toast("重複候補は見つかりませんでした");
    return;
  }

  const wrap = document.createElement("div");
  const summary = document.createElement("div");
  summary.className = "dup-summary";
  summary.textContent =
    `${data.groups.length} グループ / 選択候補 ${data.select_ids.length} 件。` +
    `名前の違いが${data.max_distance}文字以内(拡張子の違いは数えない)で、` +
    "含まれる数値が一致する項目を同一とみなし、" +
    "更新日が古い方を選択します(削除はしません)。";
  wrap.appendChild(summary);
  for (const group of data.groups) {
    const list = document.createElement("ul");
    list.className = "dup-group";
    for (const item of group.items) {
      const li = document.createElement("li");
      const tag = document.createElement("span");
      tag.className = `dup-tag ${item.keep ? "keep" : "old"}`;
      tag.textContent = item.keep ? "残す" : "選択";
      const name = document.createElement("span");
      name.className = "dup-name";
      name.textContent = item.display_name;
      const date = document.createElement("span");
      date.className = "dup-date";
      date.textContent = formatDate(item.modified_at);
      li.append(tag, name, date);
      list.appendChild(li);
    }
    wrap.appendChild(list);
  }
  showModal("重複候補", wrap, [
    { label: "キャンセル", onClick: closeModal },
    {
      label: "古い方を選択する",
      kind: "primary",
      onClick: () => {
        closeModal();
        setSelectMode(true);
        replaceSelection(data.select_ids);
        toast(`${S.checked.size} 件を選択しました。内容を確認して削除してください`);
      },
    },
  ]);
}

async function requestBulkDelete() {
  const ids = [...S.checked];
  if (!ids.length) { toast("削除する項目を選択してください"); return; }
  let req;
  try {
    req = await api("/api/library/items/bulk-delete-request", { json: { item_ids: ids } });
  } catch (e) { toast(e.message, true); return; }

  const body = document.createElement("div");
  const head = document.createElement("div");
  head.textContent = req.to_trash
    ? `${req.count} 件をゴミ箱へ移動します。よろしいですか?`
    : `${req.count} 件をディスクから完全に削除します。この操作は元に戻せません。`;
  const list = document.createElement("ul");
  list.className = "delete-preview";
  for (const name of req.display_names) {
    const li = document.createElement("li");
    li.textContent = name;
    list.appendChild(li);
  }
  body.append(head, list);
  showModal("一括削除の確認", body, [
    { label: "キャンセル", onClick: closeModal },
    {
      label: req.to_trash ? `${req.count} 件をゴミ箱へ` : `${req.count} 件を完全に削除`,
      kind: "danger",
      onClick: async () => {
        closeModal();
        try {
          const result = await api(
            `/api/library/items/bulk-delete?token=${encodeURIComponent(req.token)}`,
            { json: { item_ids: req.item_ids } }
          );
          S.checked.clear();
          S.selectedId = null;
          await loadFolder(S.folderId, { remember: false });
          if (result.failed.length) {
            toast(`${result.deleted.length} 件削除、${result.failed.length} 件失敗`, true);
          } else {
            toast(`${result.deleted.length} 件削除しました`);
          }
        } catch (e) { toast(`削除に失敗: ${e.message}`, true); }
      },
    },
  ]);
}

$("btn-select-mode").onclick = () => setSelectMode(!S.selectMode);
$("btn-select-low-rated").onclick = selectLowRatedItems;
$("btn-select-duplicates").onclick = selectDuplicates;
$("btn-select-all").onclick = () => replaceSelection(S.items.map((i) => i.id));
$("btn-select-none").onclick = () => replaceSelection([]);
$("btn-bulk-delete").onclick = requestBulkDelete;

/* ================= delete ================= */
async function requestDelete() {
  if (!S.selectedId) { toast("削除する項目を選択してください"); return; }
  let req;
  try {
    req = await api(`/api/library/items/${S.selectedId}/delete-request`, { method: "POST" });
  } catch (e) { toast(e.message, true); return; }

  const body = document.createElement("div");
  body.textContent = req.to_trash
    ? `「${req.display_name}」をゴミ箱へ移動します。よろしいですか?`
    : `「${req.display_name}」をディスクから完全に削除します。この操作は元に戻せません。`;
  showModal("削除の確認", body, [
    { label: "キャンセル", onClick: closeModal },
    {
      label: req.to_trash ? "ゴミ箱へ移動" : "完全に削除",
      kind: "danger",
      onClick: async () => {
        closeModal();
        try {
          await api(`/api/library/items/${S.selectedId}?token=${req.token}`,
                    { method: "DELETE" });
          toast("削除しました");
          S.selectedId = null;
          await loadFolder(S.folderId, { remember: false });
        } catch (e) { toast(`削除に失敗: ${e.message}`, true); }
      },
    },
  ]);
}

/* ================= comic viewer ================= */
function showViewer(kind) {
  $("viewer-placeholder").classList.add("hidden");
  $("comic-viewer").classList.toggle("hidden", kind !== "comic");
  $("video-player").classList.toggle("hidden", kind !== "video");
  if (kind !== "video") stopVideo();
}

function comicProfileOptions() {
  const prefix = S.uiProfile === "mobile" ? "comic_mobile" : "comic_desktop";
  return {
    view_mode: S.settings[`${prefix}_view_mode`] || S.settings.view_mode,
    reading_direction: S.settings.reading_direction,
    cover_as_single_page: S.settings.cover_as_single_page,
  };
}

async function applyComicProfileOptions(state) {
  const options = comicProfileOptions();
  if (!state?.session_id) return state;
  if (state.view_mode === options.view_mode &&
      state.reading_direction === options.reading_direction) {
    return state;
  }
  return api(`/api/comics/session/${state.session_id}/options`, {
    method: "PATCH",
    json: options,
  });
}

async function openComic(item) {
  try {
    let result = await api("/api/comics/session", { json: { item_id: item.id } });
    if (result.requires_choice) {
      chooseEntry(item, result.entries);
      return;
    }
    result = await applyComicProfileOptions(result);
    S.readingItemId = item.id;
    setComicState(result);
  } catch (e) {
    toast(`漫画を開けません: ${e.message}`, true);
  }
}

function chooseEntry(item, entries) {
  const list = document.createElement("ul");
  list.className = "choice-list";
  for (const entry of entries) {
    const li = document.createElement("li");
    const icon = entry.source_type === "image_folder" ? "📁" : "📦";
    li.textContent = `${icon} ${entry.label}`;
    li.onclick = async () => {
      closeModal();
      try {
        let state = await api("/api/comics/session", {
          json: { item_id: item.id, entry_id: entry.id },
        });
        state = await applyComicProfileOptions(state);
        S.readingItemId = item.id;
        setComicState(state);
      } catch (e) { toast(e.message, true); }
    };
    list.appendChild(li);
  }
  showModal("開く漫画を選択してください", list,
            [{ label: "キャンセル", onClick: closeModal }]);
}

function updateComicView(state) {
  S.comic.state = state;
  showViewer("comic");
  renderComicPages();
  updateComicControls();
  preloadComicPages();
}

function clearComicBoundaryState() {
  if (S.comic.boundaryTimer) clearTimeout(S.comic.boundaryTimer);
  S.comic.boundaryTimer = null;
  S.comic.boundaryIntent = null;
}

function setComicState(state) {
  clearComicBoundaryState();
  updateComicView(state);
  if (state.root_item_id && S.items.some((i) => i.id === state.root_item_id)) {
    S.readingItemId = state.root_item_id;
    S.selectedId = state.root_item_id;
    updateStarBar();
    renderList();
  }
}

function focusLibraryItem(itemId) {
  S.selectedId = itemId;
  S.readingItemId = itemId;
  renderList();
  updateStarBar();
  const row = $("item-list").querySelector(`[data-id="${CSS.escape(itemId)}"]`);
  row?.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

async function syncLibraryToComicState(state) {
  if (!state.root_item_id) return;
  if (!S.items.some((item) => item.id === state.root_item_id) && state.root_folder_id) {
    await loadFolder(state.root_folder_id, { remember: true });
  }
  focusLibraryItem(state.root_item_id);
}

async function applyComicEntryState(state) {
  clearComicBoundaryState();
  updateComicView(state);
  await syncLibraryToComicState(state);
}

function comicDeliveryProfile() {
  const prefix = S.uiProfile === "mobile" ? "comic_mobile" : "comic_desktop";
  return S.settings[`${prefix}_delivery_profile`] || (S.uiProfile === "mobile" ? "mobile" : "high");
}

function comicPageUrl(pageIndex, side = "full") {
  const state = S.comic.state;
  const base = `/api/comics/session/${state.session_id}/page/${pageIndex}`;
  if (S.settings.comic_delivery_mode === "original") {
    return side === "full" ? base : `${base}?split_side=${side}`;
  }
  const rect = $("comic-stage").getBoundingClientRect();
  const params = new URLSearchParams();
  params.set("width", String(Math.max(64, Math.round(rect.width || window.innerWidth))));
  params.set("height", String(Math.max(64, Math.round(rect.height || window.innerHeight))));
  params.set("dpr", String(Math.min(window.devicePixelRatio || 1, S.uiProfile === "mobile" ? 2 : 3)));
  params.set("profile", comicDeliveryProfile());
  params.set("format", S.settings.comic_output_format || "auto");
  params.set("auto_crop", String(S.settings.comic_auto_crop !== false));
  if (side !== "full") params.set("split_side", side);
  params.set("entry", state.entry_id || "");
  return `${base}?${params.toString()}`;
}

function comicVisiblePageSpecs(state) {
  const sides = state.visible_page_sides || [];
  return state.visible_pages.map((pageIndex, i) => ({
    index: pageIndex,
    side: sides[i] || "full",
  }));
}


function calculateSpreadLayout({ pages, availableWidth, availableHeight }) {
  if (pages.length !== 2 || availableWidth <= 0 || availableHeight <= 0) {
    return { widths: [], height: 0 };
  }
  const safePages = pages.map((page) => ({
    width: Math.max(1, page.width || 1),
    height: Math.max(1, page.height || 1),
  }));
  let height = Math.floor(Math.min(
    availableHeight,
    Math.max(safePages[0].height, safePages[1].height)
  ));
  let widths = safePages.map((page) => Math.max(1, Math.round(page.width * height / page.height)));
  const combined = widths[0] + widths[1];
  if (combined > availableWidth) {
    const scale = availableWidth / combined;
    height = Math.max(1, Math.floor(height * scale));
    const leftWidth = Math.max(1, Math.floor(widths[0] * scale));
    widths = [leftWidth, Math.max(1, Math.floor(availableWidth) - leftWidth)];
  }
  return { widths, height };
}

function layoutComicSpread() {
  const container = $("comic-pages");
  const images = [...container.querySelectorAll("img")];
  if (images.length !== 2) {
    for (const img of images) {
      img.style.width = "";
      img.style.height = "";
    }
    return;
  }
  if (images.some((img) => !img.complete || !img.naturalWidth || !img.naturalHeight)) return;
  const rect = $("comic-stage").getBoundingClientRect();
  const layout = calculateSpreadLayout({
    pages: images.map((img) => ({ width: img.naturalWidth, height: img.naturalHeight })),
    availableWidth: Math.floor(rect.width),
    availableHeight: Math.floor(rect.height),
  });
  if (!layout.height || layout.widths.length !== 2) return;
  images[0].style.width = `${layout.widths[0]}px`;
  images[0].style.height = `${layout.height}px`;
  images[1].style.width = `${layout.widths[1]}px`;
  images[1].style.height = `${layout.height}px`;
}

function renderComicPages() {
  const state = S.comic.state;
  const container = $("comic-pages");
  container.innerHTML = "";
  $("comic-msg").classList.add("hidden");
  let pages = comicVisiblePageSpecs(state);
  if (state.reading_direction === "rtl" && pages.length === 2) {
    pages.reverse();
  }
  container.classList.toggle("two", pages.length === 2);
  for (const page of pages) {
    const img = document.createElement("img");
    img.alt = `page ${page.index + 1}`;
    img.draggable = false;
    img.decoding = "async";
    img.fetchPriority = "high";
    img.onload = layoutComicSpread;
    img.onerror = () => {
      $("comic-msg").textContent = "画像を読み込めませんでした";
      $("comic-msg").classList.remove("hidden");
    };
    container.appendChild(img);
    img.src = comicPageUrl(page.index, page.side);
    if (img.complete) requestAnimationFrame(layoutComicSpread);
  }
  requestAnimationFrame(layoutComicSpread);
}

function preloadComicPages() {
  const state = S.comic.state;
  const preload = (index, side = "full") => {
    const img = new Image();
    img.fetchPriority = "low";
    img.decoding = "async";
    img.src = comicPageUrl(index, side);
  };
  const splitActive = (state.visible_page_sides || []).some((s) => s !== "full");
  const last = state.visible_pages[state.visible_pages.length - 1];
  if (splitActive) {
    // 分割表示中: 現ページと次ページの両面を先読みする
    for (const side of ["right", "left"]) {
      preload(last, side);
      if (last + 1 < state.page_count) preload(last + 1, side);
    }
  }
  for (let i = 1; i <= 4; i++) {
    const idx = last + i;
    if (idx < state.page_count) preload(idx);
  }
  const first = state.visible_pages[0];
  for (let i = 1; i <= 2; i++) {
    const idx = first - i;
    if (idx >= 0) preload(idx);
  }
}

function updateComicControls() {
  const state = S.comic.state;
  const seek = $("comic-seek");
  seek.max = Math.max(0, state.page_count - 1);
  seek.value = state.page_index;
  seek.classList.toggle("rtl", state.reading_direction === "rtl");
  const first = state.visible_pages[0] + 1;
  const last = state.visible_pages[state.visible_pages.length - 1] + 1;
  const range = first === last ? `${first}` : `${first}-${last}`;
  $("comic-page-label").textContent =
    `${range} / ${state.page_count}  [${state.entry_index + 1}/${state.entry_count}]`;
  $("comic-title").textContent = state.title;
  $("btn-view-mode").classList.toggle("active", state.view_mode === "single");
  $("btn-direction").textContent = state.reading_direction === "rtl" ? "⇤" : "⇥";
  // ボタンは見た目の方向基準: シークバーの進行方向(RTLでは左=進む)と
  // 一致するよう、綴じ方向に応じてラベル/ツールチップ/無効状態を割り当てる
  const rtl = state.reading_direction === "rtl";
  const stop = S.settings.comic_sequence_end_behavior === "stop";
  const prevEntryDisabled = !state.has_previous_entry && stop;
  const nextEntryDisabled = !state.has_next_entry && stop;
  $("btn-comic-entry-left").disabled = rtl ? nextEntryDisabled : prevEntryDisabled;
  $("btn-comic-entry-right").disabled = rtl ? prevEntryDisabled : nextEntryDisabled;
  $("btn-comic-entry-left").title = rtl ? "次の漫画 (N)" : "前の漫画 (P)";
  $("btn-comic-entry-right").title = rtl ? "前の漫画 (P)" : "次の漫画 (N)";
  $("btn-comic-spread-left").title = rtl ? "見開きを送る" : "見開きを戻す";
  $("btn-comic-spread-right").title = rtl ? "見開きを戻す" : "見開きを送る";
  $("btn-comic-page-left").textContent = rtl ? "+1" : "-1";
  $("btn-comic-page-right").textContent = rtl ? "-1" : "+1";
  $("btn-comic-page-left").title = rtl ? "1ページ進む" : "1ページ戻す";
  $("btn-comic-page-right").title = rtl ? "1ページ戻す" : "1ページ進む";
}

async function comicCall(path, body) {
  const state = S.comic.state;
  if (!state) return null;
  try {
    const result = await api(
      `/api/comics/session/${state.session_id}/${path}`,
      { json: body || {} }
    );
    return result;
  } catch (e) {
    toast(e.message, true);
    return null;
  }
}

function armComicBoundary(direction) {
  clearComicBoundaryState();
  S.comic.boundaryIntent = direction;
  S.comic.boundaryTimer = setTimeout(clearComicBoundaryState, 1500);
  toast(
    direction === "next"
      ? "最後のページです。もう一度進むと次の漫画へ移動します"
      : "最初のページです。もう一度戻ると前の漫画へ移動します"
  );
}

async function performComicPageAction(apiPath, direction) {
  const before = S.comic.state;
  if (!before) return;
  if (S.comic.boundaryIntent && S.comic.boundaryIntent !== direction) {
    clearComicBoundaryState();
  }
  const state = await comicCall(apiPath);
  if (!state) {
    clearComicBoundaryState();
    return;
  }
  const didMove =
    state.entry_id !== before.entry_id ||
    state.page_index !== before.page_index ||
    JSON.stringify(state.visible_page_sides || []) !==
      JSON.stringify(before.visible_page_sides || []);
  if (didMove) {
    setComicState(state);
    return;
  }
  const canCross = direction === "next"
    ? state.has_next_entry || S.settings.comic_sequence_end_behavior === "wrap"
    : state.has_previous_entry || S.settings.comic_sequence_end_behavior === "wrap";
  if (!canCross) {
    clearComicBoundaryState();
    toast(direction === "next"
      ? "最後の漫画の最後のページです"
      : "最初の漫画の先頭ページです");
    return;
  }
  if (S.comic.boundaryIntent === direction) {
    clearComicBoundaryState();
    if (direction === "next") await comicNextEntry();
    else await comicPrevEntry();
    return;
  }
  armComicBoundary(direction);
}

async function comicSpreadForward() {
  return performComicPageAction("next-spread", "next");
}
async function comicSpreadBackward() {
  return performComicPageAction("previous-spread", "previous");
}
async function comicShiftForward() {
  clearComicBoundaryState();
  const state = await comicCall("next-page");
  if (state) setComicState(state);
}
async function comicShiftBackward() {
  clearComicBoundaryState();
  const state = await comicCall("previous-page");
  if (state) setComicState(state);
}

async function navigateComicEntry(delta, source = "ui") {
  if (!S.comic.state || S.comic.entryNavigationBusy) return;
  const before = S.comic.state;
  S.comic.entryNavigationBusy = true;
  clearComicBoundaryState();
  try {
    const state = await comicCall(delta > 0 ? "next-entry" : "previous-entry");
    if (!state) return;
    if (delta > 0 && state.at_sequence_end) {
      toast(S.settings.comic_sequence_end_behavior === "prompt"
        ? "シーケンスの末尾です(設定: 確認)"
        : "最後の漫画です");
      updateComicView(state);
      return;
    }
    if (delta < 0 && state.at_sequence_start) {
      toast("最初の漫画です");
      updateComicView(state);
      return;
    }
    if (S.settings.debug_aux_mouse) {
      console.debug("[FrameDeck] comic entry navigation", {
        source,
        before: before.entry_id,
        after: state.entry_id,
        title: state.title,
      });
    }
    await applyComicEntryState(state);
  } finally {
    S.comic.entryNavigationBusy = false;
  }
}

function comicNextEntry() {
  return navigateComicEntry(+1, "button");
}

function comicPrevEntry() {
  return navigateComicEntry(-1, "button");
}

/* comic operations wiring */
function comicNextAction() { comicSpreadForward(); }
function comicPrevAction() { comicSpreadBackward(); }
function comicTapLeft() {
  if (!S.comic.state) return;
  const forward = S.comic.state.reading_direction === "rtl";
  (S.settings.comic_tap_reverse ? !forward : forward)
    ? comicSpreadForward()
    : comicSpreadBackward();
}
function comicTapRight() {
  if (!S.comic.state) return;
  const forward = S.comic.state.reading_direction !== "rtl";
  (S.settings.comic_tap_reverse ? !forward : forward)
    ? comicSpreadForward()
    : comicSpreadBackward();
}
function comicShiftByVisualDirection(direction) {
  if (!S.comic.state) return;
  const forward = S.comic.state.reading_direction === "rtl"
    ? direction === "left"
    : direction === "right";
  forward ? comicShiftForward() : comicShiftBackward();
}

async function comicOptionsPatch(body) {
  const state = S.comic.state;
  if (!state) return null;
  try {
    return await api(`/api/comics/session/${state.session_id}/options`,
                     { method: "PATCH", json: body });
  } catch (e) { toast(e.message, true); return null; }
}

async function toggleViewMode() {
  const state = S.comic.state;
  if (!state) return;
  const next = state.view_mode === "spread" ? "single" : "spread";
  const result = await comicOptionsPatch({ view_mode: next });
  if (result) { setComicState(result); toast(next === "single" ? "単ページ表示" : "見開き表示"); }
}
$("btn-view-mode").onclick = toggleViewMode;

$("btn-direction").onclick = async () => {
  const state = S.comic.state;
  if (!state) return;
  const next = state.reading_direction === "rtl" ? "ltr" : "rtl";
  const result = await comicOptionsPatch({ reading_direction: next });
  if (result) { setComicState(result); toast(next === "rtl" ? "右綴じ (RTL)" : "左綴じ (LTR)"); }
};

/* ボタンは見た目の方向で動作を決める(シークバーの進行方向と一致)。
   右綴じ(RTL)では左向き=進む、左綴じ(LTR)では右向き=進む。 */
function comicIsRtl() {
  return S.comic.state?.reading_direction === "rtl";
}
$("btn-comic-spread-left").onclick = () =>
  comicIsRtl() ? comicSpreadForward() : comicSpreadBackward();
$("btn-comic-spread-right").onclick = () =>
  comicIsRtl() ? comicSpreadBackward() : comicSpreadForward();
$("btn-comic-page-left").onclick = () =>
  comicIsRtl() ? comicShiftForward() : comicShiftBackward();
$("btn-comic-page-right").onclick = () =>
  comicIsRtl() ? comicShiftBackward() : comicShiftForward();
$("btn-comic-entry-left").onclick = () =>
  comicIsRtl() ? comicNextEntry() : comicPrevEntry();
$("btn-comic-entry-right").onclick = () =>
  comicIsRtl() ? comicPrevEntry() : comicNextEntry();
$("btn-comic-full").onclick = () => toggleFullscreen($("comic-viewer"));
function handleComicTapZone(e, action) {
  e.preventDefault();
  e.stopPropagation();
  action();
}
$("comic-tap-left").onclick = (e) => handleComicTapZone(e, comicTapLeft);
$("comic-tap-right").onclick = (e) => handleComicTapZone(e, comicTapRight);
for (const tapZone of [$("comic-tap-left"), $("comic-tap-right")]) {
  for (const eventName of ["pointerdown", "pointerup", "touchstart", "touchend"]) {
    tapZone.addEventListener(eventName, (e) => e.stopPropagation(), { passive: true });
  }
}
$("comic-tap-left").ondblclick = (e) => e.preventDefault();
$("comic-tap-right").ondblclick = (e) => e.preventDefault();

let comicSeekTimer = null;
$("comic-seek").addEventListener("input", () => {
  clearTimeout(comicSeekTimer);
  comicSeekTimer = setTimeout(async () => {
    const state = await comicCall("goto",
      { page_index: Number($("comic-seek").value) });
    if (state) setComicState(state);
  }, 160);
});

$("comic-stage").addEventListener("wheel", (e) => {
  e.preventDefault();
  const now = performance.now();
  if (now < S.comic.wheelLockedUntil) return;
  if (Math.abs(e.deltaY) < 10) return;
  S.comic.wheelLockedUntil = now + 180;
  if (e.deltaY > 0) comicSpreadForward();
  else comicSpreadBackward();
}, { passive: false });

$("comic-stage").addEventListener("dblclick", (e) => {
  if (e.target.closest(".tap-zone")) return;
  toggleFullscreen($("comic-viewer"));
});

/* swipe */
let touchStart = null;
$("comic-stage").addEventListener("touchstart", (e) => {
  if (e.touches.length === 1) {
    touchStart = { x: e.touches[0].clientX, y: e.touches[0].clientY, t: Date.now() };
  }
}, { passive: true });
$("comic-stage").addEventListener("touchend", (e) => {
  if (!touchStart) return;
  const dx = e.changedTouches[0].clientX - touchStart.x;
  const dy = e.changedTouches[0].clientY - touchStart.y;
  const dt = Date.now() - touchStart.t;
  touchStart = null;
  if (dt < 600 && Math.abs(dx) > 60 && Math.abs(dx) > Math.abs(dy) * 1.5) {
    // スワイプ方向 = めくる方向(RTLでは左スワイプ=進む)
    if (dx < 0) comicTapLeft(); else comicTapRight();
  }
}, { passive: true });

/* ================= video player ================= */
const video = $("video");

function fmtTime(seconds) {
  if (!isFinite(seconds) || seconds < 0) seconds = 0;
  const s = Math.floor(seconds % 60).toString().padStart(2, "0");
  const m = Math.floor(seconds / 60) % 60;
  const h = Math.floor(seconds / 3600);
  return h > 0 ? `${h}:${m.toString().padStart(2, "0")}:${s}` : `${m}:${s}`;
}

function transcodeStreamUrl(itemId, seconds) {
  const params = new URLSearchParams({
    start: Math.max(0, Number(seconds) || 0).toFixed(2),
    session: CLIENT_SESSION_ID,
  });
  // 上限未指定 = 原寸(コーデック変換のみ)
  if (S.video.maxHeight) params.set("max_height", String(S.video.maxHeight));
  if (S.video.maxWidth) params.set("max_width", String(S.video.maxWidth));
  // 端末が映像コーデックを再生できるなら再エンコードしない(remux)
  if (S.video.copyVideo) params.set("copy_video", "1");
  if (S.video.copyAudio) params.set("copy_audio", "1");
  if (S.video.smoothFps) params.set("smooth_fps", String(S.video.smoothFps));
  return `/api/videos/${itemId}/stream-transcode?${params.toString()}`;
}

async function openVideo(item) {
  const token = ++S.video.loadToken;
  // 旧動画の変換停止を先に通知する。サーバ側のowner置換も最終保証になる。
  stopVideo();
  showViewer("video");
  $("video-msg").classList.add("hidden");
  $("video-badge").classList.add("hidden");
  $("video-spinner").classList.remove("hidden");
  S.readingItemId = item.id;
  renderList();
  let detail;
  try {
    detail = await api(`/api/videos/${item.id}`);
  } catch (e) {
    if (token !== S.video.loadToken) return;
    $("video-spinner").classList.add("hidden");
    $("video-msg").textContent = `動画情報を取得できません\n${e.message}`;
    $("video-msg").classList.remove("hidden");
    return;
  }
  // 読み込み中に別の動画へ切り替わっていたら、この応答は捨てる
  // (古い応答が video.src を上書きして再生が止まるのを防ぐ)
  if (token !== S.video.loadToken) return;
  S.video.item = item;
  S.video.info = detail.info;
  S.video.duration = detail.info.duration_seconds || 0;
  S.video.offset = 0;
  S.video.pendingSeekSeconds = null;
  $("video-title").textContent = item.display_name;

  const resume = detail.resume_position || 0;
  if ($("sel-video-quality")) $("sel-video-quality").value = S.video.quality || "auto";
  let playbackProfile = null;
  let network = null;
  // 直接再生できるかは端末側の申告を反映したサーバ判定を使う
  // (サーバ単独の推定だと、再生できるHEVCを不要に変換してしまう)
  let canDirectPlay = detail.info.direct_play;
  let directPlayReason = detail.info.direct_play_reason;
  S.video.copyVideo = false;
  S.video.copyAudio = false;
  S.video.transcodeFallback = false;
  S.video.hlsFallback = false;
  S.video.mobileStableOriginal = false;
  S.video.desktopStableOriginal = false;
  S.video.starveTimes = [];
  try {
    const hints = clientMediaHints();
    if (S.video.quality && S.video.quality !== "auto") hints.requestedProfile = S.video.quality;
    const decision = await api(`/api/videos/${item.id}/playback-profile`, {
      json: hints,
    });
    playbackProfile = decision.profile;
    network = decision.network;
    canDirectPlay = decision.direct_play;
    directPlayReason = decision.direct_play_reason || directPlayReason;
    S.video.copyVideo = Boolean(decision.copy_video);
    S.video.copyAudio = Boolean(decision.copy_audio);
  } catch (e) {
    playbackProfile = null;
  }
  if (token !== S.video.loadToken) return;
  // iOSの原寸Direct Playはファイルの瞬間ビットレートによって
  // Range取得が間に合わない。1080p以下なら同じ寸法のHLSへ寄せる。
  if (!playbackProfile?.transcode && canDirectPlay &&
      shouldStabilizeMobileOriginal(detail.info)) {
    const stableProfile = hlsProfileForSource(detail.info.height);
    playbackProfile = {
      name: stableProfile,
      transcode: true,
      height: detail.info.height || null,
      width: detail.info.width || null,
      reason: "ios-original-hls-stability",
    };
    S.video.mobileStableOriginal = true;
  }
  if (playbackProfile && !playbackProfile.transcode && canDirectPlay &&
      shouldStabilizeDesktopOriginal()) {
    playbackProfile = {
      name: "original",
      transcode: true,
      height: null,
      width: null,
      reason: "desktop-original-remux-stability",
    };
    // direct_play=trueなら端末は映像・音声の両方を再生可能。
    // エンコードせずコンテナだけ連続配信向けに作り直す。
    S.video.copyVideo = true;
    S.video.copyAudio = true;
    S.video.desktopStableOriginal = true;
  }
  // 変換時の上限。原寸(null)ならスケーリングなしで配信する
  S.video.maxHeight = playbackProfile?.height || null;
  S.video.maxWidth = playbackProfile?.width || null;

  if (S.video.quality === "remux") {
    // 画質はそのまま(再エンコードなし)、コンテナだけ作り直して連続配信する。
    // 直接再生で不定期に引っかかる場合の逃げ道。データトラック等の
    // 余計なストリームも落ちるため、素の配信より素直に流れる
    playbackProfile = { name: "original", transcode: true, height: null, width: null };
    canDirectPlay = false;
    S.video.maxHeight = null;
    S.video.maxWidth = null;
    S.video.copyVideo = true;
    S.video.copyAudio = clientCodecSupport().audioCodecs.includes(
      (detail.info.audio_codec || "").toLowerCase());
  }

  // 画面との相性を先に判定する(なめらか変換を使うかの判断に必要)
  S.video.info = detail.info;
  await updateDisplaySync();
  if (token !== S.video.loadToken) return;
  S.video.smoothFps = detail.transcode_available ? smoothMotionTarget() : null;

  const wantsTranscode = Boolean(playbackProfile?.transcode) || Boolean(S.video.smoothFps);
  if (!wantsTranscode && canDirectPlay) {
    S.video.transcode = false;
    S.video.hls = false;
    video.src = `/api/videos/${item.id}/stream`;
    if (resume > 0) {
      video.addEventListener("loadedmetadata", () => {
        video.currentTime = resume;
      }, { once: true });
      toast(`続きから再生: ${fmtTime(resume)}`);
    }
  } else if (detail.transcode_available) {
    const original = !S.video.maxHeight;
    if (shouldUseNativeHls()) {
      const profile = original
        ? hlsProfileForSource(detail.info.height)
        : hlsProfileName(playbackProfile?.name || configuredVideoQuality());
      S.video.transcode = false;
      S.video.hls = true;
      S.video.hlsProfile = profile;
      // HLSは start 秒からの再生成で途中再生する(fMP4変換と同じoffset方式)
      S.video.offset = resume;
      video.src = hlsMasterUrl(item.id, profile, resume);
      if (resume > 0) toast(`続きから再生: ${fmtTime(resume)}`);
      $("video-badge").textContent = S.video.mobileStableOriginal
        ? `原寸安定配信 HLS ${profile}`
        : `HLS軽量配信 ${profile}`;
    } else {
      S.video.transcode = true;
      S.video.hls = false;
      S.video.offset = resume;
      if (S.video.smoothFps) S.video.copyVideo = false;   // 補間には再エンコードが要る
      video.src = transcodeStreamUrl(item.id, resume);
      if (S.video.quality === "remux") {
        $("video-badge").textContent =
          `原寸そのまま配信 (再多重化${S.video.copyAudio ? "" : " / 音声のみ変換"})`;
      } else if (S.video.smoothFps) {
        $("video-badge").textContent =
          `なめらか変換 ${S.video.smoothFps.toFixed(1)}fps (${detail.info.frame_rate.toFixed(2)}fps素材)`;
      } else if (S.video.desktopStableOriginal) {
        $("video-badge").textContent = "原寸安定配信 (無劣化・連続配信)";
      } else if (S.video.copyVideo) {
        // 映像は無変換。コンテナ(必要なら音声も)だけ入れ替えるので軽い
        $("video-badge").textContent =
          `原寸そのまま配信 (${detail.info.container} → mp4)`;
      } else if (original) {
        $("video-badge").textContent =
          `原寸変換配信 (${detail.info.video_codec || detail.info.container})`;
      } else {
        $("video-badge").textContent = `逐次軽量配信 ${playbackProfile.name}`;
        if (playbackProfile?.reason?.startsWith("encode-limited")) {
          $("video-badge").textContent += " · 実時間変換のため縮小";
        }
      }
      if (resume > 0) toast(`続きから再生: ${fmtTime(resume)}`);
    }
    $("video-badge").textContent += network === "cellular" ? " · モバイル回線" : "";
    $("video-badge").classList.remove("hidden");
  } else if (canDirectPlay) {
    S.video.transcode = false;
    S.video.hls = false;
    video.src = `/api/videos/${item.id}/stream`;
    $("video-badge").textContent = "直接再生";
    $("video-badge").classList.remove("hidden");
    if (resume > 0) {
      video.addEventListener("loadedmetadata", () => {
        video.currentTime = resume;
      }, { once: true });
      toast(`続きから再生: ${fmtTime(resume)}`);
    }
  } else {
    $("video-spinner").classList.add("hidden");
    $("video-msg").textContent =
      `この形式はブラウザで再生できません\n${directPlayReason}\n` +
      "ffmpegをインストールすると変換再生が可能になります";
    $("video-msg").classList.remove("hidden");
    return;
  }
  applyPlaybackRate();
  video.volume = Number($("video-volume").value) / 100;
  try { await video.play(); } catch (e) { /* 自動再生ブロックは無視 */ }
  startProgressTimer();
  startPlaybackWatchdog();
}

function currentPosition() {
  return S.video.offset + (video.currentTime || 0);
}
function videoDisplayPosition() {
  return S.video.pendingSeekSeconds ?? currentPosition();
}
function totalDuration() {
  if (S.video.transcode || S.video.hls) return S.video.duration;
  return video.duration || S.video.duration || 0;
}

function seekableDuration() {
  // 変換/HLS再生中は video.duration が生成済み範囲しか返さないため
  // ffprobe由来の全長を優先する
  const candidates = (S.video.transcode || S.video.hls)
    ? [S.video.duration, S.video.info?.duration_seconds, video.duration]
    : [video.duration, S.video.duration, S.video.info?.duration_seconds];
  for (const value of candidates) {
    if (Number.isFinite(value) && value > 0) return value;
  }
  return 0;
}

function finiteSeconds(value) {
  return Number.isFinite(value) && value > 0 ? value : 0;
}

function saveVideoProgress() {
  const item = S.video.item;
  if (!item) return;
  const payload = JSON.stringify({
    position_seconds: finiteSeconds(currentPosition()),
    duration_seconds: finiteSeconds(totalDuration()),
    playback_speed: Number.isFinite(video.playbackRate) ? video.playbackRate : 1.0,
  });
  navigator.sendBeacon?.(
    `/api/videos/${item.id}/progress`,
    new Blob([payload], { type: "application/json" })
  ) || api(`/api/videos/${item.id}/progress`, { json: JSON.parse(payload) }).catch(() => {});
}

function startProgressTimer() {
  stopProgressTimer();
  S.video.saveTimer = setInterval(() => {
    if (!video.paused) saveVideoProgress();
  }, 5000);
}
function stopProgressTimer() {
  if (S.video.saveTimer) { clearInterval(S.video.saveTimer); S.video.saveTimer = null; }
}

const PAUSE_CONVERSION_IDLE_MS = 60000;

function clearPauseConversionStop() {
  if (S.video.pauseStopTimer) clearTimeout(S.video.pauseStopTimer);
  S.video.pauseStopTimer = null;
}

function schedulePauseConversionStop() {
  clearPauseConversionStop();
  if ((!S.video.hls && !S.video.transcode) || !S.video.item) return;
  const itemId = S.video.item.id;
  S.video.pauseStopTimer = setTimeout(() => {
    S.video.pauseStopTimer = null;
    if (!S.video.item || S.video.item.id !== itemId || !video.paused) return;
    saveVideoProgress();
    requestTranscodeStop(itemId);
    S.video.pausedConversionStopped = true;
  }, PAUSE_CONVERSION_IDLE_MS);
}

function resumePausedConversion() {
  if (!S.video.pausedConversionStopped || !S.video.item) return false;
  const position = currentPosition();
  S.video.pausedConversionStopped = false;
  S.video.offset = position;
  clearVideoErrorRetry();
  video.src = S.video.hls
    ? hlsMasterUrl(S.video.item.id, S.video.hlsProfile || "720p", position)
    : transcodeStreamUrl(S.video.item.id, position);
  applyPlaybackRate();
  video.play().catch(() => {});
  startPlaybackWatchdog();
  return true;
}

function requestTranscodeStop(itemId) {
  // 生成中の変換ジョブ(HLS/fMP4)を止めて、CPUとキャッシュを解放する
  const url = `/api/videos/${itemId}/hls/stop?session=${encodeURIComponent(CLIENT_SESSION_ID)}`;
  if (!navigator.sendBeacon?.(url, "")) {
    fetch(url, {
      method: "POST",
      keepalive: true,
      credentials: "same-origin",
    }).catch(() => {});
  }
}

/* ---- 再生ストールの自動復帰 ----
   変換配信は、シークやファイル切替の連打でサーバ側の生成が取り残されると
   バッファが進まなくなることがある。進捗を監視し、止まったら同じ位置から
   読み直して復帰させる(アプリの再起動を不要にする)。 */
const WATCHDOG_INTERVAL_MS = 4000;
const WATCHDOG_STRIKES = 3;

function stopPlaybackWatchdog() {
  if (S.video.watchdogTimer) clearInterval(S.video.watchdogTimer);
  S.video.watchdogTimer = null;
  S.video.watchdogStrikes = 0;
}

function startPlaybackWatchdog() {
  stopPlaybackWatchdog();
  S.video.watchdogPosition = currentPosition();
  S.video.watchdogDropped = video.getVideoPlaybackQuality?.().droppedVideoFrames || 0;
  S.video.watchdogTimer = setInterval(() => {
    if (!S.video.item || video.paused || video.ended || S.video.recovering) {
      S.video.watchdogStrikes = 0;
      return;
    }
    // 表示落ち(描画が間に合っていない)の検知。供給不足とは原因が異なる
    const quality = video.getVideoPlaybackQuality?.();
    if (quality) {
      const dropped = quality.droppedVideoFrames - (S.video.watchdogDropped || 0);
      if (dropped > 2) recordGlitch("frames", `${dropped}コマ落ち`);
      S.video.watchdogDropped = quality.droppedVideoFrames;
    }
    const position = currentPosition();
    if (Math.abs(position - S.video.watchdogPosition) > 0.25) {
      S.video.watchdogPosition = position;
      S.video.watchdogStrikes = 0;
      return;
    }
    S.video.watchdogStrikes += 1;
    if (S.video.watchdogStrikes >= WATCHDOG_STRIKES) {
      S.video.watchdogStrikes = 0;
      recoverPlayback(position);
    }
  }, WATCHDOG_INTERVAL_MS);
}

async function recoverPlayback(position) {
  const item = S.video.item;
  if (!item || S.video.recovering) return;
  S.video.recovering = true;
  $("video-spinner").classList.remove("hidden");
  recordGlitch("reload", `${Math.round(position)}秒地点`);
  toast("再生が止まったため読み直します");
  try {
    if (S.video.transcode && !S.video.hls) {
      S.video.offset = position;
      video.src = transcodeStreamUrl(item.id, position);
    } else if (S.video.hls) {
      S.video.offset = position;
      video.src = hlsMasterUrl(item.id, S.video.hlsProfile || "720p", position);
    } else {
      video.load();
      video.currentTime = position;
    }
    applyPlaybackRate();
    await video.play().catch(() => {});
  } finally {
    S.video.recovering = false;
  }
}

function clearVideoErrorRetry() {
  if (S.video.errorRetryTimer) clearTimeout(S.video.errorRetryTimer);
  S.video.errorRetryTimer = null;
  S.video.errorRetryCount = 0;
}

function stopVideo() {
  if (S.video.item) saveVideoProgress();
  if ((S.video.hls || S.video.transcode) && S.video.item) {
    requestTranscodeStop(S.video.item.id);
  }
  stopProgressTimer();
  stopPlaybackWatchdog();
  clearPauseConversionStop();
  clearVideoErrorRetry();
  if (S.video.reloadTimer) { clearTimeout(S.video.reloadTimer); S.video.reloadTimer = null; }
  S.video.pendingSeekSeconds = null;
  S.video.recovering = false;
  video.pause();
  video.removeAttribute("src");
  video.load();
  clearPauseConversionStop();
  S.video.item = null;
  S.video.transcode = false;
  S.video.hls = false;
  S.video.hlsProfile = null;
  S.video.pausedConversionStopped = false;
  if (S.video.orientationLocked) {
    S.video.orientationLocked = false;
    clearVideoOrientationLock();
  }
}

/* 変換配信のシークは「start秒からの作り直し」になるため、連打すると
   ffmpegが多重起動してサーバが飽和する。最後の位置だけを少し遅らせて
   適用する(その間の表示位置は pendingSeekSeconds で先に反映する)。 */
const TRANSCODE_SEEK_DEBOUNCE_MS = 320;

function scheduleTranscodeReload(seconds) {
  S.video.pendingSeekSeconds = seconds;
  updateVideoUi();
  if (S.video.reloadTimer) clearTimeout(S.video.reloadTimer);
  S.video.reloadTimer = setTimeout(() => {
    S.video.reloadTimer = null;
    const item = S.video.item;
    if (!item) return;
    const wasPaused = video.paused;
    S.video.offset = seconds;
    S.video.pendingSeekSeconds = null;
    clearVideoErrorRetry();
    video.src = S.video.hls
      ? hlsMasterUrl(item.id, S.video.hlsProfile || "720p", seconds)
      : transcodeStreamUrl(item.id, seconds);
    applyPlaybackRate();
    $("video-spinner").classList.remove("hidden");
    if (!wasPaused) video.play().catch(() => {});
    startPlaybackWatchdog();
  }, TRANSCODE_SEEK_DEBOUNCE_MS);
}

function videoSeekTo(seconds) {
  const duration = seekableDuration();
  seconds = Math.max(0, Math.min(seconds, duration || Infinity));
  if (!S.video.item) return;
  if (S.video.transcode && !S.video.hls) {
    scheduleTranscodeReload(seconds);
  } else if (S.video.hls) {
    const relative = seconds - S.video.offset;
    const ranges = video.seekable;
    const generatedEnd = ranges && ranges.length ? ranges.end(ranges.length - 1) : 0;
    if (relative >= 0 && relative <= Math.max(0, generatedEnd - 0.5)) {
      // 生成済み範囲内はネイティブシーク
      S.video.pendingSeekSeconds = seconds;
      video.currentTime = relative;
    } else {
      // 未生成範囲へのシークは start 秒からの再生成として読み直す
      // (旧ジョブと未完成キャッシュはサーバ側で破棄される)
      scheduleTranscodeReload(seconds);
    }
  } else {
    S.video.pendingSeekSeconds = seconds;
    video.currentTime = seconds;
  }
}
/* 連続シーク(ホイール・長押し・キー)は保留中の位置を基準に積み上げる。
   変換配信は再読み込みを遅らせるため、実位置だけを見ると加算されない。 */
function videoSeekBy(delta) { videoSeekTo(videoDisplayPosition() + delta); }

function currentOrientationMode() {
  // CSSエンジンと同じ判定を使う。実機の回転直後は innerWidth/innerHeight が
  // 旧向きの値を返すことがあり、それに依存すると誤判定して
  // 「一致している」とみなし回転が外れたままになる
  const query = matchMedia?.("(orientation: landscape)");
  if (query) return query.matches ? "landscape" : "portrait";
  return window.innerWidth >= window.innerHeight ? "landscape" : "portrait";
}

/* 回転フォールバックはインラインstyle(!important)で適用する。
   メディアクエリCSS方式は、全画面(.fullscreen-active)の
   width/height !important に負けて表示が崩れるため使わない。
   寸法はビューポート単位で指定し、回転直後の未確定なピクセル値に
   依存しない(ブラウザが常時再評価する)。 */
const ORIENTATION_ROTATION_PROPS = [
  "position", "top", "left", "right", "bottom",
  "width", "height", "transform", "transform-origin", "z-index",
];

function clearVideoOrientationRotation() {
  const style = $("video-player").style;
  for (const prop of ORIENTATION_ROTATION_PROPS) style.removeProperty(prop);
}

function applyVideoOrientationRotation() {
  const mode = S.video.orientationLockMode;
  if (!S.video.orientationLocked || !mode ||
      currentOrientationMode() === mode) {
    // 物理向きがロック方向と一致している間は回転不要
    clearVideoOrientationRotation();
    return;
  }
  const angle = mode === "landscape" ? 90 : -90;
  const style = $("video-player").style;
  const set = (prop, value) => style.setProperty(prop, value, "important");
  set("position", "fixed");
  set("top", "50%");
  set("left", "50%");
  set("right", "auto");
  set("bottom", "auto");
  // dvh/dvw未対応ブラウザ向けにvh/vwを先に置き、対応環境では上書きする
  set("width", "100vh");
  set("width", "100dvh");
  set("height", "100vw");
  set("height", "100dvw");
  set("transform", `translate(-50%, -50%) rotate(${angle}deg)`);
  set("transform-origin", "center");
  set("z-index", "1000");
}

/* OS側の回転アニメーションと回転補正の適用差で「ぐるん」と見えるのを
   隠すため、向きが実際に切り替わった間だけプレーヤーを非表示にし、
   確定後にフェードインで復帰させる。 */
function clearOrientationMask() {
  const style = $("video-player").style;
  style.removeProperty("opacity");
  style.removeProperty("transition");
  if (S.video.orientationRevealTimer) {
    clearTimeout(S.video.orientationRevealTimer);
    S.video.orientationRevealTimer = null;
  }
}

function refreshVideoOrientationLock() {
  if (!S.video.orientationLocked) return;
  const current = currentOrientationMode();
  if (current === S.video.lastSeenOrientation) {
    // 向きは変わっていない(ツールバー開閉等のresize)。点滅させない
    applyVideoOrientationRotation();
    return;
  }
  S.video.lastSeenOrientation = current;
  const style = $("video-player").style;
  style.removeProperty("transition");
  style.setProperty("opacity", "0", "important");
  applyVideoOrientationRotation();
  // 実機は回転イベント直後に向き/寸法の確定が遅れることがあるため、
  // フレーム後にも適用し、確定を待ってから表示を戻す
  requestAnimationFrame(() => {
    if (S.video.orientationLocked) applyVideoOrientationRotation();
  });
  clearTimeout(S.video.orientationRevealTimer);
  S.video.orientationRevealTimer = setTimeout(() => {
    S.video.orientationRevealTimer = null;
    if (!S.video.orientationLocked) {
      clearOrientationMask();
      return;
    }
    applyVideoOrientationRotation();
    style.setProperty("transition", "opacity .15s ease", "important");
    style.setProperty("opacity", "1", "important");
    setTimeout(() => {
      if (!S.video.orientationRevealTimer) clearOrientationMask();
    }, 220);
  }, 380);
}

async function applyVideoOrientationLock() {
  const mode = S.video.orientationLockMode || currentOrientationMode();
  S.video.orientationLockMode = mode;
  document.body.classList.add("orientation-lock-active");
  document.body.classList.toggle("orientation-lock-landscape", mode === "landscape");
  document.body.classList.toggle("orientation-lock-portrait", mode === "portrait");
  applyVideoOrientationRotation();
  try {
    await screen.orientation?.lock?.(mode);
  } catch (e) {
    // Mobile browsers may reject orientation lock unless already fullscreen. Inline rotation fallback remains active.
  }
}

function clearVideoOrientationLock() {
  S.video.orientationLockMode = null;
  S.video.lastSeenOrientation = null;
  document.body.classList.remove(
    "orientation-lock-active", "orientation-lock-landscape", "orientation-lock-portrait"
  );
  clearOrientationMask();
  clearVideoOrientationRotation();
  try { screen.orientation?.unlock?.(); } catch (e) {}
}

async function toggleVideoOrientationLock() {
  S.video.orientationLocked = !S.video.orientationLocked;
  if (S.video.orientationLocked) {
    S.video.orientationLockMode = currentOrientationMode();
    S.video.lastSeenOrientation = S.video.orientationLockMode;
    await applyVideoOrientationLock();
    toast("画面回転をロックしました");
  } else {
    clearVideoOrientationLock();
    toast("画面回転ロックを解除しました");
  }
  updateVideoUi();
}

async function changeVideoQuality(profile) {
  const item = S.video.item;
  if (!item) return;
  const position = currentPosition();
  const wasPaused = video.paused;
  const speed = video.playbackRate;
  const volume = video.volume;
  const muted = video.muted;
  S.video.quality = profile || "auto";
  stopProgressTimer();
  video.pause();
  await openVideo(item);
  if (S.video.transcode || S.video.hls) {
    videoSeekTo(position);
  } else {
    video.addEventListener("loadedmetadata", () => { video.currentTime = position; }, { once: true });
  }
  video.playbackRate = speed;
  video.volume = volume;
  video.muted = muted;
  if (!wasPaused) await video.play().catch(() => {});
}

function updateVideoUi() {
  const duration = seekableDuration() || totalDuration();
  const position = videoDisplayPosition();
  const label = `${fmtTime(position)} / ${fmtTime(duration)}`;
  $("video-time").textContent = label;
  if (duration > 0 && !videoSeekDragging) {
    $("video-seek").value = Math.round(position / duration * 1000);
  }
  $("btn-play").textContent = video.paused ? "▶" : "⏸";
  $("btn-mute").textContent =
    (video.muted || video.volume === 0) ? "🔇" : "🔊";
  if ($("btn-orientation-lock")) {
    $("btn-orientation-lock").textContent = S.video.orientationLocked ? "🔒" : "🔓";
    $("btn-orientation-lock").classList.toggle("active", S.video.orientationLocked);
  }
}

let videoSeekDragging = false;
function sliderValueFromPointer(input, event) {
  const rect = input.getBoundingClientRect();
  if (!rect.width) return Number(input.value) || 0;
  const ratio = Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width));
  return Math.round(ratio * Number(input.max || 1000));
}
function seekVideoFromSlider(input) {
  const duration = seekableDuration();
  if (duration > 0) videoSeekTo(Number(input.value) / 1000 * duration);
}
function bindVideoSeekSlider(input) {
  if (!input) return;
  input.addEventListener("pointerdown", (e) => {
    videoSeekDragging = true;
    input.value = sliderValueFromPointer(input, e);
    seekVideoFromSlider(input);
    input.setPointerCapture?.(e.pointerId);
    e.preventDefault();
    e.stopPropagation();
  });
  input.addEventListener("pointermove", (e) => {
    if (!videoSeekDragging) return;
    input.value = sliderValueFromPointer(input, e);
    e.preventDefault();
  });
  input.addEventListener("change", () => {
    seekVideoFromSlider(input);
    videoSeekDragging = false;
  });
  input.addEventListener("pointerup", (e) => {
    input.value = sliderValueFromPointer(input, e);
    seekVideoFromSlider(input);
    videoSeekDragging = false;
    e.preventDefault();
  });
}
bindVideoSeekSlider($("video-seek"));

video.addEventListener("timeupdate", updateVideoUi);
video.addEventListener("seeked", () => {
  S.video.pendingSeekSeconds = null;
  updateVideoUi();
});
video.addEventListener("play", () => {
  clearPauseConversionStop();
  updateVideoUi();
  resumePausedConversion();
});
video.addEventListener("pause", () => {
  updateVideoUi();
  saveVideoProgress();
  schedulePauseConversionStop();
});
video.addEventListener("waiting", () => {
  $("video-spinner").classList.remove("hidden");
  if (S.video.item && !video.seeking) {
    const b = video.buffered;
    const ahead = b.length ? (b.end(b.length - 1) - video.currentTime).toFixed(1) : 0;
    recordGlitch("buffer", `先読み残り ${ahead}秒`);
    noteStarvation();
  }
});
video.addEventListener("stalled", () => {
  if (S.video.item) recordGlitch("buffer", "データ供給が止まりました");
});
video.addEventListener("canplay", () => {
  $("video-spinner").classList.add("hidden");
  clearVideoErrorRetry();
});

const VIDEO_ERROR_MAX_RETRIES = 4;
const VIDEO_ERROR_RETRY_MS = 2500;
/* 直接再生が失敗したときの保険。
   ブラウザの canPlayType は "probably" でも実ファイルで失敗することがある
   (MKVの一部など)。その場合だけ変換配信へ自動で切り替える。 */
async function fallbackToTranscode() {
  const item = S.video.item;
  if (!item || S.video.transcode || S.video.hls || S.video.transcodeFallback) return false;
  S.video.transcodeFallback = true;
  const position = currentPosition();
  S.video.transcode = true;
  S.video.offset = position;
  // 映像コーデック自体は再生できている可能性が高いので、まずremuxを試す
  S.video.copyVideo = true;
  S.video.copyAudio = false;
  S.video.maxHeight = null;
  S.video.maxWidth = null;
  $("video-spinner").classList.remove("hidden");
  $("video-badge").textContent = "直接再生できないため変換に切り替えました";
  $("video-badge").classList.remove("hidden");
  toast("直接再生できないため変換配信に切り替えます");
  video.src = transcodeStreamUrl(item.id, position);
  applyPlaybackRate();
  await video.play().catch(() => {});
  startPlaybackWatchdog();
  return true;
}

video.addEventListener("error", () => {
  if (!S.video.item) return;
  if (!S.video.transcode && !S.video.hls) {
    fallbackToTranscode();
    return;
  }
  // 逐次fMP4はRange要求に応えられないため、iOS Safari等では再生できない。
  // 同じ経路で粘らずHLSへ切り替える(4回リトライして失敗する事象への対策)
  if (S.video.transcode && !S.video.hls && videoSupportsNativeHls() &&
      S.video.item && !S.video.hlsFallback) {
    S.video.hlsFallback = true;
    const position = currentPosition();
    const profile = hlsProfileForSource(S.video.info?.height);
    S.video.transcode = false;
    S.video.hls = true;
    S.video.hlsProfile = profile;
    S.video.offset = position;
    clearVideoErrorRetry();
    $("video-spinner").classList.remove("hidden");
    $("video-badge").textContent = `HLS配信 ${profile} (この端末向けに切替)`;
    $("video-badge").classList.remove("hidden");
    video.src = hlsMasterUrl(S.video.item.id, profile, position);
    applyPlaybackRate();
    video.play().catch(() => {});
    startPlaybackWatchdog();
    return;
  }
  // 変換/HLSは初回アクセス時に圧縮動画が未生成でエラーになり得るため、
  // すぐエラー表示せず少し待ってから読み直す
  if ((S.video.hls || S.video.transcode) &&
      S.video.errorRetryCount < VIDEO_ERROR_MAX_RETRIES) {
    S.video.errorRetryCount += 1;
    $("video-spinner").classList.remove("hidden");
    toast(`変換の準備中です… 再試行します (${S.video.errorRetryCount}/${VIDEO_ERROR_MAX_RETRIES})`);
    // HLSはmaster URLを組み直す(生成失敗でキー付きキャッシュが消えても
    // 再要求で生成をやり直せるように)
    const src = S.video.hls
      ? hlsMasterUrl(S.video.item.id, S.video.hlsProfile || "720p", S.video.offset)
      : (S.video.transcode
          ? transcodeStreamUrl(S.video.item.id, S.video.offset)
          : (video.currentSrc || video.src));
    const wasPaused = video.paused;
    clearTimeout(S.video.errorRetryTimer);
    S.video.errorRetryTimer = setTimeout(() => {
      if (!S.video.item) return;
      video.src = src;
      video.load();
      applyPlaybackRate();
      if (!wasPaused) video.play().catch(() => {});
    }, VIDEO_ERROR_RETRY_MS);
    return;
  }
  $("video-spinner").classList.add("hidden");
  $("video-msg").textContent = (S.video.transcode || S.video.hls)
    ? "変換ストリーミングの再生に失敗しました。ffmpegの有無、入力動画形式、またはモバイル互換出力を確認してください。"
    : "再生エラーが発生しました";
  $("video-msg").classList.remove("hidden");
});
video.addEventListener("ended", () => {
  saveVideoProgress();
  if ((S.video.hls || S.video.transcode) && S.video.item) {
    requestTranscodeStop(S.video.item.id);
  }
  playAdjacentVideo(1);
});
video.addEventListener("click", (e) => {
  if (e.target.closest(".video-gesture-zone")) return;
  togglePlay();
});
video.addEventListener("dblclick", () => toggleFullscreen($("video-player")));

/* ================= 回線に合わせた自動調整 =================
   Wi-Fi越しでは帯域も遅延も揺らぐため、原寸配信が続かないことがある。
   供給不足(バッファ切れ)が短時間に続いたら画質を一段下げて安定させる。
   手動で画質を選んでいる場合は尊重して何もしない。 */
const ADAPT_WINDOW_MS = 45000;
const ADAPT_STARVE_LIMIT = 3;
const QUALITY_LADDER = ["original", "1440p", "1080p", "720p", "480p"];

function noteStarvation() {
  if (S.video.qualityIsManual || S.video.adapting) return;
  const now = performance.now();
  S.video.starveTimes = (S.video.starveTimes || [])
    .filter((t) => now - t < ADAPT_WINDOW_MS);
  S.video.starveTimes.push(now);
  if (S.video.starveTimes.length >= ADAPT_STARVE_LIMIT) stepDownQuality();
}

function stepDownQuality() {
  const current = ["auto", "remux", ""].includes(S.video.quality || "auto")
    ? "original" : S.video.quality;
  const index = QUALITY_LADDER.indexOf(current);
  const next = QUALITY_LADDER[Math.min(QUALITY_LADDER.length - 1,
                                       (index < 0 ? 0 : index) + 1)];
  if (!next || next === current) return;
  S.video.starveTimes = [];
  S.video.adapting = true;
  toast(`回線が追いつかないため画質を ${next} に下げました`);
  changeVideoQuality(next).finally(() => { S.video.adapting = false; });
}

function togglePlay() {
  if (!S.video.item) return;
  if (video.paused) video.play().catch(() => {}); else video.pause();
}

function videoItems() {
  return S.items.filter((i) => i.media_type === "video");
}

function playAdjacentVideo(delta) {
  const items = videoItems();
  if (!items.length || !S.video.item) return;
  const index = items.findIndex((i) => i.id === S.video.item.id);
  const next = index + delta;
  if (next < 0 || next >= items.length) {
    toast(delta > 0 ? "最後の動画です" : "最初の動画です");
    return;
  }
  selectItem(items[next].id);
  openVideo(items[next]);
}

$("btn-play").onclick = togglePlay;
$("btn-next-video").onclick = () => playAdjacentVideo(1);
$("btn-prev-video").onclick = () => playAdjacentVideo(-1);
$("btn-video-full").onclick = () => toggleFullscreen($("video-player"));
$("btn-orientation-lock")?.addEventListener("click", toggleVideoOrientationLock);
$("btn-pip").onclick = async () => {
  if (detectUiProfile() === "mobile" || !document.pictureInPictureEnabled || !video.requestPictureInPicture) {
    toast("PiPはこの端末では利用できません", true);
    return;
  }
  try {
    if (document.pictureInPictureElement) await document.exitPictureInPicture();
    else await video.requestPictureInPicture();
  } catch (e) { toast("PiPは利用できません", true); }
};
$("btn-mute").onclick = () => {
  video.muted = !video.muted;
  updateVideoUi();
};
$("video-volume").addEventListener("input", () => {
  video.volume = Number($("video-volume").value) / 100;
  video.muted = false;
  updateVideoUi();
});
$("sel-speed").addEventListener("change", () => {
  applyPlaybackRate();
});
$("sel-video-quality")?.addEventListener("change", () => {
  // 手動で選んだ画質は自動調整で上書きしない
  S.video.qualityIsManual = $("sel-video-quality").value !== "auto";
  changeVideoQuality($("sel-video-quality").value);
});

$("video-stage").addEventListener("wheel", (e) => {
  if (!S.video.item) return;
  e.preventDefault();
  if (S.settings.video_wheel_action === "volume") {
    const value = Math.max(0, Math.min(100,
      Number($("video-volume").value) + (e.deltaY < 0 ? 5 : -5)));
    $("video-volume").value = value;
    video.volume = value / 100;
  } else {
    videoSeekBy(e.deltaY < 0 ? 10 : -10);
  }
}, { passive: false });

/* モバイル: 左右エリアは10秒移動/長押し高速送り専用 */
const videoHoldState = { timer: null, interval: null, active: false, speed: 1, previousRate: 1, direction: 0 };
function clearVideoHold() {
  clearTimeout(videoHoldState.timer);
  clearInterval(videoHoldState.interval);
  videoHoldState.timer = null;
  videoHoldState.interval = null;
  if (videoHoldState.active) {
    video.playbackRate = videoHoldState.previousRate || syncedRate(Number($("sel-speed").value) || 1);
  }
  videoHoldState.active = false;
  videoHoldState.speed = 1;
  videoHoldState.direction = 0;
}
function startVideoHold(direction) {
  if (!S.video.item) return;
  clearVideoHold();
  videoHoldState.direction = direction;
  videoHoldState.previousRate = video.playbackRate || 1;
  videoHoldState.timer = setTimeout(() => {
    videoHoldState.active = true;
    videoHoldState.speed = 1.5;
    if (direction > 0) {
      video.playbackRate = syncedRate(videoHoldState.speed);
      video.play().catch(() => {});
    }
    videoHoldState.interval = setInterval(() => {
      videoHoldState.speed = Math.min(5, videoHoldState.speed + 0.5);
      if (direction > 0) {
        video.playbackRate = syncedRate(videoHoldState.speed);
      } else {
        videoSeekBy(-Math.max(5, 3 * videoHoldState.speed));
      }
    }, 600);
  }, 420);
}
function bindVideoGestureZone(id, direction) {
  const zone = $(id);
  if (!zone) return;
  for (const eventName of ["contextmenu", "selectstart", "dragstart"]) {
    zone.addEventListener(eventName, (e) => {
      e.preventDefault();
      e.stopPropagation();
    });
  }
  zone.addEventListener("touchstart", (e) => {
    if (!S.video.item || detectUiProfile() !== "mobile") return;
    e.preventDefault();
    e.stopPropagation();
  }, { passive: false });
  zone.addEventListener("pointerdown", (e) => {
    if (!S.video.item || detectUiProfile() !== "mobile") return;
    e.preventDefault();
    e.stopPropagation();
    zone.setPointerCapture?.(e.pointerId);
    startVideoHold(direction);
  });
  zone.addEventListener("pointerup", (e) => {
    if (!S.video.item || detectUiProfile() !== "mobile") return;
    e.preventDefault();
    e.stopPropagation();
    const wasHold = videoHoldState.active;
    clearVideoHold();
    if (!wasHold) videoSeekBy(direction * 10);
  });
  zone.addEventListener("pointercancel", (e) => { e.preventDefault(); clearVideoHold(); });
  zone.addEventListener("pointerleave", (e) => { e.preventDefault(); clearVideoHold(); });
}
bindVideoGestureZone("video-zone-left", -1);
bindVideoGestureZone("video-zone-right", 1);

/* ================= fullscreen / UI visibility ================= */
function fullscreenElement() {
  return document.fullscreenElement || document.webkitFullscreenElement || null;
}

function isViewerFullscreen(viewer) {
  return fullscreenElement() === viewer || viewer.classList.contains("fullscreen-active");
}

async function enterViewerFullscreen(viewer) {
  if (viewer.requestFullscreen) {
    await viewer.requestFullscreen().catch(() => {});
  } else if (viewer.webkitRequestFullscreen) {
    viewer.webkitRequestFullscreen();
  }
  if (!fullscreenElement()) {
    viewer.classList.add("fullscreen-active");
    document.body.classList.add("viewer-fullscreen-active");
  }
  viewer.classList.add("show-ui");
  layoutComicSpread();
}

async function exitViewerFullscreen(viewer) {
  if (fullscreenElement()) {
    await document.exitFullscreen?.().catch(() => {});
  }
  viewer.classList.remove("fullscreen-active");
  document.body.classList.remove("viewer-fullscreen-active");
  layoutComicSpread();
}

function toggleFullscreen(el) {
  if (isViewerFullscreen(el)) exitViewerFullscreen(el);
  else enterViewerFullscreen(el);
}

function setupAutoHide(viewerId) {
  const viewer = $(viewerId);
  const isComic = viewerId === "comic-viewer";
  let hideTimer = null;
  const show = () => {
    viewer.classList.add("show-ui");
    clearTimeout(hideTimer);
    hideTimer = setTimeout(() => viewer.classList.remove("show-ui"), 2200);
  };
  const hideNow = () => {
    clearTimeout(hideTimer);
    viewer.classList.remove("show-ui");
  };
  // 漫画はページ送りタップでUIが出ないよう、上部ホットスポットと
  // コントロールバー以外ではUIを表示しない(PC/モバイル共通)
  if (!isComic) viewer.addEventListener("mousemove", show);
  viewer.addEventListener("touchstart", (e) => {
    if (isComic && !e.target.closest("#comic-ui-hotspot, .controls-bar")) {
      return;
    }
    show();
  }, { passive: true });
  viewer.querySelector(".controls-bar").addEventListener("mousemove", (e) => {
    clearTimeout(hideTimer);
    viewer.classList.add("show-ui");
    e.stopPropagation();
  });
  viewer.querySelector(".controls-bar").addEventListener("touchstart", (e) => {
    clearTimeout(hideTimer);
    viewer.classList.add("show-ui");
    e.stopPropagation();
  }, { passive: true });
  const hotspot = viewer.querySelector("#comic-ui-hotspot, #video-ui-hotspot");
  const handleHotspot = (e) => {
    e.preventDefault?.();
    e.stopPropagation();
    if (viewer.classList.contains("show-ui")) hideNow();
    else show();
  };
  hotspot?.addEventListener("click", handleHotspot);
  hotspot?.addEventListener("touchstart", handleHotspot, { passive: false });
  show();
}
setupAutoHide("comic-viewer");
setupAutoHide("video-player");
const comicStageResizeObserver = new ResizeObserver(() => layoutComicSpread());
comicStageResizeObserver.observe($("comic-stage"));
window.addEventListener("resize", () => {
  applyUiProfile();
  layoutComicSpread();
  refreshVideoOrientationLock();
});
// 実機の回転検知はresizeだけでは信頼できない(発火順・寸法確定の遅れ)。
// 向きの切替そのものを監視して回転フォールバックを再評価する。
window.addEventListener("orientationchange", refreshVideoOrientationLock);
matchMedia?.("(orientation: landscape)")
  ?.addEventListener?.("change", refreshVideoOrientationLock);
document.addEventListener("fullscreenchange", () => {
  if (!fullscreenElement()) {
    $("comic-viewer").classList.remove("fullscreen-active");
    $("video-player").classList.remove("fullscreen-active");
    document.body.classList.remove("viewer-fullscreen-active");
  }
  layoutComicSpread();
});

/* ================= keyboard ================= */
document.addEventListener("keydown", (e) => {
  if (e.target.matches("input, select, textarea")) return;
  const comicOpen = !$("comic-viewer").classList.contains("hidden") && S.comic.state;
  const videoOpen = !$("video-player").classList.contains("hidden") && S.video.item;

  if (comicOpen) {
    switch (e.key) {
      case "ArrowLeft": e.preventDefault(); e.shiftKey ? comicShiftByVisualDirection("left") : comicTapLeft(); return;
      case "ArrowRight": e.preventDefault(); e.shiftKey ? comicShiftByVisualDirection("right") : comicTapRight(); return;
      case "PageDown": case " ": e.preventDefault(); comicSpreadForward(); return;
      case "PageUp": e.preventDefault(); comicSpreadBackward(); return;
      case ",": e.preventDefault(); comicShiftBackward(); return;
      case ".": e.preventDefault(); comicShiftForward(); return;
      case "Home": comicCall("goto", { page_index: 0 }).then((s) => s && setComicState(s)); return;
      case "End": comicCall("goto", { page_index: S.comic.state.page_count - 1 }).then((s) => s && setComicState(s)); return;
      case "n": case "N": comicNextEntry(); return;
      case "p": case "P": comicPrevEntry(); return;
      case "f": case "F": toggleFullscreen($("comic-viewer")); return;
    }
  }
  if (videoOpen) {
    switch (e.key) {
      case " ": case "k": case "K": e.preventDefault(); togglePlay(); return;
      case "ArrowLeft": e.preventDefault(); videoSeekBy(e.shiftKey ? -30 : -5); return;
      case "ArrowRight": e.preventDefault(); videoSeekBy(e.shiftKey ? 30 : 5); return;
      case "ArrowUp": e.preventDefault(); adjustVolume(5); return;
      case "ArrowDown": e.preventDefault(); adjustVolume(-5); return;
      case "m": case "M": video.muted = !video.muted; updateVideoUi(); return;
      case "f": case "F": toggleFullscreen($("video-player")); return;
      case "n": case "N": playAdjacentVideo(1); return;
      case "p": case "P": playAdjacentVideo(-1); return;
      case "[": changeSpeed(-1); return;
      case "]": changeSpeed(1); return;
      case "0": $("sel-speed").value = "1"; applyPlaybackRate(); return;
    }
  }
});

function adjustVolume(delta) {
  const value = Math.max(0, Math.min(100, Number($("video-volume").value) + delta));
  $("video-volume").value = value;
  video.volume = value / 100;
  video.muted = false;
  updateVideoUi();
}

function changeSpeed(direction) {
  const options = [...$("sel-speed").options].map((o) => Number(o.value));
  let index = options.indexOf(Number($("sel-speed").value)) + direction;
  index = Math.max(0, Math.min(options.length - 1, index));
  $("sel-speed").value = String(options[index]);
  applyPlaybackRate();
  toast(`再生速度 ${options[index]}x`);
}

/* マウス戻る/進むボタン: 前後のメディアへ

   Windows の Chrome/Firefox はサイドボタンを button=3/4 のマウスイベントとして
   配送するが、Linux(Ubuntu)ではブラウザが履歴移動として処理してしまい
   ページにイベントが届かない。そのため
     (a) マウスイベントが届く環境ではそれを使い(既定動作は抑止)、
     (b) 届かない環境では履歴移動(popstate)を検知して同じ操作に割り当てる
   の二段構えにする。 */
const AUX_MOUSE_DEBOUNCE_MS = 300;
let lastMediaNavAt = 0;

function normalizeAuxDirection(event) {
  if (event.button === 3) return -1;
  if (event.button === 4) return 1;
  if (event.buttons & 8) return -1;
  if (event.buttons & 16) return 1;
  return 0;
}

function mediaNavigationTarget() {
  if (!$("comic-viewer").classList.contains("hidden") && S.comic.state) return "comic";
  if (!$("video-player").classList.contains("hidden") && S.video.item) return "video";
  return null;
}

function navigateMedia(direction, source) {
  const target = mediaNavigationTarget();
  if (!target) return false;
  const now = performance.now();
  if (now - lastMediaNavAt < AUX_MOUSE_DEBOUNCE_MS) return true;
  lastMediaNavAt = now;
  if (S.settings.debug_aux_mouse) {
    console.debug("[FrameDeck] media navigation", { source, direction, target });
  }
  if (target === "comic") navigateComicEntry(direction, source);
  else playAdjacentVideo(direction);
  return true;
}

function handleAuxMouseNavigation(event) {
  if (S.settings.aux_mouse_navigation === false) return;
  const direction = normalizeAuxDirection(event);
  if (!direction) return;
  // ブラウザの履歴移動を止める(戻る/進むで離脱しないように)
  event.preventDefault();
  event.stopPropagation();
  event.stopImmediatePropagation?.();
  navigateMedia(direction, "aux-mouse");
}
window.addEventListener("mousedown", handleAuxMouseNavigation, { capture: true, passive: false });
window.addEventListener("auxclick", handleAuxMouseNavigation, { capture: true, passive: false });
window.addEventListener("mouseup", handleAuxMouseNavigation, { capture: true, passive: false });

/* 履歴を使ったフォールバック(Ubuntu等でサイドボタンがイベント化されない場合)。
   [前] [現在] [次] の3件を積んで常に中央に居座り、戻る/進むが発生したら
   メディア移動として処理してから中央へ戻す。 */
const HISTORY_NAV_CENTER = 1;
let historyNavSuppress = 0;
let historyNavReady = false;

function setupHistoryNavigation() {
  if (!window.history?.pushState) return;
  const base = location.href;
  history.replaceState({ fdNav: 0 }, "", base);
  history.pushState({ fdNav: 1 }, "", base);
  history.pushState({ fdNav: 2 }, "", base);
  historyNavSuppress += 1;
  history.go(-1);          // 中央(fdNav=1)へ
  historyNavReady = true;
}

function restoreHistoryCenter(from) {
  const delta = HISTORY_NAV_CENTER - from;
  if (!delta) return;
  historyNavSuppress += 1;
  history.go(delta);
}

window.addEventListener("popstate", (event) => {
  const position = event.state?.fdNav;
  if (!historyNavReady || typeof position !== "number") return;
  if (historyNavSuppress > 0) {
    historyNavSuppress -= 1;
    return;
  }
  if (position === HISTORY_NAV_CENTER) return;
  const direction = position < HISTORY_NAV_CENTER ? -1 : 1;
  const handled = S.settings.aux_mouse_navigation !== false &&
    navigateMedia(direction, "history");
  // ビューアを開いていない時は普通の履歴移動として扱う(離脱できるように)
  if (handled) restoreHistoryCenter(position);
});

/* ================= settings modal ================= */
function settingRow(grid, label, control, hint) {
  const lab = document.createElement("label");
  lab.textContent = label;
  grid.append(lab, control);
  if (hint) {
    const hintEl = document.createElement("div");
    hintEl.className = "hint";
    hintEl.textContent = hint;
    grid.appendChild(hintEl);
  }
}

function makeSelect(key, options) {
  const select = document.createElement("select");
  select.className = "tb-select";
  for (const [value, label] of options) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    select.appendChild(option);
  }
  select.value = String(S.settings[key]);
  select.onchange = async () => {
    try {
      let value = select.value;
      if (value === "true") value = true;
      else if (value === "false") value = false;
      S.settings = await api("/api/settings", { method: "PUT", json: { [key]: value } });
    } catch (e) { toast(e.message, true); }
  };
  return select;
}

function makeNumberInput(key, { min = 0, max = 999999, step = 1 } = {}) {
  const input = document.createElement("input");
  input.type = "number";
  input.min = String(min);
  input.max = String(max);
  input.step = String(step);
  input.value = String(S.settings[key] ?? "");
  input.className = "tb-select";
  input.onchange = async () => {
    try {
      S.settings = await api("/api/settings", {
        method: "PUT",
        json: { [key]: Number(input.value) },
      });
    } catch (e) { toast(e.message, true); }
  };
  return input;
}

async function openSettings() {
  try { S.settings = await api("/api/settings"); } catch (e) {}
  const wrap = document.createElement("div");
  const grid = document.createElement("div");
  grid.className = "settings-grid";

  settingRow(grid, "起動時の表示位置", makeSelect("library_start_folder", [
    ["last", "前回のフォルダ"], ["root", "ライブラリのルート"],
  ]), "漫画・動画とも、前回開いていたフォルダから再開します。");
  settingRow(grid, "漫画末尾の動作", makeSelect("comic_sequence_end_behavior", [
    ["stop", "停止"], ["wrap", "ループ"], ["prompt", "確認"],
  ]));
  settingRow(grid, "左右クリック", makeSelect("comic_tap_reverse", [
    ["false", "標準"], ["true", "進む/戻るを反転"],
  ]));
  settingRow(grid, "綴じ方向(既定)", makeSelect("reading_direction", [
    ["rtl", "右綴じ"], ["ltr", "左綴じ"],
  ]));
  settingRow(grid, "表示モード(既定)", makeSelect("view_mode", [
    ["spread", "見開き"], ["single", "単ページ"],
  ]));
  settingRow(grid, "表紙を単独表示", makeSelect("cover_as_single_page", [
    ["true", "する"], ["false", "しない"],
  ]));
  settingRow(grid, "前の漫画の開始位置", makeSelect("previous_entry_start", [
    ["first", "先頭ページ"], ["last", "最終見開き"], ["saved", "保存位置"],
  ]));
  settingRow(grid, "親アーカイブの直接画像", makeSelect("include_parent_direct_images", [
    ["true", "読書順に含める"], ["false", "除外する"],
  ]));
  settingRow(grid, "動画上のホイール操作", makeSelect("video_wheel_action", [
    ["seek", "10秒シーク"], ["volume", "音量"],
  ]));
  settingRow(grid, "マウスの戻る/進むボタン", makeSelect("aux_mouse_navigation", [
    ["true", "前後のファイルへ移動"], ["false", "使わない"],
  ]), "Ubuntu等ではブラウザの履歴移動として届くため、その場合も同じ動作にします。");
  settingRow(grid, "続きから再生", makeSelect("resume_playback", [
    ["true", "有効"], ["false", "無効"],
  ]));

  const comicHead = document.createElement("h3");
  comicHead.textContent = "漫画配信";
  comicHead.style.gridColumn = "1 / -1";
  comicHead.style.margin = "14px 0 0";
  grid.appendChild(comicHead);
  settingRow(grid, "軽量画像配信", makeSelect("comic_delivery_mode", [
    ["original", "無効"], ["auto", "自動"], ["compressed", "常に有効"],
  ]));
  settingRow(grid, "画像形式", makeSelect("comic_output_format", [
    ["auto", "自動"], ["jpeg", "JPEG"], ["webp", "WebP"],
    ["avif", "AVIF"], ["png", "PNG"], ["original", "原本"],
  ]));
  settingRow(grid, "自動トリミング", makeSelect("comic_auto_crop", [
    ["true", "有効"], ["false", "無効"],
  ]));
  settingRow(grid, "縮小後シャープ化", makeSelect("comic_variant_sharpen", [
    ["true", "有効"], ["false", "無効"],
  ]), "軽量配信時に縮小でなまった線を復元してから圧縮します。");
  settingRow(grid, "漫画キャッシュMB", makeNumberInput("comic_cache_max_mb", { min: 0, max: 1000000, step: 50 }),
    "変換画像・ページ・サムネイルの合計上限。超過分は古い順に自動削除(0で無制限)。");
  settingRow(grid, "白枠トリミング", makeSelect("comic_crop_white", [
    ["true", "有効"], ["false", "無効"],
  ]));
  settingRow(grid, "灰色枠トリミング", makeSelect("comic_crop_gray", [
    ["true", "有効"], ["false", "無効"],
  ]));
  settingRow(grid, "黒枠トリミング", makeSelect("comic_crop_black", [
    ["true", "有効"], ["false", "無効"],
  ]));
  settingRow(grid, "見開き自動判定", makeSelect("comic_spread_detection", [
    ["true", "有効"], ["false", "無効"],
  ]));
  settingRow(grid, "PC 表示モード", makeSelect("comic_desktop_view_mode", [
    ["spread", "見開き"], ["single", "単ページ"],
  ]));
  settingRow(grid, "PC 配信品質", makeSelect("comic_desktop_delivery_profile", [
    ["high", "高画質"], ["balanced", "標準"], ["mobile", "軽量"],
    ["data_saver", "データ節約"], ["original", "原本"],
  ]));
  settingRow(grid, "モバイル表示モード", makeSelect("comic_mobile_view_mode", [
    ["single", "単ページ"], ["spread", "見開き"],
  ]));
  settingRow(grid, "モバイル配信品質", makeSelect("comic_mobile_delivery_profile", [
    ["mobile", "軽量"], ["balanced", "標準"], ["data_saver", "データ節約"],
    ["high", "高画質"], ["original", "原本"],
  ]));
  settingRow(grid, "端末側補正", makeSelect("comic_client_enhancement", [
    ["auto", "自動"], ["off", "無効"], ["sharpen", "シャープ"],
    ["contrast", "コントラスト"], ["super_resolution", "超解像(実験)"],
  ]));

  const videoHead = document.createElement("h3");
  videoHead.textContent = "動画配信";
  videoHead.style.gridColumn = "1 / -1";
  videoHead.style.margin = "14px 0 0";
  grid.appendChild(videoHead);
  settingRow(grid, "動画軽量配信", makeSelect("video_stream_mode", [
    ["original", "無効"], ["auto", "自動"], ["transcode", "常に有効"],
  ]));
  const videoQualityOptions = [
    ["auto", "自動 (回線で判定)"], ["original", "原寸"], ["2160p", "4K"], ["1440p", "1440p"],
    ["1080p", "1080p"], ["720p", "720p"], ["480p", "480p"], ["360p", "360p"],
  ];
  settingRow(grid, "最大解像度", makeSelect("video_max_resolution", videoQualityOptions),
    "4K変換は通信量・CPU/GPU負荷・キャッシュ容量が大きくなります。");
  settingRow(grid, "PC 動画品質", makeSelect("video_profile_desktop", videoQualityOptions),
    "自動: Wi-Fi/有線(同一LAN)なら原寸、モバイル回線なら下の上限で配信します。");
  settingRow(grid, "モバイル動画品質", makeSelect("video_profile_mobile", videoQualityOptions),
    "既定は1080p。iOSでは原寸の直接再生が安定しないため、回線によらず" +
    "セグメント配信(HLS)で届けます。原寸にしたい場合はここで変更できます。");
  settingRow(grid, "モバイル回線の上限",
    makeSelect("video_cellular_max_resolution", videoQualityOptions.filter(([v]) => v !== "auto")),
    "モバイル回線と判定された時だけ適用される上限です。");
  settingRow(grid, "表示同期", makeSelect("video_display_sync", [
    ["auto", "自動 (±1.2%まで)"], ["strong", "強め (±5%まで)"], ["off", "無効"],
  ]), buildDisplaySyncHint());
  const glitchRow = document.createElement("div");
  glitchRow.className = "hint";
  glitchRow.style.gridColumn = "1 / -1";
  glitchRow.textContent = glitchSummary();
  grid.appendChild(glitchRow);
  settingRow(grid, "なめらか変換", makeSelect("video_smooth_motion", [
    ["off", "しない"], ["auto", "必要なときだけ (実験)"],
  ]), "速度補正で均等にできない組み合わせ(60Hzで23.976fpsなど)のとき、" +
     "画面に合わせた中間フレームをサーバ側で生成します。コマ表示のムラは" +
     "消えますが、映像を再エンコードするため速い動きは少し柔らかくなります。" +
     "対象は1080p以下・30fps以下の素材のみ。");
  settingRow(grid, "動画コーデック", makeSelect("video_codec", [
    ["h264", "H.264"], ["hevc", "HEVC"], ["vp9", "VP9"],
    ["av1", "AV1"], ["copy", "コピー可能ならコピー"],
  ]));
  settingRow(grid, "映像ビットレートkbps", makeNumberInput("video_bitrate_kbps", { min: 0, max: 100000, step: 50 }));
  settingRow(grid, "音声ビットレートkbps", makeNumberInput("video_audio_bitrate_kbps", { min: 0, max: 2000, step: 8 }));
  settingRow(grid, "HLSセグメント秒", makeNumberInput("video_segment_duration", { min: 1, max: 30, step: 1 }));
  settingRow(grid, "同時HLS変換数", makeNumberInput("video_hls_max_concurrent", { min: 1, max: 16, step: 1 }),
    "サーバ全体で同時に実行するHLS変換の上限。待機中も各タブの最新要求だけを残します。");
  settingRow(grid, "動画キャッシュMB", makeNumberInput("video_variant_cache_mb", { min: 0, max: 10000000, step: 50 }),
    "HLS変換キャッシュの合計上限。超過分は古い順に自動削除(0で無制限)。");

  settingRow(grid, "削除方法", makeSelect("delete_to_trash", [
    ["true", "ゴミ箱へ移動"], ["false", "完全削除"],
  ]));
  wrap.appendChild(grid);

  /* ライブラリルート管理 */
  function buildLibraryRootSection(kind, title) {
    const section = document.createElement("section");
    section.className = "library-root-section";
    const heading = document.createElement("h3");
    heading.textContent = title;
    heading.style.margin = "16px 0 8px";
    section.appendChild(heading);

    const rootList = document.createElement("ul");
    rootList.className = "choice-list";
    const roots = S.roots.filter((root) => root.kind === kind);
    if (!roots.length) {
      const empty = document.createElement("li");
      empty.textContent = "登録済みルートはありません";
      rootList.appendChild(empty);
    }
    for (const root of roots) {
      const li = document.createElement("li");
      li.textContent = `📁 ${root.display_name}`;
      const remove = document.createElement("button");
      remove.className = "modal-btn danger";
      remove.textContent = "解除";
      const rename = document.createElement("button");
      rename.className = "modal-btn";
      rename.textContent = "名称変更";
      rename.style.marginLeft = "auto";
      rename.onclick = async (e) => {
        e.stopPropagation();
        const displayName = prompt("表示名", root.display_name);
        if (displayName === null) return;
        try {
          await api(`/api/library/roots/${root.id}`, {
            method: "PATCH",
            json: { display_name: displayName || null },
          });
          await loadRoots();
          closeModal();
          renderRootSelectors();
          toast("表示名を変更しました");
        } catch (err) { toast(err.message, true); }
      };
      remove.onclick = async (e) => {
        e.stopPropagation();
        const ok = confirm(
          `「${root.display_name}」の登録を解除します。\n実際のファイルは削除されません。`
        );
        if (!ok) return;
        try {
          await api(`/api/library/roots/${root.id}`, { method: "DELETE" });
          if (S.activeRootIds[kind] === root.id) saveActiveRootId(kind, null);
          await loadRoots();
          closeModal();
          if (S.mode === kind) await switchToActiveRoot();
          toast("ルートを解除しました");
        } catch (err) { toast(err.message, true); }
      };
      li.append(rename, remove);
      li.onclick = (e) => e.stopPropagation();
      rootList.appendChild(li);
    }
    section.appendChild(rootList);

    const form = document.createElement("div");
    form.className = "library-root-form";
    form.style.display = "grid";
    form.style.gridTemplateColumns = "1fr auto";
    form.style.gap = "8px";
    form.style.marginTop = "8px";
    const nameInput = document.createElement("input");
    nameInput.type = "text";
    nameInput.placeholder = "表示名 (省略可)";
    const pathInput = document.createElement("input");
    pathInput.type = "text";
    pathInput.placeholder = "サーバ上のフォルダパス";
    for (const input of [nameInput, pathInput]) {
      input.style.background = "var(--surface)";
      input.style.border = "1px solid var(--border)";
      input.style.borderRadius = "8px";
      input.style.color = "var(--text)";
      input.style.padding = "8px";
      input.style.minWidth = "0";
    }
    const addBtn = document.createElement("button");
    addBtn.className = "modal-btn primary";
    addBtn.textContent = kind === "comic" ? "漫画フォルダを追加" : "動画フォルダを追加";
    addBtn.style.gridRow = "1 / span 2";
    addBtn.style.gridColumn = "2";
    addBtn.onclick = async () => {
      try {
        const created = await api("/api/library/roots", {
          json: {
            path: pathInput.value,
            kind,
            display_name: nameInput.value || null,
          },
        });
        await loadRoots();
        if (S.mode === kind) {
          await switchLibraryRoot(created.id, { closeDrawer: false });
        }
        closeModal();
        toast("ルートを追加しました");
      } catch (e) { toast(e.message, true); }
    };
    form.append(nameInput, addBtn, pathInput);
    section.appendChild(form);
    return section;
  }

  wrap.appendChild(buildLibraryRootSection("comic", "漫画ライブラリ"));
  wrap.appendChild(buildLibraryRootSection("video", "動画ライブラリ"));

  const note = document.createElement("div");
  note.className = "hint";
  note.style.marginTop = "12px";
  note.style.color = "var(--text-dim)";
  note.style.fontSize = "11px";
  note.textContent =
    "FrameDeckはローカル/LAN利用を想定しています。インターネットへ直接公開しないでください。";
  wrap.appendChild(note);

  showModal("設定", wrap, [{ label: "閉じる", onClick: closeModal }]);
}
$("btn-settings").onclick = openSettings;

/* ================= top bar wiring ================= */
async function setMode(mode) {
  if (S.mode === mode) return;
  S.mode = mode;
  resetNavigationState();
  clearCurrentViewer();
  $("placeholder-icon").textContent = mode === "comic" ? "📖" : "▶";
  $("placeholder-text").textContent =
    mode === "comic" ? "漫画を選択してください" : "動画を選択してください";
  updateModeButtons();
  await switchToActiveRoot();
}
function updateModeButtons() {
  $("btn-mode-comic").classList.toggle("active", S.mode === "comic");
  $("btn-mode-video").classList.toggle("active", S.mode === "video");
  $("btn-mobile-comic")?.classList.toggle("active", S.mode === "comic");
  $("btn-mobile-video")?.classList.toggle("active", S.mode === "video");
}
$("btn-mode-comic").onclick = () => setMode("comic");
$("btn-mode-video").onclick = () => setMode("video");
$("btn-mobile-comic").onclick = () => setMode("comic");
$("btn-mobile-video").onclick = () => setMode("video");

function refreshCurrentFolder() {
  if (S.folderId) loadFolder(S.folderId, { remember: false });
}
function goFolderBack() {
  if (S.histIndex > 0) {
    S.histIndex--;
    loadFolder(S.history[S.histIndex], { remember: false });
  }
}
function goFolderForward() {
  if (S.histIndex < S.history.length - 1) {
    S.histIndex++;
    loadFolder(S.history[S.histIndex], { remember: false });
  }
}
function goFolderUp() {
  if (S.folderInfo && S.folderInfo.parent_id) loadFolder(S.folderInfo.parent_id);
}
$("btn-refresh").onclick = refreshCurrentFolder;
$("btn-folder-back").onclick = goFolderBack;
$("btn-folder-forward").onclick = goFolderForward;
$("btn-folder-up").onclick = goFolderUp;
$("btn-mobile-refresh").onclick = refreshCurrentFolder;
$("btn-mobile-back").onclick = goFolderBack;
$("btn-mobile-forward").onclick = goFolderForward;
$("btn-mobile-up").onclick = goFolderUp;
$("sel-sort").onchange = () => { syncMobileSelectValue("sel-sort", "sel-sort-mobile"); refreshCurrentFolder(); };
$("sel-filter").onchange = () => { syncMobileSelectValue("sel-filter", "sel-filter-mobile"); refreshCurrentFolder(); };
$("sel-sort-mobile").onchange = () => { syncMobileSelectValue("sel-sort-mobile", "sel-sort"); refreshCurrentFolder(); };
$("sel-filter-mobile").onchange = () => { syncMobileSelectValue("sel-filter-mobile", "sel-filter"); refreshCurrentFolder(); };
let librarySearchTimer = null;
function bindLibrarySearch(inputId, peerId) {
  const input = $(inputId);
  if (!input) return;
  input.addEventListener("input", () => {
    if ($(peerId)) $(peerId).value = input.value;
    clearTimeout(librarySearchTimer);
    librarySearchTimer = setTimeout(refreshCurrentFolder, 220);
  });
}
bindLibrarySearch("library-search", "library-search-mobile");
bindLibrarySearch("library-search-mobile", "library-search");
$("sel-library-root").onchange = (e) => switchLibraryRoot(e.target.value);
$("sel-library-root-mobile").onchange = (e) => switchLibraryRoot(e.target.value);

/* 削除ボタン: 複数選択中なら一括削除、それ以外は選択中の1件 */
function requestDeleteDispatch() {
  if (S.selectMode && S.checked.size) requestBulkDelete();
  else requestDelete();
}
$("btn-delete").onclick = requestDeleteDispatch;
$("btn-delete-mobile").onclick = requestDeleteDispatch;

function syncMobileSelectValue(fromId, toId) {
  const from = $(fromId);
  const to = $(toId);
  if (from && to) to.value = from.value;
}
function copySelectOptions(fromId, toId) {
  const from = $(fromId);
  const to = $(toId);
  if (!from || !to) return;
  to.innerHTML = from.innerHTML;
  to.value = from.value;
}
function setupMobileSelects() {
  copySelectOptions("sel-sort", "sel-sort-mobile");
  copySelectOptions("sel-filter", "sel-filter-mobile");
}

/* ================= ライブラリペインの幅 / 最小化 (PC) ================= */
const LIBRARY_WIDTH_KEY = "framedeck.libraryWidth";
const LIBRARY_COLLAPSED_KEY = "framedeck.libraryCollapsed";
const LIBRARY_DEFAULT_WIDTH = 340;
const LIBRARY_MIN_WIDTH = 200;

function libraryMaxWidth() {
  return Math.max(LIBRARY_MIN_WIDTH, Math.round(window.innerWidth * 0.8));
}

function applyLibraryWidth(width, { persist = true } = {}) {
  const value = Math.round(
    Math.min(libraryMaxWidth(), Math.max(LIBRARY_MIN_WIDTH, width || LIBRARY_DEFAULT_WIDTH))
  );
  document.documentElement.style.setProperty("--library-width", `${value}px`);
  if (persist) localStorage.setItem(LIBRARY_WIDTH_KEY, String(value));
  layoutComicSpread();
  return value;
}

function setLibraryCollapsed(collapsed, { persist = true } = {}) {
  document.body.classList.toggle("library-collapsed", collapsed);
  $("library-expand")?.classList.toggle("hidden", !collapsed);
  $("btn-library-collapse")?.setAttribute("aria-expanded", String(!collapsed));
  if (persist) localStorage.setItem(LIBRARY_COLLAPSED_KEY, collapsed ? "1" : "0");
  layoutComicSpread();
}

function toggleLibraryCollapsed() {
  setLibraryCollapsed(!document.body.classList.contains("library-collapsed"));
}

function setupLibraryResizer() {
  applyLibraryWidth(Number(localStorage.getItem(LIBRARY_WIDTH_KEY)) || LIBRARY_DEFAULT_WIDTH,
                    { persist: false });
  setLibraryCollapsed(localStorage.getItem(LIBRARY_COLLAPSED_KEY) === "1",
                      { persist: false });

  const resizer = $("library-resizer");
  if (!resizer) return;
  let drag = null;
  resizer.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    drag = { x: e.clientX, width: $("library-pane").getBoundingClientRect().width };
    resizer.setPointerCapture?.(e.pointerId);
    document.body.classList.add("library-resizing");
  });
  resizer.addEventListener("pointermove", (e) => {
    if (!drag) return;
    e.preventDefault();
    applyLibraryWidth(drag.width + (e.clientX - drag.x), { persist: false });
  });
  const endDrag = (e) => {
    if (!drag) return;
    applyLibraryWidth(drag.width + (e.clientX - drag.x));
    drag = null;
    document.body.classList.remove("library-resizing");
  };
  resizer.addEventListener("pointerup", endDrag);
  resizer.addEventListener("pointercancel", () => {
    drag = null;
    document.body.classList.remove("library-resizing");
  });
  resizer.addEventListener("dblclick", () => {
    applyLibraryWidth(LIBRARY_DEFAULT_WIDTH);
    toast("一覧の幅を既定に戻しました");
  });
  resizer.addEventListener("keydown", (e) => {
    const step = e.shiftKey ? 60 : 20;
    const current = $("library-pane").getBoundingClientRect().width;
    if (e.key === "ArrowLeft") { e.preventDefault(); applyLibraryWidth(current - step); }
    else if (e.key === "ArrowRight") { e.preventDefault(); applyLibraryWidth(current + step); }
  });
}
$("btn-library-collapse").onclick = toggleLibraryCollapsed;
$("library-expand").onclick = toggleLibraryCollapsed;
$("btn-library-reset").onclick = () => {
  applyLibraryWidth(LIBRARY_DEFAULT_WIDTH);
  setLibraryCollapsed(false);
  toast("一覧の表示を既定に戻しました");
};

/* mobile drawer */
function openMobileDrawer() {
  $("library-pane").classList.add("open");
  $("library-backdrop").classList.remove("hidden");
  document.body.classList.add("drawer-open");
}
function closeMobileDrawer() {
  $("library-pane").classList.remove("open");
  $("library-backdrop")?.classList.add("hidden");
  document.body.classList.remove("drawer-open");
}
$("btn-list-toggle").onclick = openMobileDrawer;
$("btn-list-close").onclick = closeMobileDrawer;
$("library-backdrop").onclick = closeMobileDrawer;
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && $("library-pane").classList.contains("open")) {
    closeMobileDrawer();
    return;
  }
  if (e.key === "Escape" && S.selectMode &&
      $("modal-backdrop").classList.contains("hidden")) {
    setSelectMode(false);
    return;
  }
  if ((e.ctrlKey || e.metaKey) && (e.key === "b" || e.key === "B")) {
    e.preventDefault();
    toggleLibraryCollapsed();
  }
});

/* ================= websocket ================= */
function connectWs() {
  try {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws/events`);
    ws.onmessage = (event) => {
      try {
        const message = JSON.parse(event.data);
        if (message.event === "settings_changed") {
          api("/api/settings").then((s) => { S.settings = s; });
        }
      } catch (e) {}
    };
    ws.onclose = () => setTimeout(connectWs, 5000);
  } catch (e) {}
}

/* ================= save on unload ================= */
window.addEventListener("pagehide", () => {
  if (S.video.item) saveVideoProgress();
  if ((S.video.hls || S.video.transcode) && S.video.item) {
    requestTranscodeStop(S.video.item.id);
  }
});

/* ================= init ================= */
async function init() {
  applyUiProfile();
  buildStarBar();
  updateModeButtons();
  restoreListPreferences();
  setupLibraryResizer();
  setupHistoryNavigation();
  try {
    S.settings = await api("/api/settings");
    await loadRoots();
  } catch (e) {
    toast(`初期化に失敗: ${e.message}`, true);
    return;
  }
  setupMobileSelects();
  await switchToActiveRoot({ announceResume: true });
  connectWs();
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  }
}
init();
