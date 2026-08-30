# HLS startup and settings version status

Date: 2026-08-30

## Publication

- Branch: `fix/hls-startup-version-settings`
- Remote: `origin/fix/hls-startup-version-settings`
- Base: `f390044` (`v2.3.0`)
- Status: pushed for review; not merged

## Changes

- `d5fc1a3 Reduce HLS startup segment latency`
  - Changes the default HLS segment duration from four seconds to two.
  - Migrates only the previous default, preserving explicit custom durations.
  - Forces keyframes at segment boundaries and invalidates the old HLS cache namespace.
- `0b49da8 Show app version at top of settings`
  - Shows the installed FrameDeck version at the top of Settings.
  - Keeps release lookup behind the explicit update-check action.

## Verification

- `node --check framedeck/web/static/js/updater.js`: PASS
- `node --check framedeck/web/static/js/app.js`: PASS
- Focused pytest selection: PASS, 54 tests
- Full pytest suite: PASS, 207 tests
- `git diff --check`: PASS

The pytest run reported 2,718 existing Pillow deprecation warnings from
`Image.Image.getdata`; no test failed.

## Not yet verified

- Real-media measurement of HLS time-to-first-frame after the two-second change.
- Visual browser acceptance of the Settings version card on desktop and mobile.
