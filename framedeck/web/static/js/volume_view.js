/* FrameDeck adaptive comic volume list.
 * Existing library APIs and conventional rendering stay intact; this layer only
 * changes the presentation of comic folders when volume recognition is useful.
 */
"use strict";

(() => {
  const MODE_KEY_PREFIX = "framedeck.volumeView.";
  const state = { folderId: null, meta: null, rawItems: [] };
  const originalLoadFolder = loadFolder;
  const originalRenderList = renderList;

  function storageKey(folderId) {
    return `${MODE_KEY_PREFIX}${folderId}`;
  }

  function savedMode(folderId) {
    return localStorage.getItem(storageKey(folderId)) || "auto";
  }

  function effectiveMode() {
    if (S.mode !== "comic" || !state.meta || state.folderId !== S.folderId) return "files";
    const selected = savedMode(S.folderId);
    return selected === "auto" ? state.meta.recommended_mode : selected;
  }

  function ensureSelector() {
    let select = document.getElementById("sel-volume-view");
    if (select) return select;
    const toolbar = document.getElementById("library-toolbar");
    if (!toolbar) return null;
    select = document.createElement("select");
    select.id = "sel-volume-view";
    select.className = "mini-btn volume-view-select";
    select.title = "漫画一覧の表示方式";
    select.setAttribute("aria-label", "漫画一覧の表示方式");
    for (const [value, label] of [["auto", "表示: 自動"], ["volume", "表示: 巻"], ["files", "表示: ファイル"]]) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = label;
      select.appendChild(option);
    }
    select.onchange = () => {
      if (!S.folderId) return;
      localStorage.setItem(storageKey(S.folderId), select.value);
      renderList();
    };
    toolbar.insertBefore(select, document.getElementById("btn-select-mode"));
    return select;
  }

  function applyModeToItems() {
    if (!state.rawItems.length || state.folderId !== S.folderId) return;
    const mode = effectiveMode();
    if (mode !== "volume") {
      S.items = state.rawItems.map((item) => ({ ...item }));
      return;
    }

    const byId = new Map((state.meta?.entries || []).map((entry, index) => [entry.item_id, { ...entry, index }]));
    S.items = state.rawItems.map((item, sourceIndex) => {
      const entry = byId.get(item.id);
      return {
        ...item,
        _volumeSourceIndex: sourceIndex,
        _volumeEntry: entry || null,
        display_name: entry ? entry.label : item.display_name,
      };
    }).sort((a, b) => {
      const af = a.media_type === "folder";
      const bf = b.media_type === "folder";
      if (af !== bf) return af ? -1 : 1;
      const ae = a._volumeEntry;
      const be = b._volumeEntry;
      if (ae && be) return ae.index - be.index;
      if (ae !== be) return ae ? -1 : 1;
      return a._volumeSourceIndex - b._volumeSourceIndex;
    });
  }

  function decorateVolumeList() {
    const list = document.getElementById("item-list");
    if (!list) return;
    const active = effectiveMode() === "volume";
    list.classList.toggle("volume-list", active);
    if (!active) return;
    for (const li of list.querySelectorAll("li")) {
      const item = S.items.find((candidate) => candidate.id === li.dataset.id);
      if (!item || item.media_type === "folder") continue;
      li.classList.add("volume-row");
      const icon = li.querySelector(".item-icon");
      const stars = li.querySelector(".item-stars");
      if (icon) icon.hidden = true;
      if (stars) stars.hidden = true;
      const name = li.querySelector(".item-name");
      if (name) {
        name.title = item._volumeEntry?.recognized
          ? `${item._volumeEntry.kind} · confidence ${Math.round((item._volumeEntry.confidence || 0) * 100)}%`
          : item.display_name;
      }
    }
  }

  renderList = function adaptiveRenderList() {
    const selector = ensureSelector();
    if (selector) {
      selector.hidden = S.mode !== "comic";
      if (S.folderId) selector.value = savedMode(S.folderId);
    }
    applyModeToItems();
    originalRenderList();
    decorateVolumeList();
  };

  loadFolder = async function adaptiveLoadFolder(folderId, options = {}) {
    state.folderId = null;
    state.meta = null;
    state.rawItems = [];
    await originalLoadFolder(folderId, options);
    state.folderId = S.folderId;
    state.rawItems = S.items.map((item) => ({ ...item }));
    if (S.mode !== "comic" || !S.folderId) {
      renderList();
      return;
    }
    try {
      state.meta = await api(`/api/library/volume-view?folder_id=${encodeURIComponent(S.folderId)}`);
    } catch (error) {
      // Supplemental analysis must never make the conventional library unusable.
      state.meta = { recommended_mode: "files", recognized_ratio: 0, entries: [] };
      console.warn("volume view analysis failed", error);
    }
    renderList();
  };

  ensureSelector();
})();
