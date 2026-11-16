# OFF-NAS verified FrameDeck backup copy

Tracking: https://github.com/souten-yd/FrameDeck/issues/62

This feature is an optional extra copy of **completed, verified FrameDeck SQLite
and private settings generations**. It is not a copy of source manga/video
files. Original data remains on QNAP A, and there is no remote delete or
restore API.

## Prerequisites: separate mounted Linux storage

The Linux processing worker B may be turned on intermittently. To use it as a
backup destination, provision an **independent backup volume on Linux B** and
export a restricted destination to QNAP A (NFS/SMB/SSHFS or another trusted OS
network filesystem). For actual resilience, use different physical drives
and keep a separate offline backup for NAS and Linux failures.

IMPORTANT: FrameDeck does **not** encrypt the copied backup bytes itself.
The generations contain settings, potentially including the Web PIN and
worker secret. Configure encryption both **on the transport** (e.g. SMB3
encryption, SSHFS or a trusted encrypted overlay) and **at rest on Linux**
(e.g. LUKS/cryptsetup or other encrypted volume) before enabling replication.
Never expose this share to unrelated LAN users or to the internet.

The OS mount must be a direct, existing mountpoint that reports a different
device from QNAP's FrameDeck data volume. A normal local directory, symlink,
missing path or same-device location is rejected. The application creates
a private 0700 FrameDeck-Metadata-Backups subdirectory and insists that
permissions are respected. Use least-privileged NAS credentials allowed to
write ONLY the backup destination, not the source video/SQLite directories.

## Enable and use

1. Provision and mount the external Linux encrypted backup volume on QNAP,
   e.g. /mnt/linux-framedeck-backup, using your NAS/Linux OS tools. **FrameDeck
   does not create mounts or run privileged shell setup.**
2. In QNAP FrameDeck Settings > データ保全・世代バックアップ, set the replica
   mount path to that exact absolute mountpoint and enable
   Linuxへの世代バックアップ複製.
3. Set the Web PIN before LAN administration. Run an ordinary snapshot first
   using 今すぐバックアップ and then click
   最新世代をLinux側へ複製・検証.
4. QNAP verifies the local SQLite snapshot checksum/SQLite integrity,
   copies source metadata in 1 MiB chunks to a private .incoming directory
   on the mounted Linux filesystem, fsyncs each file, rechecks the remote
   SQLite integrity and per-file SHA-256 against the source manifest, and
   atomically publishes the directory only on full success.
5. With replication enabled and the paired Linux worker online, a daemon
   checks worker state every 30 seconds (no disk probe while offline),
   schedules a replica scan when B becomes available and no more often
   than every 30 minutes afterwards. Only one new completed generation is
   copied per scan to limit QNAP HDD/USB/network I/O.

The ordinary local SQLite daily 7 + weekly 4 retention still applies.
This separate, opt-in destination **does not delete completed remote
generations**. This is deliberate: a damaged QNAP backup must not cause
older Linux copies to be deleted. Plan Linux storage space and offline
rotation yourself; a full volume results in a safe failure, not overwrite.

## Constraints and failure recovery

- Linux B must have the destination writable and mounted from A. If B is
  off, automatic replication skips work; local backups remain available.
- A remote filesystem mount can block during a network power outage even
  when one copy is in progress. Replication runs in a dedicated background
  daemon thread; prefer a network mount with appropriate OS timeouts.
- Copied generations are private and sealed with the source's manifest.
  Corrupted or differing existing remote generations are preserved for
  manual diagnosis: FrameDeck will not delete or overwrite them.
- If the QNAP device fails completely, its own Web UI cannot automatically
  restore. An administrator may copy a verified Linux generation back
  while FrameDeck is **stopped**, after preserving the current DB/WAL and
  checking schema compatibility. Use docs/DATA_BACKUPS.md offline
  restoration procedure. No automated restore API is introduced.
- Neither NFS/SMB nor an OS filesystem mount automatically proves
  encryption or a different physical machine. The administrator must
  validate the remote path, volume encryption, transport, access controls
  and failure domain before enabling this option.

## Tests and validation

Run tests/test_backup_replica.py and all existing pytest tests. Validate
permissions, nonmount rejection, source-corruption rejection, interruption
before publication, source/destination checksums, SQLite integrity and
preservation of older generations. Validate real QNAP TS-253Be and Linux
encrypted mount behavior, network disconnect and write/seek performance
before relying on this as a disaster-recovery copy.
