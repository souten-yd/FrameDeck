"""Exercise the real preloader with controlled image events and clocks."""
import shutil
import subprocess
from pathlib import Path

import pytest


def test_preloader_keeps_useful_work_and_recovers_from_stalls():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js required')
    js = (Path(__file__).resolve().parents[1] / 'framedeck/web/static/js/app.js').read_text()
    code = js[js.index('const comicPreloader = {'):js.index('function updateComicControls')]
    harness = '''
const assert = require('node:assert/strict');
const timers = new Map(); let timerId=0;
const setTimeout = cb => {timers.set(++timerId,cb); return timerId;};
const clearTimeout = id => timers.delete(id);
const window = {setTimeout};
const images=[];
class Image {
  constructor() { images.push(this); this.naturalWidth=0; this.removed=false; }
  removeAttribute() {this.removed=true;}
}
const S={comic:{state:{visible_pages:[0]}}};
const $ = () => ({querySelectorAll:()=>[]});
const comicVisiblePageSpecs = state => state.visible_pages.map(index=>({index}));
const comicPageUrl = index => 'page-'+index;
'''
    checks = '''
queueComicPreloads('volume-a',[{index:1},{index:2},{index:3}]);
const first=images[0];
// Advancing/refreshing the window must not cancel a still-useful request.
queueComicPreloads('volume-a',[{index:1},{index:2},{index:3},{index:4}]);
assert.equal(images.length,1);
assert.equal(first.removed,false);
first.naturalWidth=100; first.onload();
assert.equal(images[1].src,'page-2');
assert.equal(timers.size,1);
// A missing load/error event must advance the queue after its deadline.
const lateLoad=images[1].onload;
const expire=timers.get(comicPreloader.active.timer); expire();
assert.equal(images[1].removed,true);
assert.equal(images[2].src,'page-3');
lateLoad(); // A late callback cannot clear the new active request.
assert.equal(comicPreloader.active.image,images[2]);
// Obsolete work is still cancelled and content identity still isolates books.
queueComicPreloads('volume-b',[{index:7}]);
assert.equal(images[2].removed,true);
assert.equal(comicPreloader.active.url,'page-7');
resetComicPreloader();
assert.equal(timers.size,0);
assert.equal(comicPreloader.active,null);
'''
    subprocess.run([node, '-e', harness + code + checks], check=True, timeout=10)
