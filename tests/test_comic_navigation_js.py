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
  comicResumeNeeded = false; // Recovery behavior is covered separately below.
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


def test_resume_aborts_frozen_requests_and_recreates_missing_session():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js required')
    js = (Path(__file__).resolve().parents[1] / 'framedeck/web/static/js/app.js').read_text()
    code = js[js.index('let comicRequestBusy = false;'):js.index('function armComicBoundary')]
    harness = '''
const assert = require('node:assert/strict');
const old = {session_id:'old', root_item_id:'book', entry_id:'volume4', page_index:42, view_mode:'single', reading_direction:'rtl'};
const S = {comic:{state:old}};
const toast = () => {};
let resets=0;
const resetComicPreloader = () => {resets++;};
const setComicState = state => {S.comic.state=state;};
let handler;
const api = (path, options) => handler(path, options);
'''
    checks = '''
(async () => {
  handler = (path, options) => new Promise((resolve,reject) => {
    options.signal.addEventListener('abort',()=>reject(Object.assign(new Error('aborted'),{name:'AbortError'})));
  });
  const frozen = comicCall('next-page');
  suspendComicRequests();
  assert.equal(await frozen, null);
  assert.equal(comicRequestBusy,false);
  assert.equal(resets,1);
  const calls=[];
  handler = async (path, options) => {
    calls.push(path);
    if(path==='/api/comics/session/old') throw Object.assign(new Error('missing'),{status:404});
    if(path==='/api/comics/session') {
      assert.equal(options.json.entry_id,'volume4');
      assert.equal(options.json.item_id,'book');
      return {...old,session_id:'new',page_index:0};
    }
    if(path.endsWith('/options')) return {...old,session_id:'new',page_index:0};
    if(path.endsWith('/goto')) {
      assert.equal(options.json.page_index,42);
      return {...old,session_id:'new'};
    }
    throw new Error('unexpected request '+path);
  };
  await comicCall(null);
  assert.equal(S.comic.state.session_id,'new');
  assert.equal(S.comic.state.page_index,42);
  assert.equal(calls.length,4);
  assert.equal(comicResumeNeeded,false);
  // A timed-out mutation is not automatically replayed.
  handler = async () => {throw Object.assign(new Error('timeout'),{name:'AbortError'});};
  assert.equal(await comicCall('next-page'),null);
  assert.equal(comicRequestBusy,false);
  const recovery=[];
  handler = async path => {recovery.push(path); return {...old,session_id:'new',page_index:43};};
  await comicCall(null);
  assert.deepEqual(recovery,['/api/comics/session/new']);
  assert.equal(S.comic.state.page_index,43);
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
    subprocess.run([node, '-e', harness + code + checks], check=True, timeout=10)
