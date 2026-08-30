# Comic volume reading progress status

Date: 2026-08-30

## Publication

- Branch: `feat/volume-reading-progress`
- Remote: `origin/feat/volume-reading-progress`
- Base: `f390044` (`v2.3.0`)
- Feature commit: `a09f046 Show reading progress in comic volume lists`
- Status: pushed for review; not merged

## Behavior

- Volume rows show `未読`, `読書中`, or `読了`.
- Saved progress shows a percentage, known page count, and last-read date.
- The last-read date is hidden for the mobile UI profile and at widths up to
  760 px; status, percentage, and page count remain visible.
- A physical item containing multiple nested comics reports an overall
  percentage only when all nested page counts are known.
- Unopened comics remain honest: their page count is unknown until FrameDeck
  has opened the readable entry and saved progress.

## Data and performance

- Reuses the existing `reading_progress` SQLite records; no schema migration is
  required.
- Reading records are fetched in batches of at most 500 entry ids.
- Normal archive entry ids are derived without reopening each archive.
- Nested archives are discovered for recently opened physical items only.
- On a real 15-volume folder with saved progress, the cold progress API path
  improved from about 3.2 seconds during development to about 0.11 seconds
  after avoiding redundant archive discovery.

## Verification

- `node --check framedeck/web/static/js/volume_view.js`: PASS
- Focused storage, volume, and API tests: PASS, 46 tests
- Full pytest suite: PASS, 210 tests
- `git diff --check`: PASS
- Real library API: PASS; reading, completed, percentages, page counts, and
  timestamps matched existing saved progress.
- Headless Chrome desktop at 1280 x 800: PASS; no horizontal row overflow.
- Headless Chrome mobile at 390 x 844: PASS; no horizontal drawer overflow and
  the last-read date computed to `display: none`.

The pytest run reported 2,718 existing Pillow deprecation warnings from
`Image.Image.getdata`; no test failed.

## Known limitation

Legacy progress for a nested-only archive that has fallen outside the most
recent 100 physical items cannot be mapped back to its parent without scanning
the archive. It is shown as unread until that physical item is opened again.
