"""Regression tests for delayed library responses when changing viewing modes."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_library_mode_navigation_rejects_stale_responses():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the frontend behavioral test")

    js = (Path(__file__).resolve().parents[1] /
          "framedeck/web/static/js/app.js").read_text("utf-8")
    code = "\n".join([
        js[js.index("const LIBRARY_MODE_STORAGE_KEY"):js.index("function renderRootSelectorInto(")],
        js[js.index("function resetNavigationState("):js.index("function pushHistory(")],
        js[js.index("async function loadFolder("):js.index("function renderBreadcrumb(")],
        js[js.index("function showMissingLibraryRoot("):js.index("/* ================= star rating */")],
        js[js.index("async function setMode("):js.index("function refreshCurrentFolder(")],
    ])
    assert "restoreLibraryMode();" in js[js.index("async function init()"):]

    harness = r'''
const assert = require("node:assert/strict");
const store = new Map();
const localStorage = {
  getItem: key => store.get(key) ?? null,
  setItem: (key, value) => store.set(key, String(value)),
  removeItem: key => store.delete(key),
};
const S = {
  mode: "comic",
  roots: [
    {id: "c", kind: "comic", display_name: "Comics"},
    {id: "v", kind: "video", display_name: "Videos"},
    {id: "v2", kind: "video", display_name: "Other Videos"},
  ],
  activeRootIds: {comic: "c", video: "v"},
  folderId: null, folderInfo: null, items: [],
  selectedId: null, readingItemId: null,
  history: [], histIndex: -1, checked: new Set(),
};
const nodes = new Map();
const $ = id => {
  if (!nodes.has(id)) {
    const classes = new Set();
    nodes.set(id, {
      value: id === "sel-sort" ? "name" : "all",
      classList: {
        toggle: (name, on) => on ? classes.add(name) : classes.delete(name),
        contains: name => classes.has(name),
      },
    });
  }
  return nodes.get(id);
};
const pending = [];
const api = url => new Promise((resolve, reject) => pending.push({url, resolve, reject}));
const take = text => {
  const idx = pending.findIndex(req => req.url.includes(text));
  assert.notEqual(idx, -1, "missing pending " + text +
    " (seen " + pending.map(req => req.url).join(",") + ")");
  return pending.splice(idx, 1)[0];
};
const tick = () => new Promise(resolve => setImmediate(resolve));
const response = (name, mediaType) => ({
  folder: {id: name, display_name: name, parent_id: null},
  items: [{id: name + "-item", media_type: mediaType}],
});
let renderCount = 0;
const alerts = [];
const toast = text => alerts.push(text);
const rememberFolderScroll = () => {};
const prepareFolderPosition = () => {};
const restoreFolderPosition = () => {};
const renderBreadcrumb = () => {};
const renderList = () => {renderCount++;};
const updateNavButtons = () => {};
const updateBulkBar = () => {};
const renderRootSelectors = () => {};
const clearCurrentViewer = () => {};
const closeMobileDrawer = () => {};
const pushHistory = id => {S.history.push(id); S.histIndex = S.history.length - 1;};
'''
    assertions = r'''
(async () => {
  // Restore previously selected mode after an unrelated page reload.
  store.set("framedeck.libraryMode", "video");
  restoreLibraryMode();
  assert.equal(S.mode, "video");
  store.set("framedeck.libraryMode", "invalid");
  S.mode = "comic";
  restoreLibraryMode();
  assert.equal(S.mode, "comic");

  // Initial comic restore is slow; the user chooses video in the meantime.
  const startup = switchToActiveRoot({announceResume: true});
  const oldStart = take("start-folder?mode=comic");
  const videoSwitch = setMode("video");
  assert.equal(localStorage.getItem("framedeck.libraryMode"), "video");
  take("start-folder?mode=video").resolve({folder_id:"v", root_id:"v"});
  await tick();
  take("items?").resolve(response("v", "video"));
  await videoSwitch;
  const rendersAfterVideo = renderCount;
  oldStart.resolve({folder_id:"c", root_id:"c", relative_path:"Comics"});
  await startup;
  assert.equal(S.mode, "video");
  assert.equal(S.folderId, "v");
  assert.equal(S.items[0].media_type, "video");
  assert.equal(renderCount, rendersAfterVideo);
  assert.equal($("btn-mode-video").classList.contains("active"), true);
  assert.equal($("btn-mode-comic").classList.contains("active"), false);
  assert.equal(pending.length, 0, "stale start must not cause another items request");

  // An already-running comic folder fetch also must not overwrite video.
  const comicSwitch = setMode("comic");
  take("start-folder?mode=comic").resolve({folder_id:"c", root_id:"c"});
  await tick();
  const oldComicItems = take("items?");
  const videoAgain = setMode("video");
  take("start-folder?mode=video").resolve({folder_id:"v", root_id:"v"});
  await tick();
  take("items?").resolve(response("v", "video"));
  await videoAgain;
  oldComicItems.resolve(response("c", "comic"));
  await comicSwitch;
  assert.equal(S.folderId, "v");
  assert.equal(S.items[0].media_type, "video");
  assert.equal(S.history.at(-1), "v");

  // Same-mode quick folder navigation: the most recent request wins.
  const first = loadFolder("v-old");
  const oldItems = take("items?");
  const second = loadFolder("v-new");
  take("items?").resolve(response("v-new", "video"));
  await second;
  oldItems.resolve(response("v-old", "video"));
  assert.equal(await first, null);
  assert.equal(S.folderId, "v-new");

  // An obsolete failed response cannot display an error over newer content.
  const failing = loadFolder("v-error");
  const obsolete = take("items?");
  const current = loadFolder("v-final");
  take("items?").resolve(response("v-final", "video"));
  await current;
  const alertsBefore = alerts.length;
  obsolete.reject(new Error("old network error"));
  await failing;
  assert.equal(alerts.length, alertsBefore);
  assert.equal(S.folderId, "v-final");

  // A manual root selection invalidates an in-flight server-side resume.
  const resuming = switchToActiveRoot();
  const oldResume = take("start-folder?mode=video");
  const rootSwitch = switchLibraryRoot("v2");
  take("items?").resolve(response("v2", "video"));
  await rootSwitch;
  oldResume.resolve({folder_id:"v", root_id:"v"});
  await resuming;
  assert.equal(S.folderId, "v2");
  assert.equal(S.activeRootIds.video, "v2");
  assert.equal(pending.length, 0);
})().catch(error => {console.error(error); process.exitCode = 1;});
'''
    subprocess.run([node, "-e", harness + code + assertions],
                   check=True, timeout=15)
