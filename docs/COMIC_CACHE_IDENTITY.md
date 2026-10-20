# Comic page cache identity

v2.4.15 keeps ZIP snapshots in memory. The existing raw image and dimension
cache keys only used entry ID and page index, so two sources for the same path
could share bytes even when their snapshots or page lists differed. A regression
test reproduces this with an old open ZIP and a rewritten/reopened ZIP: main
returns the old image for the new source, while this change returns the new one.
This proves a stale-image path; it does not establish that the user's NAS file
was modified or that every reported jump had this cause.

- Physical archive and image-folder sources have a unique cache identity.
  Raw bytes, prefetch keys and dimension lookups include it plus the member name.
  The existing bounded raw LRU still limits image memory.
- Disk image/analysis keys include member names and use a new key version, so
  index-only legacy files are not reused. Variant keys also include member names.
- Each reader entry opening returns a new content_id. Original and converted
  browser image URLs and preload groups include it. The server validates supplied
  content IDs under the session lock and rejects requests for a superseded source.
  Legacy clients without the field remain compatible. Page turns within the same
  opening keep the identity, retaining prefetch/browser-cache benefits.
- View-option PATCH requests now use the same bounded transport/epoch checks as
  page navigation, preventing delayed option responses from restoring an old
  session or overlapping a page turn.

ZIP RAM limits, batched progress saving and stored read/completed state are
unchanged. Tests cover old/new snapshots, member-name changes, stale generation
requests, browser URL separation and delayed option responses. QNAP/iOS hardware
reproduction remains unverified; a page-number jump versus only a wrong image is
a useful distinction if the symptom persists.
