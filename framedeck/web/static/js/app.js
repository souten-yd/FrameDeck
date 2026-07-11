/* FrameDeck Web UI */
"use strict";

/* ================= state ================= */
const S = {
  mode: "comic",
  roots: [],
  folderId: null,
  folderInfo: null,
  items: [],
  selectedId: null,
  readingItemId: null,
  history: [],
  histIndex: -1,
  settings: {},
  comic: { state: null, pendingNext: false, pendingPrev: false },
  video: {
    item: null, info: null, transcode: false, offset: 0,
    saveTimer: null, duration: 0,
  },
};

const $ = (id) => document.getElementById(id);

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
function rootForMode() {
  const kind = S.mode === "comic" ? "comic" : "video";
  return S.roots.find((r) => r.kind === kind) || S.roots[0] || null;
}

async function loadRoots() {
  S.roots = await api("/api/library/roots");
}

function pushHistory(folderId) {
  if (S.history[S.histIndex] === folderId) return;
  S.history = S.history.slice(0, S.histIndex + 1);
  S.history.push(folderId);
  S.histIndex = S.history.length - 1;
  updateNavButtons();
}

function updateNavButtons() {
  $("btn-folder-back").disabled = S.histIndex <= 0;
  $("btn-folder-forward").disabled = S.histIndex >= S.history.length - 1;
  $("btn-folder-up").disabled = !(S.folderInfo && S.folderInfo.parent_id);
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
  $("breadcrumb").textContent = info
    ? (info.relative_path ? `${info.display_name} — ${info.relative_path}` : info.display_name)
    : "";
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

async function openComic(item) {
  try {
    const result = await api("/api/comics/session", { json: { item_id: item.id } });
    if (result.requires_choice) {
      chooseEntry(item, result.entries);
      return;
    }
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
        const state = await api("/api/comics/session", {
          json: { item_id: item.id, entry_id: entry.id },
        });
        S.readingItemId = item.id;
        setComicState(state);
      } catch (e) { toast(e.message, true); }
    };
    list.appendChild(li);
  }
  showModal("開く漫画を選択してください", list,
            [{ label: "キャンセル", onClick: closeModal }]);
}

function setComicState(state) {
  S.comic.state = state;
  S.comic.pendingNext = false;
  S.comic.pendingPrev = false;
  showViewer("comic");
  if (state.root_item_id &&
      S.items.some((i) => i.id === state.root_item_id)) {
    S.readingItemId = state.root_item_id;
    S.selectedId = state.root_item_id;
    updateStarBar();
  }
  renderList();
  renderComicPages();
  updateComicControls();
  preloadComicPages();
}

function comicPageUrl(pageIndex) {
  return `/api/comics/session/${S.comic.state.session_id}/page/${pageIndex}`;
}

function renderComicPages() {
  const state = S.comic.state;
  const container = $("comic-pages");
  container.innerHTML = "";
  $("comic-msg").classList.add("hidden");
  let pages = [...state.visible_pages];
  if (state.reading_direction === "rtl" && pages.length === 2) {
    pages.reverse();
  }
  container.classList.toggle("two", pages.length === 2);
  for (const pageIndex of pages) {
    const img = document.createElement("img");
    img.src = comicPageUrl(pageIndex);
    img.alt = `page ${pageIndex + 1}`;
    img.draggable = false;
    img.onerror = () => {
      $("comic-msg").textContent = "画像を読み込めませんでした";
      $("comic-msg").classList.remove("hidden");
    };
    container.appendChild(img);
  }
}

function preloadComicPages() {
  const state = S.comic.state;
  const last = state.visible_pages[state.visible_pages.length - 1];
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
  $("btn-prev-entry").disabled =
    !state.has_previous_entry &&
    S.settings.comic_sequence_end_behavior === "stop";
  $("btn-next-entry").disabled =
    !state.has_next_entry &&
    S.settings.comic_sequence_end_behavior === "stop";
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

async function comicForward() {
  const before = S.comic.state;
  const state = await comicCall("next-page");
  if (!state) return;
  if (state.page_index === before.page_index && state.entry_id === before.entry_id) {
    // 末尾ページ: もう一度の操作で次の漫画へ
    if (state.has_next_entry || S.settings.comic_sequence_end_behavior === "wrap") {
      if (S.comic.pendingNext) { await comicNextEntry(); return; }
      S.comic.pendingNext = true;
      toast("最後のページです。もう一度で次の漫画へ ⏭");
    } else {
      toast("最後の漫画の最後のページです");
    }
    return;
  }
  setComicState(state);
}

async function comicBackward() {
  const before = S.comic.state;
  const state = await comicCall("previous-page");
  if (!state) return;
  if (state.page_index === before.page_index && state.entry_id === before.entry_id) {
    if (state.has_previous_entry || S.settings.comic_sequence_end_behavior === "wrap") {
      if (S.comic.pendingPrev) { await comicPrevEntry(); return; }
      S.comic.pendingPrev = true;
      toast("先頭ページです。もう一度で前の漫画へ ⏮");
    } else {
      toast("最初の漫画の先頭ページです");
    }
    return;
  }
  setComicState(state);
}

async function comicNextEntry() {
  const state = await comicCall("next-entry");
  if (!state) return;
  if (state.at_sequence_end) {
    if (S.settings.comic_sequence_end_behavior === "prompt") {
      toast("シーケンスの末尾です(設定: 確認)");
    } else {
      toast("最後の漫画です");
    }
    S.comic.state = state;
    updateComicControls();
    return;
  }
  setComicState(state);
}

async function comicPrevEntry() {
  const state = await comicCall("previous-entry");
  if (!state) return;
  if (state.at_sequence_start) {
    toast("最初の漫画です");
    S.comic.state = state;
    updateComicControls();
    return;
  }
  setComicState(state);
}

/* comic operations wiring */
function comicNextAction() { comicForward(); }
function comicPrevAction() { comicBackward(); }
function comicTapLeft() {
  if (!S.comic.state) return;
  S.comic.state.reading_direction === "rtl" ? comicForward() : comicBackward();
}
function comicTapRight() {
  if (!S.comic.state) return;
  S.comic.state.reading_direction === "rtl" ? comicBackward() : comicForward();
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

$("btn-comic-fwd").onclick = comicForward;
$("btn-comic-back").onclick = comicBackward;
$("btn-next-entry").onclick = comicNextEntry;
$("btn-prev-entry").onclick = comicPrevEntry;
$("btn-comic-full").onclick = () => toggleFullscreen($("comic-viewer"));
$("comic-tap-left").onclick = comicTapLeft;
$("comic-tap-right").onclick = comicTapRight;
$("comic-tap-left").ondblclick = (e) => e.preventDefault();

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
  if (e.deltaY > 0) comicForward(); else comicBackward();
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
  $("video-title").textContent = item.display_name;

  const resume = detail.resume_position || 0;
  if (detail.info.direct_play) {
    S.video.transcode = false;
    video.src = `/api/videos/${item.id}/stream`;
    if (resume > 0) {
      video.addEventListener("loadedmetadata", () => {
        video.currentTime = resume;
      }, { once: true });
      toast(`続きから再生: ${fmtTime(resume)}`);
    }
  } else if (detail.transcode_available) {
    S.video.transcode = true;
    S.video.offset = resume;
    video.src = `/api/videos/${item.id}/stream-transcode?start=${resume}`;
    $("video-badge").textContent =
      `変換ストリーミング (${detail.info.video_codec || detail.info.container})`;
    $("video-badge").classList.remove("hidden");
    if (resume > 0) toast(`続きから再生: ${fmtTime(resume)}`);
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
function totalDuration() {
  if (S.video.transcode) return S.video.duration;
  return video.duration || S.video.duration || 0;
}

function saveVideoProgress() {
  const item = S.video.item;
  if (!item) return;
  const payload = JSON.stringify({
    position_seconds: currentPosition(),
    duration_seconds: totalDuration(),
    playback_speed: video.playbackRate,
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

function stopVideo() {
  if (S.video.item) saveVideoProgress();
  stopProgressTimer();
  video.pause();
  video.removeAttribute("src");
  video.load();
  S.video.item = null;
}

function videoSeekTo(seconds) {
  seconds = Math.max(0, Math.min(seconds, totalDuration() || Infinity));
  if (S.video.transcode) {
    const item = S.video.item;
    if (!item) return;
    S.video.offset = seconds;
    const wasPaused = video.paused;
    video.src = `/api/videos/${item.id}/stream-transcode?start=${seconds.toFixed(2)}`;
    video.playbackRate = Number($("sel-speed").value);
    if (!wasPaused) video.play().catch(() => {});
  } else {
    video.currentTime = seconds;
  }
}
function videoSeekBy(delta) { videoSeekTo(currentPosition() + delta); }

function updateVideoUi() {
  const duration = totalDuration();
  $("video-time").textContent =
    `${fmtTime(currentPosition())} / ${fmtTime(duration)}`;
  if (duration > 0 && !videoSeekDragging) {
    $("video-seek").value = Math.round(currentPosition() / duration * 1000);
  }
  $("btn-play").textContent = video.paused ? "▶" : "⏸";
  $("btn-mute").textContent =
    (video.muted || video.volume === 0) ? "🔇" : "🔊";
}

let videoSeekDragging = false;
$("video-seek").addEventListener("pointerdown", () => { videoSeekDragging = true; });
$("video-seek").addEventListener("change", () => {
  const duration = totalDuration();
  if (duration > 0) videoSeekTo(Number($("video-seek").value) / 1000 * duration);
  videoSeekDragging = false;
});

video.addEventListener("timeupdate", updateVideoUi);
video.addEventListener("play", updateVideoUi);
video.addEventListener("pause", () => { updateVideoUi(); saveVideoProgress(); });
video.addEventListener("waiting", () => $("video-spinner").classList.remove("hidden"));
video.addEventListener("canplay", () => $("video-spinner").classList.add("hidden"));
video.addEventListener("error", () => {
  $("video-spinner").classList.add("hidden");
  if (S.video.item) {
    $("video-msg").textContent = "再生エラーが発生しました";
    $("video-msg").classList.remove("hidden");
  }
});
video.addEventListener("ended", () => {
  saveVideoProgress();
  playAdjacentVideo(1);
});
video.addEventListener("click", () => togglePlay());
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
$("btn-pip").onclick = async () => {
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

/* モバイル: 左右ダブルタップで±10秒 */
let lastTap = { t: 0, x: 0 };
$("video-stage").addEventListener("touchend", (e) => {
  if (!S.video.item) return;
  const x = e.changedTouches[0].clientX;
  const now = Date.now();
  if (now - lastTap.t < 350 && Math.abs(x - lastTap.x) < 40) {
    const rect = $("video-stage").getBoundingClientRect();
    const ratio = (x - rect.left) / rect.width;
    if (ratio < 0.35) videoSeekBy(-10);
    else if (ratio > 0.65) videoSeekBy(10);
    else togglePlay();
    lastTap = { t: 0, x: 0 };
  } else {
    lastTap = { t: now, x };
  }
}, { passive: true });

/* ================= fullscreen / UI visibility ================= */
function toggleFullscreen(el) {
  if (document.fullscreenElement) document.exitFullscreen();
  else el.requestFullscreen?.();
}

function setupAutoHide(viewerId) {
  const viewer = $(viewerId);
  let hideTimer = null;
  const show = () => {
    viewer.classList.add("show-ui");
    clearTimeout(hideTimer);
    hideTimer = setTimeout(() => viewer.classList.remove("show-ui"), 2200);
  };
  viewer.addEventListener("mousemove", show);
  viewer.addEventListener("touchstart", show, { passive: true });
  viewer.querySelector(".controls-bar").addEventListener("mousemove", (e) => {
    clearTimeout(hideTimer);
    viewer.classList.add("show-ui");
    e.stopPropagation();
  });
  show();
}
setupAutoHide("comic-viewer");
setupAutoHide("video-player");

/* ================= keyboard ================= */
document.addEventListener("keydown", (e) => {
  if (e.target.matches("input, select, textarea")) return;
  const comicOpen = !$("comic-viewer").classList.contains("hidden") && S.comic.state;
  const videoOpen = !$("video-player").classList.contains("hidden") && S.video.item;

  if (comicOpen) {
    switch (e.key) {
      case "ArrowLeft": e.preventDefault(); comicTapLeft(); return;
      case "ArrowRight": e.preventDefault(); comicTapRight(); return;
      case "PageDown": case " ": e.preventDefault(); comicForward(); return;
      case "PageUp": e.preventDefault(); comicBackward(); return;
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
document.addEventListener("mouseup", (e) => {
  if (e.button !== 3 && e.button !== 4) return;
  const delta = e.button === 3 ? -1 : 1;
  if (!$("comic-viewer").classList.contains("hidden") && S.comic.state) {
    e.preventDefault();
    delta > 0 ? comicNextEntry() : comicPrevEntry();
  } else if (!$("video-player").classList.contains("hidden") && S.video.item) {
    e.preventDefault();
    playAdjacentVideo(delta);
  }
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
  settingRow(grid, "削除方法", makeSelect("delete_to_trash", [
    ["true", "ゴミ箱へ移動"], ["false", "完全削除"],
  ]));
  wrap.appendChild(grid);

  /* ライブラリルート管理 */
  const rootsTitle = document.createElement("h3");
  rootsTitle.textContent = "ライブラリルート";
  rootsTitle.style.margin = "16px 0 8px";
  wrap.appendChild(rootsTitle);
  const rootList = document.createElement("ul");
  rootList.className = "choice-list";
  for (const root of S.roots) {
    const li = document.createElement("li");
    li.textContent = `📁 ${root.display_name} (${root.kind})`;
    const remove = document.createElement("button");
    remove.className = "modal-btn danger";
    remove.textContent = "解除";
    remove.style.marginLeft = "auto";
    remove.onclick = async () => {
      await api(`/api/library/roots/${root.id}`, { method: "DELETE" });
      await loadRoots();
      closeModal();
      toast("ルートを解除しました");
    };
    li.appendChild(remove);
    li.onclick = (e) => e.stopPropagation();
    rootList.appendChild(li);
  }
  wrap.appendChild(rootList);

  const addWrap = document.createElement("div");
  addWrap.style.display = "flex";
  addWrap.style.gap = "8px";
  addWrap.style.marginTop = "8px";
  const pathInput = document.createElement("input");
  pathInput.type = "text";
  pathInput.placeholder = "サーバ上のフォルダパス (例: /mnt/Download/Manga)";
  pathInput.style.flex = "1";
  pathInput.style.background = "var(--surface)";
  pathInput.style.border = "1px solid var(--border)";
  pathInput.style.borderRadius = "8px";
  pathInput.style.color = "var(--text)";
  pathInput.style.padding = "8px";
  const addBtn = document.createElement("button");
  addBtn.className = "modal-btn primary";
  addBtn.textContent = "追加";
  addBtn.onclick = async () => {
    try {
      await api("/api/library/roots", {
        json: { path: pathInput.value, kind: S.mode },
      });
      await loadRoots();
      closeModal();
      toast("ルートを追加しました");
    } catch (e) { toast(e.message, true); }
  };
  addWrap.append(pathInput, addBtn);
  wrap.appendChild(addWrap);

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
function setMode(mode) {
  if (S.mode === mode) return;
  S.mode = mode;
  S.selectedId = null;
  S.readingItemId = null;
  S.comic.state = null;
  stopVideo();
  $("comic-viewer").classList.add("hidden");
  $("video-player").classList.add("hidden");
  $("viewer-placeholder").classList.remove("hidden");
  $("placeholder-icon").textContent = mode === "comic" ? "📖" : "▶";
  $("placeholder-text").textContent =
    mode === "comic" ? "漫画を選択してください" : "動画を選択してください";
  updateModeButtons();
  const root = rootForMode();
  if (root) loadFolder(root.id);
}
function updateModeButtons() {
  $("btn-mode-comic").classList.toggle("active", S.mode === "comic");
  $("btn-mode-video").classList.toggle("active", S.mode === "video");
}
$("btn-mode-comic").onclick = () => setMode("comic");
$("btn-mode-video").onclick = () => setMode("video");

$("btn-refresh").onclick = () => loadFolder(S.folderId, { remember: false });
$("btn-folder-back").onclick = () => {
  if (S.histIndex > 0) {
    S.histIndex--;
    loadFolder(S.history[S.histIndex], { remember: false });
  }
};
$("btn-folder-forward").onclick = () => {
  if (S.histIndex < S.history.length - 1) {
    S.histIndex++;
    loadFolder(S.history[S.histIndex], { remember: false });
  }
};
$("btn-folder-up").onclick = () => {
  if (S.folderInfo && S.folderInfo.parent_id) loadFolder(S.folderInfo.parent_id);
};
$("sel-sort").onchange = () => loadFolder(S.folderId, { remember: false });
$("sel-filter").onchange = () => loadFolder(S.folderId, { remember: false });
$("btn-delete").onclick = requestDelete;

/* mobile drawer */
$("btn-list-toggle").onclick = () => {
  $("library-pane").classList.toggle("open");
};
function closeMobileDrawer() {
  $("library-pane").classList.remove("open");
}
document.addEventListener("click", (e) => {
  if (window.innerWidth > 760) return;
  const pane = $("library-pane");
  if (pane.classList.contains("open") &&
      !pane.contains(e.target) && e.target !== $("btn-list-toggle")) {
    pane.classList.remove("open");
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
});

/* ================= init ================= */
async function init() {
  buildStarBar();
  updateModeButtons();
  try {
    S.settings = await api("/api/settings");
    await loadRoots();
  } catch (e) {
    toast(`初期化に失敗: ${e.message}`, true);
    return;
  }
  const root = rootForMode();
  if (root) await loadFolder(root.id);
  else toast("設定からライブラリルートを追加してください");
  connectWs();
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  }
}
init();
