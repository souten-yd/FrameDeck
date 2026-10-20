# Comic HDD I/O reduction

## Baseline findings

QNAP's existing defaults prefetch one page ahead and none behind, with a 64 MiB
raw image LRU. The browser also preloads image URLs. ArchiveReader normally reads
ZIP members from a physical file, not a complete in-memory archive. Each page
navigation previously committed reading_progress to SQLite. Thus audible access
can be writes as well as reads; no NAS hardware trace was available for this work.
QNAP defaults to original image delivery and skips speculative image analysis.
Resized/converted images and analysis in other configurations still use disk
caches. Nested archives are initially extracted to disk. RAR external backends
still access disk when a page misses the existing raw-page cache.

## Changes

- Active ZIP/CBZ sources can load the compressed archive sequentially into RAM
  on open. The shared reader budget defaults to 256 MiB, is reserved before
  allocation, and is released on close or failed open. Directory listings do not
  fill it. A zero budget disables it. Existing sources are not forcibly evicted.
- Oversized archives, a budget occupied by other sessions, or allocation/read
  failures use the original file-backed reader. RAR is unchanged. Extracted child
  ZIPs can use the same budget, but their initial extraction still writes disk.
- Reading progress is visible in memory immediately and committed in batches
  every 5 seconds by default. The timer starts at the first pending update and
  is not postponed by later turns. Completion, server-side session close, clean
  shutdown, and folder-rating migration flush pending state. Failed flushes keep
  pending data and timer flushes retry. Setting 0 seconds restores immediate writes.
- Settings expose archive RAM (0–1024 MiB), save interval (0–30 seconds), and
  existing ahead/behind prefetch counts (0–12 / 0–4). Existing prefetch preferences
  and QNAP defaults are retained; ahead 4 / behind 2 is a reasonable starting point.

The archive limit counts compressed payload bytes across active readers, not
total process RSS: image LRU, decoded pixels, ZIP indexes, allocation overhead,
and videos use additional memory. Opening a book can take longer because its
compressed contents are loaded before the first page. Lowering the limit takes
effect for future allocations; existing buffers are released when readers close.
Do not promise zero HDD I/O: metadata, database, cache, and OS activity remain.
An abrupt power loss can lose the pending interval of reading position. Browser
tab close is not a guaranteed server-session close; the timed flush still runs.

## Verification

Synthetic archive tests read 20 pages forward and backward from the memory
buffer, verify shared capacity fallback and release, and check corrupt ZIP
cleanup. SQLite tracing confirms 20 pending updates become one commit, with
immediate single/bulk progress reads and immediate completed-state persistence.
Timer, close/reopen persistence and pending-state folder-rename tests also pass.
These are application-level checks, not physical HDD or QNAP performance results.
