# FrameDeck metadata backup and recovery (Stage 3)

Tracking: https://github.com/souten-yd/FrameDeck/issues/62

## What is protected

This opt-in feature creates **online SQLite snapshots** without copying a
live .db file or WAL/SHM files as raw bytes. The snapshot includes:

- framedeck.db: reading/video progress, favorites, ratings, library metadata
  and other SQLite-managed state
- settings.json: the FrameDeck configuration, potentially including Web PIN
- cluster-worker.json, **only if present**: the private Linux-worker pairing
  secret and URL

The **original comics, movies, archives, downloads, cached transcodes,
HLS segments and other media files are NOT backed up**.

The backup folder is inside the persistent FrameDeck data directory:

    /share/<volume>/.framedeck/backups/        (QNAP)
    <FRAMEDECK_HOME>/backups/                  (Linux)

Each completed generation is an immutable-style named directory containing
the database, configuration and manifest.json with SHA-256 checksums,
creation timestamp, local day and ISO week.

## Safety properties

- Snapshot taken with Python sqlite3.Connection.backup: it is consistent
  with concurrent SQLite WAL writes. In-memory pending comic progress is
  flushed before the snapshot.
- The SQLite backup inherits the source's WAL journal mode, so the sealed
  **copy** is explicitly checkpointed and changed to DELETE journal mode.
  The finished generation must contain only one framedeck.db and must not
  contain any -wal/-shm sidecars.
- SQLite PRAGMA integrity_check runs on the new copy, not the live database.
  Checksums, sizes and required filenames are then verified before publish.
  Unexpected files or broken generations are rejected, and older verified
  generations are kept even if a newer one becomes corrupted.
- Temporary directories are owner-only (0700), files owner-only (0600).
  Files and manifests are fsynced, then the directory is atomically renamed.
  The parent directory is fsynced where the filesystem supports it.
- If free disk space is too low or verification fails, the incomplete new
  copy is discarded and older completed generations are left untouched.
- Retention maintains the newest **7 daily dates + 4 distinct ISO weeks**
  (overlapping generations counted once), and **always keeps at least the
  two most recent verified snapshots**, even if created on the same day.
  This can retain one extra generation to protect the previous known-good
  recovery point. There can be fewer generations until sufficient dates pass.
- Daily schedule: when enabled, the NAS checks after local 03:00 and makes
  at most one automatic attempt per local calendar day. It does not retry a
  failing backup every 10 minutes and constantly write to the NAS.
- Any filesystem source symlink or backup directory symlink is rejected.
  There is **no remote restore, delete or arbitrary backup-path endpoint**.
- Runtime behavior, video playback, HLS, iPad audio and original library
  file storage are not modified.

## Enable

Backup is initially **disabled** to avoid surprise HDD activity on QNAP.
In FrameDeck > Settings > データ保全・世代バックアップ:

1. Turn on SQLiteの自動バックアップ for daily 03:00+ generation rotation.
2. Click 今すぐバックアップ to create an initial tested snapshot.
3. Click 最新世代を整合性検証 to recheck the latest generation.
4. The status displays the retained generation count and last completed time.

For remote backup administration through the web UI, protect FrameDeck with
the existing Web PIN first. The app never returns backed-up secrets or
absolute backup filesystem paths in the status API.

## Restore policy: deliberately OFFLINE and explicit

There is no automatic rollback and no live-restore button. Restoring a
previous backup overwrites more recent progress and may mismatch a newer
FrameDeck schema. Only a verified backup matching the intended software
version should be restored. Administrator procedure:

1. Stop FrameDeck completely on QNAP (QTS App Center) or Linux.
2. Copy the **entire current persistent FrameDeck data/config directory**
   to a separate safety location before touching it.
3. Select a known generation and verify the checksum manifest and SQLite
   integrity_check. This can be done through the API before stopping.
4. With FrameDeck stopped, preserve the current framedeck.db, -wal and -shm
   together in a quarantine directory. Do not copy a backup database on top
   of an active WAL/SHM pair.
5. Copy the verified snapshot's framedeck.db to its original data directory,
   and the saved settings.json (and optional cluster-worker.json) to the
   config directory. Preserve file ownership and private permissions.
6. Start the matching FrameDeck version. Verify library roots, progress and
   ratings before deleting the quarantined previous state.

This procedure is intentionally not automated in this PR. A future restore
tool should implement a dry-run, pre-restore snapshot and explicit selection.

## Important limitations / next work

- Same-NAS backups protect against application/schema mistakes, bad writes
  and some corruption, **not** QNAP hardware, volume, fire or ransomware
  loss. Copies to separate offline/remote storage are still necessary.
- The snapshots contain Web PIN and possibly the worker pairing secret. Do
  not expose backup shares over untrusted LAN or upload them unencrypted.
- There is no incremental revision/event synchronization between QNAP A and
  Linux B yet. The NAS remains authoritative; the worker is a processing
  node, not a second database writer.
- Applying updates through FrameDeck's own Web UI now creates and verifies
  an online SQLite/settings snapshot BEFORE launching the updater, even when
  the scheduled daily-backup toggle is OFF. Failure blocks the update.
  This does NOT apply to a manual QTS App Center QPKG installation or to
  arbitrary schema migrations started outside the Web updater; create a
  manual snapshot before either of those operations.
- Hardware verification, low-disk failure, power-cut injection and QNAP
  disk-I/O measurements remain required before merging and releasing.
  Python unit tests alone cannot prove crash durability on every filesystem.

## Developer verification

Run tests/test_backups.py and all existing pytest suites. Verify:

- Snapshot from a WAL database while progress writes are active.
- Completed snapshot hashes, integrity_check, private permissions.
- Corrupted files and malformed manifest rejection.
- Free-space failures preserve older snapshots.
- Retention of 7 daily and 4 weekly generations.
- Web PIN / same-origin protection for backup mutation.
- No direct live DB replacement, no original media modifications.

Do not claim these tests have passed until they have actually run.
