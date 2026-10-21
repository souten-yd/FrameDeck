# Comic navigation stalls

Investigation against v2.4.16/main found avoidable work, but has not reproduced
the reported timeout on QNAP/iOS hardware. These paths predate v2.4.16; the
content identity guard itself is retained, not identified as the cause.

## Changes

- Every navigation response resolves root_folder_id. For a nested folder this
  previously statted and upserted the parent, committing SQLite on each turn
  despite batched reading progress. It also replaced the stored rating with
  None. Look up the existing parent by absolute path and return its stored ID
  without filesystem metadata access or a write. Register an absent parent once.
  Library scanning remains responsible for refreshing existing folder metadata.
- When the prefetch window changes, keep an in-flight image if it is still
  visible or in the desired window. Previously a still-needed nonvisible image
  was cancelled and restarted. Cancelling browser work need not stop server
  decompression/conversion, so repeated requests can waste work.
- Bound each speculative image load/decode to 12 seconds. A missing event or
  stalled decode releases that slot and continues with the next queued image.
  Clear its timer and handlers on completion/reset, and reject late callbacks.
  Failed images are not retained and can be retried on a later window update.

Page-turn timeout and mutation replay behavior, page grouping, content identity,
archive RAM limits, single-image prefetch concurrency and batched progress writes
are unchanged. The watchdog bounds speculative work, not visible-page loading.

## Validation

Three new behavioral tests fail on v2.4.16 and pass after this change. They cover
100 parent lookups without filesystem calls/SQL writes and with rating retained,
one-time registration of a missing parent, retained in-flight image work, stalled
load recovery, late callbacks and cleanup on switching content.

130 related tests pass, including API, navigation, JavaScript transport,
prefetch, cache identity, low-I/O, split pages, storage and folder ratings.
This is not a measured NAS latency improvement. If timeouts persist, distinguish
a page-turn API timeout toast from an image-loading error, and record delivery
mode, archive format/size and whether the issue occurs at a volume boundary.
