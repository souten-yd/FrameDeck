from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_update_ui_assets_are_loaded_after_main_assets():
    html = (ROOT / "framedeck/web/templates/index.html").read_text(encoding="utf-8")
    assert '/static/css/updater.css' in html
    assert '/static/js/updater.js' in html
    assert html.index('/static/css/app.css') < html.index('/static/css/updater.css')
    assert html.index('/static/js/app.js') < html.index('/static/js/updater.js')


def test_update_ui_uses_explicit_user_actions():
    js = (ROOT / "framedeck/web/static/js/updater.js").read_text(encoding="utf-8")
    assert '/api/update/status' in js
    assert '/api/update/check' in js
    assert '/api/update/apply' in js
    assert 'window.confirm' in js
    assert '更新を確認' in js
    # Opening Settings reads only local status; GitHub release lookup is behind the button.
    assert 'checkForUpdates(panel)' in js


def test_settings_shows_installed_version_at_the_top():
    js = (ROOT / "framedeck/web/static/js/updater.js").read_text(encoding="utf-8")
    css = (ROOT / "framedeck/web/static/css/updater.css").read_text(encoding="utf-8")

    assert 'FrameDeck <span class="update-current-version" data-current-version>v-</span>' in js
    assert 'container.prepend(panel)' in js
    assert '.update-current-version' in css


def test_completed_update_unlocks_settings_and_reloads_only_once_for_its_initiator():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the frontend behavioral test")
    js = (ROOT / "framedeck/web/static/js/updater.js").read_text(encoding="utf-8")
    code = js[js.index("  function setMessage("):js.index("  async function pollJob(")]
    harness = """
const assert = require('node:assert/strict');
let updateStartedHere = false, reloads = 0;
const callbacks = [];
const window = {setTimeout: callback => callbacks.push(callback)};
const location = {reload: () => reloads++};
const nodes = new Map();
const panel = {
  querySelector: selector => {
    if (!nodes.has(selector)) nodes.set(selector, {
      dataset: {}, style: {}, disabled: false,
      classList: {toggle: (name, value) => nodes.get(selector)[name] = value},
    });
    return nodes.get(selector);
  },
  querySelectorAll: () => [panel.querySelector('[data-update-check]'), panel.querySelector('[data-update-apply]')],
};
"""
    checks = """
const job = {status: 'restarting', current_version: '2.4.2', target_version: '2.4.5'};
assert.equal(renderJob(panel, job), true);
assert.equal(panel.querySelector('[data-update-check]').disabled, true);
job.status = 'completed';
job.current_version = '2.4.9';
assert.equal(renderJob(panel, job), false);
assert.equal(panel.querySelector('[data-update-check]').disabled, false);
assert.equal(panel.querySelector('[data-update-apply]').disabled, false);
assert.equal(panel.querySelector('[data-update-progress]').hidden, true);
assert.equal(panel.querySelector('[data-update-message]').dataset.kind, 'ok');
assert.equal(callbacks.length, 0); // Opening Settings on an old job never reloads.
updateStartedHere = true;
renderJob(panel, job);
renderJob(panel, job); // Overlapping poll or reopened Settings.
assert.equal(callbacks.length, 1);
callbacks[0]();
assert.equal(reloads, 1);
renderJob(panel, {status: 'checked', current_version: '2.4.9', target_version: '2.4.9'});
assert.equal(callbacks.length, 1);
"""
    subprocess.run([node, "-e", harness + code + checks], check=True, timeout=10)
