# FrameDeck LAN Linux worker — Stage 1: authenticated pairing

Tracking: https://github.com/souten-yd/FrameDeck/issues/62

## Architecture and scope

- **A: QNAP TS-253Be** is the always-on owner of the video/comic originals,
  library metadata, ratings and reading/playback state.
- **B: Linux PC** is an optional, intermittently powered processing worker.
  Users always access A's URL, and A works normally if B is off.
- Stage 1 implements registration, signed capability checks and health monitoring.
- Stage 2 adds **opt-in Linux fMP4 transcoding** from an explicitly mapped,
  read-only NAS share. A proxies B's encoded stream, falling back to QNAP.
  HLS/iPad, comic offload, metadata synchronization and backups remain future work.
- Both hosts run the **same** FrameDeck codebase. Default is standalone mode.
  No LAN scanning, WOL, uploads, source sharing, automatic installs,
  remote execution, or scheduled backups are introduced.

## First-time configuration

1. Install the same FrameDeck version on A and B. B should have a working
   local FFmpeg binary; remote transcoding is not yet used.
2. On Linux B generate a **64-hex-character shared secret**:

       python3 -c 'import secrets; print(secrets.token_hex(32))'

   Store it safely. Start Linux FrameDeck with the environment variable
   FRAMEDECK_CLUSTER_WORKER_SECRET set to that value, alongside its normal
   startup variables. Do **not** put the value in Git, logs, browser bookmarks,
   or process command-line arguments. Configure its service manager so it
   persists across restarts. The authenticated worker endpoint is closed if
   this variable is absent or invalid.
3. For remote configuration from a phone/PC, first enable FrameDeck's existing
   Web PIN protection on QNAP A. A loopback request on A can register a worker
   without a PIN, but LAN-based registration is rejected while Web PIN is empty.
4. Open A's FrameDeck > Settings > Linux高速変換サーバー（準備段階）.
   Enter B's literal LAN IP + port, such as http://192.168.1.20:9000, and
   the shared secret generated on B. Choose ワーカーを登録・更新.
   A saves the peer config atomically in its persistent configuration
   directory with owner-only permissions (0600). The secret is not included
   in the status or normal settings API.
5. A polls the worker's authenticated capability endpoint roughly every
   10 seconds. The status becomes 接続済み when the signature and protocol
   match and オフライン when unreachable. B may power down at any time
   without interrupting media playback from A.

When the B-side secret is rotated, update it on A too. Removing the pairing
from A deletes its saved config but does not rotate the secret on B.

## Stage 2: Linux fMP4 transcoding (opt-in)

FrameDeck A is always the QNAP data authority and browser entrypoint.
Linux B is intermittently powered and reads QNAP files through a separate,
**read-only** NFS/SMB mount. No media uploads, automatic share mounts, shell
operations, or second metadata writer are introduced.

1. Identify the exact QNAP absolute path of each video library root.
   An administrator can inspect the library_roots table in the persistent
   framedeck.db, under /share/<volume>/.framedeck/data on QNAP.
2. Mount each corresponding QNAP share on Linux B, e.g. /mnt/qnap-video.
   Use a dedicated account without write/delete permission. Verify that
   normal files can be read and that a test write fails.
3. Configure Linux B with both service environment variables. The secret is
   the same 64-hex-value used during Stage 1 pairing, and the map key is the
   **exact** registered QNAP library root. Example:

       FRAMEDECK_CLUSTER_WORKER_SECRET=<secret>
       FRAMEDECK_CLUSTER_WORKER_MOUNTS_JSON='{"/share/Download/Video":"/mnt/qnap-video"}'

   Multiple roots can be included as keys in this JSON mapping. Only paths
   under an explicitly mapped mount can be encoded. Symlink escapes, '..',
   absolute relative paths and non-video extensions are rejected. Never store
   a plaintext SMB password in an environment variable or a command argument.
4. On QNAP A, open Settings > Linux高速変換サーバー and register B with its
   LAN IP + port and the matching secret. Confirm status is connected.
   Turn on **Linux高速変換を使用**. The default is OFF. Alternatively, an
   administrator may opt in with FRAMEDECK_CLUSTER_ENABLE_OFFLOAD=1 in A's
   service environment, but UI opt-in is preferred.
5. Start a video requiring fMP4 conversion on a desktop browser. The browser
   continues using A's /api/videos/{id}/stream-transcode URL. A checks B's
   signed health, FFmpeg availability and mapped source root, sends an
   authenticated conversion request to B and streams the **encoded** output
   to the browser in bounded chunks. A's response includes the diagnostic
   header X-FrameDeck-Processing-Node: linux-worker.
6. Stop B during playback. The A-side worker connection fails and is placed
   into a short cooldown; existing browser recovery retries the request from
   the most recently known video position and routes it to A's local FFmpeg.
   A short pause or buffering interval is allowed. The QNAP CPU/resolution
   limits still apply whenever QNAP performs the actual conversion.

### Limits and data safety

- Only fMP4 conversion is delegated in Stage 2. Browser-compatible original
  files still use A's direct HTTP Range path. iOS/iPad native HLS, AAC audio
  conversion, comic pages and HLS segment serving remain on A.
- Real encode-FPS monitoring and continuous best-node scoring are **not yet
  implemented**. Stage 2 prefers B if healthy, mapped and enabled. A stable
  in-progress playback is not moved to B when it comes online.
- HMAC-SHA256 authenticates each worker request and the response handshake,
  with bounded nonce/replay protection. This does **not** provide per-byte
  end-to-end content integrity or confidentiality on plaintext HTTP.
  Use correctly validated HTTPS when the LAN cannot be trusted. Initial
  browser-to-A registration also transmits the secret in JSON.
- B-side concurrent fMP4 sessions are capped at 2. Source access is limited
  to explicitly mapped file roots; there are no arbitrary URL fetches,
  execution APIs, write/delete endpoints or generic filesystem browsing.
- Neither B nor its filesystem mount may modify A's original files or SQLite.
  Persistent playback state still belongs to A. If A loses power, B cannot
  access any originals that existed **only** on A.
- Backup and metadata synchronization are not present in Stage 2. Follow-up
  work must provide SQLite online snapshots, integrity checks, safe restore,
  and initially **7 daily + 4 weekly generations** before claiming data
  corruption protection.
- Do not merge/release Stage 2 until the new worker tests and existing API,
  direct Range, HLS, iPad AAC and QNAP constraints pass. Real QNAP/Linux/iPad
  testing remains separate from local synthetic tests.

## Security / implementation notes

- Only explicit literal LAN private IPv4, 100.64/10 (Tailscale/CGNAT) or IPv6
  ULA addresses are allowed. Public IPs, hostnames, localhost, link-local,
  credentials, URL paths and redirects are rejected. No proxy or DNS
  lookups are used. Tailnet ACLs should restrict access as well.
- Requests use HMAC-SHA256 over method, path, timestamp, nonce and payload
  digest. Validation uses constant-time comparisons, a 30-second clock
  window and a bounded one-use nonce cache.
- Responses are separately signed and bound to the request's nonce.
- HTTP is **not encrypted**. **During initial pairing, the browser sends the
  shared secret to A in a JSON request**; anyone able to observe that HTTP
  connection could capture it. Worker heartbeats send only HMAC signatures,
  not the raw secret. Prefer properly validated HTTPS for the browser-to-A
  pairing step, or configure the owner-only file locally on the NAS using a
  protected administrative channel. A trusted LAN/Tailscale reduces exposure
  but does not encrypt plain HTTP by itself. Unverified TLS certificates are
  not accepted for worker polling.
- Credentials are not logged, and the status API never emits secrets.
  Reject unexpected protocol versions or malformed/unsigned responses.
- The existing original Range streaming, HLS, QNAP CPU constraints and
  iPad AAC behavior remain unchanged.

## Subsequent PRs required to deliver the approved feature

1. **Complete processing offload**: Stage 2 adds opt-in fMP4 only. Add
   HLS/iPad audio offload, actual speed measurement, and better node scoring.
2. **Automatic failback**: recover worker interruption on A with the
   video position or comic page retained as far as possible. A short pause
   is acceptable. B cannot read originals that exist only on a dead A.
3. **State and safety**: authoritative A metadata, revisioned idempotent
   updates, conflict handling, bounded write buffering, corruption-resistant
   SQLite online snapshots and recovery. Proposed initial retention is
   **7 daily + 4 weekly** backups, verified before considered usable.
4. **Optional replication**: only if explicitly enabled, limited in space
   and bandwidth, verified before publishing files, with deletion safety.
   A outage recovery for new browser sessions needs a separate entrypoint.

## Verification

Run the new tests/test_cluster_worker.py and existing API/video/comic tests
before merging. These tests cover signatures, replay, response authentication,
secrets, unsafe URL rejection and standalone regression.

Do not claim that actual B conversion, backup or cluster failover is working
until those distinct PRs have been implemented and tested on devices.
