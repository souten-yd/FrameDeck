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
  },
};

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
  return S.settings[key] || S.settings.video_max_resolution || (S.uiProfile === "mobile" ? "720p" : "1080p");
}

function videoResolutionHeight(profile) {
  const map = { "2160p": 2160, "1440p": 1440, "1080p": 1080, "720p": 720, "480p": 480, "360p": 360 };
  return map[profile] || 1080;
}
function videoResolutionWidth(profile) {
  const map = { "2160p": 3840, "1440p": 2560, "1080p": 1920, "720p": 1280, "480p": 854, "360p": 640 };
  return map[profile] || 1920;
}

function hlsProfileName(profile) {
  const allowed = new Set(["2160p", "1440p", "1080p", "720p", "480p", "360p"]);
  if (allowed.has(profile)) return profile;
  return S.uiProfile === "mobile" ? "720p" : "1080p";
}

function hlsMasterUrl(itemId, profile, startSeconds) {
  const start = Math.max(0, Number(startSeconds) || 0);
  return `/api/videos/${itemId}/hls/master.m3u8?profile=${encodeURIComponent(profile)}&start=${start.toFixed(2)}`;
}

function shouldUseNativeHls(playbackProfile) {
  return S.uiProfile === "mobile" && playbackProfile?.transcode && videoSupportsNativeHls();
}

function clientMediaHints() {
  const connection = navigator.connection || navigator.mozConnection || navigator.webkitConnection;
  return {
    effectiveType: connection?.effectiveType || null,
    downlink: connection?.downlink || null,
    saveData: Boolean(connection?.saveData),
    viewportWidth: window.innerWidth,
    viewportHeight: window.innerHeight,
    devicePixelRatio: window.devicePixelRatio || 1,
    uiProfile: S.uiProfile,
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

async function loadFolder(folderId, { remember = true } = {}) {
  if (!folderId) return;
  const sort = $("sel-sort").value;
  const filter = $("sel-filter").value;
  try {
    const data = await api(
      `/api/library/items?folder_id=${folderId}&mode=${S.mode}` +
      `&sort=${sort}&filter=${filter}`
    );
    S.folderId = folderId;
    S.folderInfo = data.folder;
    S.items = data.items;
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
  const text = info
    ? (info.relative_path ? `${info.display_name} - ${info.relative_path}` : info.display_name)
    : "";
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

    li.append(icon, name, stars);
    li.onclick = () => activateItem(item);
    list.appendChild(li);
  }
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

async function switchToActiveRoot() {
  const root = activeRootForMode();
  renderRootSelectors();
  if (root) await switchLibraryRoot(root.id, { closeDrawer: false });
  else showMissingLibraryRoot(S.mode);
}

/* ================= star rating ================= */
function buildStarBar() {
  const bar = $("star-bar");
  bar.innerHTML = "";
  for (let n = 1; n <= 5; n++) {
    const star = document.createElement("span");
    star.className = "star";
    star.textContent = "★";
    star.dataset.n = n;
    star.onclick = () => applyRating(n);
    bar.appendChild(star);
  }
  const clear = document.createElement("span");
  clear.className = "star-clear";
  clear.textContent = "✕";
  clear.title = "評価を解除";
  clear.onclick = () => applyRating(null);
  bar.appendChild(clear);
}

function updateStarBar() {
  const item = S.items.find((i) => i.id === S.selectedId);
  const rating = item ? item.rating || 0 : 0;
  for (const star of $("star-bar").querySelectorAll(".star")) {
    star.classList.toggle("on", Number(star.dataset.n) <= rating);
  }
}

async function applyRating(rating) {
  if (!S.selectedId) { toast("項目を選択してください"); return; }
  try {
    await api(`/api/library/items/${S.selectedId}/rating`, { json: { rating } });
    await loadFolder(S.folderId, { remember: false });
    updateStarBar();
  } catch (e) {
    toast(`評価の設定に失敗: ${e.message}`, true);
  }
}

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
  const splitActive = (state.visible_page_sides || []).some((s) => s !== "full");
  const last = state.visible_pages[state.visible_pages.length - 1];
  if (splitActive) {
    // 分割表示中: 現ページと次ページの両面を先読みする
    for (const side of ["right", "left"]) {
      new Image().src = comicPageUrl(last, side);
      if (last + 1 < state.page_count) new Image().src = comicPageUrl(last + 1, side);
    }
  }
  for (let i = 1; i <= 4; i++) {
    const idx = last + i;
    if (idx < state.page_count) new Image().src = comicPageUrl(idx);
  }
  const first = state.visible_pages[0];
  for (let i = 1; i <= 2; i++) {
    const idx = first - i;
    if (idx >= 0) new Image().src = comicPageUrl(idx);
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
  S.comic.state.reading_direction === "rtl" ? comicSpreadForward() : comicSpreadBackward();
}
function comicTapRight() {
  if (!S.comic.state) return;
  S.comic.state.reading_direction === "rtl" ? comicSpreadBackward() : comicSpreadForward();
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

async function openVideo(item) {
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
    $("video-spinner").classList.add("hidden");
    $("video-msg").textContent = `動画情報を取得できません\n${e.message}`;
    $("video-msg").classList.remove("hidden");
    return;
  }
  S.video.item = item;
  S.video.info = detail.info;
  S.video.duration = detail.info.duration_seconds || 0;
  S.video.offset = 0;
  S.video.pendingSeekSeconds = null;
  $("video-title").textContent = item.display_name;

  const resume = detail.resume_position || 0;
  if ($("sel-video-quality")) $("sel-video-quality").value = S.video.quality || "auto";
  let playbackProfile = null;
  try {
    const hints = clientMediaHints();
    if (S.video.quality && S.video.quality !== "auto") hints.requestedProfile = S.video.quality;
    const decision = await api(`/api/videos/${item.id}/playback-profile`, {
      json: hints,
    });
    playbackProfile = decision.profile;
  } catch (e) {
    playbackProfile = null;
  }
  const wantsTranscode = Boolean(playbackProfile?.transcode);
  if (!wantsTranscode && detail.info.direct_play) {
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
    const fallbackQuality = configuredVideoQuality();
    const maxHeight = playbackProfile?.height || videoResolutionHeight(fallbackQuality);
    const maxWidth = playbackProfile?.width || videoResolutionWidth(playbackProfile?.name || fallbackQuality);
    if (shouldUseNativeHls(playbackProfile)) {
      const profile = hlsProfileName(playbackProfile?.name || fallbackQuality);
      S.video.transcode = false;
      S.video.hls = true;
      S.video.hlsProfile = profile;
      // HLSは start 秒からの再生成で途中再生する(fMP4変換と同じoffset方式)
      S.video.offset = resume;
      video.src = hlsMasterUrl(item.id, profile, resume);
      if (resume > 0) toast(`続きから再生: ${fmtTime(resume)}`);
      $("video-badge").textContent = `HLS軽量配信 ${profile}`;
    } else {
      S.video.transcode = true;
      S.video.hls = false;
      S.video.offset = resume;
      video.src = `/api/videos/${item.id}/stream-transcode?start=${resume}&max_height=${maxHeight}&max_width=${maxWidth}`;
      $("video-badge").textContent = playbackProfile
        ? `逐次軽量配信 ${playbackProfile.name} (${maxHeight}p)`
        : `変換ストリーミング (${detail.info.video_codec || detail.info.container})`;
      if (resume > 0) toast(`続きから再生: ${fmtTime(resume)}`);
    }
    $("video-badge").classList.remove("hidden");
  } else if (detail.info.direct_play) {
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
      `この形式はブラウザで再生できません\n${detail.info.direct_play_reason}\n` +
      "ffmpegをインストールすると変換再生が可能になります";
    $("video-msg").classList.remove("hidden");
    return;
  }
  video.playbackRate = Number($("sel-speed").value);
  video.volume = Number($("video-volume").value) / 100;
  try { await video.play(); } catch (e) { /* 自動再生ブロックは無視 */ }
  startProgressTimer();
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

function requestHlsStop(itemId) {
  // 生成中の変換ジョブを止めてキャッシュが溜まらないようにする
  const url = `/api/videos/${itemId}/hls/stop`;
  if (!navigator.sendBeacon?.(url, "")) {
    api(url, { method: "POST" }).catch(() => {});
  }
}

function clearVideoErrorRetry() {
  if (S.video.errorRetryTimer) clearTimeout(S.video.errorRetryTimer);
  S.video.errorRetryTimer = null;
  S.video.errorRetryCount = 0;
}

function stopVideo() {
  if (S.video.item) saveVideoProgress();
  if (S.video.hls && S.video.item) requestHlsStop(S.video.item.id);
  stopProgressTimer();
  clearVideoErrorRetry();
  S.video.pendingSeekSeconds = null;
  video.pause();
  video.removeAttribute("src");
  video.load();
  S.video.item = null;
  S.video.transcode = false;
  S.video.hls = false;
  S.video.hlsProfile = null;
  if (S.video.orientationLocked) {
    S.video.orientationLocked = false;
    clearVideoOrientationLock();
  }
}

function videoSeekTo(seconds) {
  const duration = seekableDuration();
  seconds = Math.max(0, Math.min(seconds, duration || Infinity));
  S.video.pendingSeekSeconds = seconds;
  if (S.video.transcode && !S.video.hls) {
    const item = S.video.item;
    if (!item) return;
    S.video.offset = seconds;
    S.video.pendingSeekSeconds = null;
    const wasPaused = video.paused;
    const quality = configuredVideoQuality();
    video.src = `/api/videos/${item.id}/stream-transcode?start=${seconds.toFixed(2)}&max_height=${videoResolutionHeight(quality)}&max_width=${videoResolutionWidth(quality)}`;
    video.playbackRate = Number($("sel-speed").value);
    if (!wasPaused) video.play().catch(() => {});
  } else if (S.video.hls) {
    const item = S.video.item;
    if (!item) return;
    const relative = seconds - S.video.offset;
    const ranges = video.seekable;
    const generatedEnd = ranges && ranges.length ? ranges.end(ranges.length - 1) : 0;
    if (relative >= 0 && relative <= Math.max(0, generatedEnd - 0.5)) {
      // 生成済み範囲内はネイティブシーク
      video.currentTime = relative;
    } else {
      // 未生成範囲へのシークは start 秒からの再生成として読み直す
      // (旧ジョブと未完成キャッシュはサーバ側で破棄される)
      S.video.offset = seconds;
      S.video.pendingSeekSeconds = null;
      const wasPaused = video.paused;
      video.src = hlsMasterUrl(item.id, S.video.hlsProfile || "720p", seconds);
      video.playbackRate = Number($("sel-speed").value);
      if (!wasPaused) video.play().catch(() => {});
    }
  } else {
    video.currentTime = seconds;
  }
}
function videoSeekBy(delta) { videoSeekTo(currentPosition() + delta); }

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

function refreshVideoOrientationLock() {
  if (!S.video.orientationLocked) return;
  applyVideoOrientationRotation();
  // 実機は回転イベント直後に向き/寸法の確定が遅れることがあるため、
  // フレーム後と少し後にもう一度適用して自己修復する
  requestAnimationFrame(() => {
    if (S.video.orientationLocked) applyVideoOrientationRotation();
  });
  setTimeout(() => {
    if (S.video.orientationLocked) applyVideoOrientationRotation();
  }, 350);
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
  document.body.classList.remove(
    "orientation-lock-active", "orientation-lock-landscape", "orientation-lock-portrait"
  );
  clearVideoOrientationRotation();
  try { screen.orientation?.unlock?.(); } catch (e) {}
}

async function toggleVideoOrientationLock() {
  S.video.orientationLocked = !S.video.orientationLocked;
  if (S.video.orientationLocked) {
    S.video.orientationLockMode = currentOrientationMode();
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
video.addEventListener("play", updateVideoUi);
video.addEventListener("pause", () => { updateVideoUi(); saveVideoProgress(); });
video.addEventListener("waiting", () => $("video-spinner").classList.remove("hidden"));
video.addEventListener("canplay", () => {
  $("video-spinner").classList.add("hidden");
  clearVideoErrorRetry();
});

const VIDEO_ERROR_MAX_RETRIES = 4;
const VIDEO_ERROR_RETRY_MS = 2500;
video.addEventListener("error", () => {
  if (!S.video.item) return;
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
      : (video.currentSrc || video.src);
    const wasPaused = video.paused;
    clearTimeout(S.video.errorRetryTimer);
    S.video.errorRetryTimer = setTimeout(() => {
      if (!S.video.item) return;
      video.src = src;
      video.load();
      video.playbackRate = Number($("sel-speed").value);
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
  playAdjacentVideo(1);
});
video.addEventListener("click", (e) => {
  if (e.target.closest(".video-gesture-zone")) return;
  togglePlay();
});
video.addEventListener("dblclick", () => toggleFullscreen($("video-player")));

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
  video.playbackRate = Number($("sel-speed").value);
});
$("sel-video-quality")?.addEventListener("change", () => {
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
    video.playbackRate = videoHoldState.previousRate || Number($("sel-speed").value) || 1;
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
      video.playbackRate = videoHoldState.speed;
      video.play().catch(() => {});
    }
    videoHoldState.interval = setInterval(() => {
      videoHoldState.speed = Math.min(5, videoHoldState.speed + 0.5);
      if (direction > 0) {
        video.playbackRate = videoHoldState.speed;
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
      case "0": $("sel-speed").value = "1"; video.playbackRate = 1; return;
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
  video.playbackRate = options[index];
  toast(`再生速度 ${options[index]}x`);
}

/* マウス戻る/進むボタン: 前後のメディアへ */
const AUX_MOUSE_DEBOUNCE_MS = 300;
let lastAuxMouse = { button: null, time: 0 };

function normalizeAuxDirection(event) {
  if (event.button === 3) return -1;
  if (event.button === 4) return 1;
  if (event.buttons & 8) return -1;
  if (event.buttons & 16) return 1;
  return 0;
}

function handleAuxMouseNavigation(event) {
  const direction = normalizeAuxDirection(event);
  if (!direction) return;
  event.preventDefault();
  event.stopPropagation();
  event.stopImmediatePropagation?.();
  const now = performance.now();
  if (lastAuxMouse.button === direction && now - lastAuxMouse.time < AUX_MOUSE_DEBOUNCE_MS) {
    return;
  }
  lastAuxMouse = { button: direction, time: now };
  if (S.settings.debug_aux_mouse) {
    console.debug("[FrameDeck] aux mouse", {
      type: event.type,
      button: event.button,
      buttons: event.buttons,
      direction,
    });
  }
  if (!$("comic-viewer").classList.contains("hidden") && S.comic.state) {
    navigateComicEntry(direction, "aux-mouse");
  } else if (!$("video-player").classList.contains("hidden") && S.video.item) {
    playAdjacentVideo(direction);
  }
}
window.addEventListener("mousedown", handleAuxMouseNavigation, { capture: true, passive: false });
window.addEventListener("auxclick", handleAuxMouseNavigation, { capture: true, passive: false });
window.addEventListener("mouseup", handleAuxMouseNavigation, { capture: true, passive: false });

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

  settingRow(grid, "漫画末尾の動作", makeSelect("comic_sequence_end_behavior", [
    ["stop", "停止"], ["wrap", "ループ"], ["prompt", "確認"],
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
    ["auto", "自動"], ["original", "原寸"], ["2160p", "4K"], ["1440p", "1440p"],
    ["1080p", "1080p"], ["720p", "720p"], ["480p", "480p"], ["360p", "360p"],
  ];
  settingRow(grid, "最大解像度", makeSelect("video_max_resolution", videoQualityOptions),
    "4K変換は通信量・CPU/GPU負荷・キャッシュ容量が大きくなります。");
  settingRow(grid, "PC 動画品質", makeSelect("video_profile_desktop", videoQualityOptions));
  settingRow(grid, "モバイル動画品質", makeSelect("video_profile_mobile", videoQualityOptions));
  settingRow(grid, "動画コーデック", makeSelect("video_codec", [
    ["h264", "H.264"], ["hevc", "HEVC"], ["vp9", "VP9"],
    ["av1", "AV1"], ["copy", "コピー可能ならコピー"],
  ]));
  settingRow(grid, "映像ビットレートkbps", makeNumberInput("video_bitrate_kbps", { min: 0, max: 100000, step: 50 }));
  settingRow(grid, "音声ビットレートkbps", makeNumberInput("video_audio_bitrate_kbps", { min: 0, max: 2000, step: 8 }));
  settingRow(grid, "HLSセグメント秒", makeNumberInput("video_segment_duration", { min: 1, max: 30, step: 1 }));
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
        const hadActiveRoot = Boolean(S.activeRootIds[kind]);
        await loadRoots();
        if (S.mode === kind && !hadActiveRoot) {
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
$("sel-library-root").onchange = (e) => switchLibraryRoot(e.target.value);
$("sel-library-root-mobile").onchange = (e) => switchLibraryRoot(e.target.value);
$("btn-delete").onclick = requestDelete;

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
  if (S.video.hls && S.video.item) requestHlsStop(S.video.item.id);
});

/* ================= init ================= */
async function init() {
  applyUiProfile();
  buildStarBar();
  updateModeButtons();
  try {
    S.settings = await api("/api/settings");
    await loadRoots();
  } catch (e) {
    toast(`初期化に失敗: ${e.message}`, true);
    return;
  }
  setupMobileSelects();
  await switchToActiveRoot();
  connectWs();
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  }
}
init();
