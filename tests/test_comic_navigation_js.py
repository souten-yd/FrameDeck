"""Run the actual navigation functions with controlled delayed network replies."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_rapid_navigation_and_page_updates_do_not_accumulate_work():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is required for the frontend behavioral test')
    js = (Path(__file__).resolve().parents[1] / 'framedeck/web/static/js/app.js').read_text()
    call = js[js.index('let comicRequestBusy = false;'):js.index('function armComicBoundary')]
    state = js[js.index('function setComicState('):js.index('function focusLibraryItem(')]
    harness = '''
const assert = require('node:assert/strict');
const S = { comic: { state: { session_id: 'one' } }, items: [{id:'book'}], readingItemId:'book', selectedId:'book' };
let calls = 0, resolveRequest, rejectRequest, renders = 0;
const api = () => { calls++; return new Promise((resolve,reject) => {resolveRequest=resolve; rejectRequest=reject;}); };
const toast = () => {};
const clearComicBoundaryState = () => {};
const updateComicView = state => {S.comic.state = state;};
const updateStarBar = () => {};
const renderList = () => {renders++;};
'''
    assertions = '''
(async () => {
  const first = comicCall('next-page');
  await Promise.all(Array.from({length:100}, () => comicCall('next-page')));
  assert.equal(calls, 1);
  resolveRequest({session_id:'one', page_index:1});
  assert.equal((await first).page_index,1);
  const failure = comicCall('next-page');
  rejectRequest(new Error('offline'));
  assert.equal(await failure, null);
  const stale = comicCall('next-page');
  S.comic.state = {session_id:'two'};
  resolveRequest({session_id:'one', page_index:2});
  assert.equal(await stale,null);
  for(let i=0;i<100;i++) setComicState({session_id:'two',root_item_id:'book',page_index:i});
  assert.equal(renders,0);
  S.items.push({id:'next-book'});
  setComicState({session_id:'two',root_item_id:'next-book',page_index:0});
  assert.equal(renders,1);
})().catch(error=>{ console.error(error); process.exitCode=1; });
'''
    subprocess.run([node, '-e', harness + call + state + assertions], check=True, timeout=10)
