# FrameDeck LAN Linux worker — registered worker, fMP4 and optional HLS

Tracking: https://github.com/souten-yd/FrameDeck/issues/62

## Architecture and scope

- **A: QNAP TS-253Be** is the always-on owner of the video/comic originals,
  library metadata, ratings and reading/playback state.
- **B: Linux PC** is an optional, intermittently powered processing worker.
  Users always access A's URL, and A works normally if B is off.
- Stage 1 implements registration, signed capability checks and health monitoring.
- Stage 2 adds **opt-in Linux fMP4 transcoding** from an explicitly mapped,
  read-only NAS share. A proxies B's encoded stream, falling back to QNAP.
  HLS/iPad worker offload is added in the next stage (see CLUSTER_HLS.md).
  SQLite backups are implemented in docs/DATA_BACKUPS.md; comic offload,
  metadata replication, off-NAS backup copy and HLS on-device performance tests remain future work.
- Both hosts run the **same** FrameDeck codebase. Default is standalone mode.
  No LAN scanning, WOL, uploads, raw source sharing, or automatic installs
  are introduced. Optional LAN discovery uses authenticated UDP beacons.
  SQLite backups are independently opt-in and OFF by default.

## Recommended: browser-only pairing (no user-managed keys)

Follow [KEYLESS_LAN_PAIRING.md](KEYLESS_LAN_PAIRING.md).
Open FrameDeck in both Linux and QNAP browsers, start a 2-minute pairing
window, compare the same 6-digit verification code and approve at both ends.
Both servers derive and persist the shared HMAC key internally using ephemeral
X25519/HKDF. Users never need to see, enter or copy the worker key, edit
environment variables, run QNAP SSH or manually register an IP address.
Subsequent authenticated UDP discovery/reconnect is automatic.

The manual workflow below remains only for legacy installations that
explicitly configure FRAMEDECK_CLUSTER_WORKER_SECRET. It is not the
recommended first-time onboarding process.

## Legacy: manual LAN discovery and shared-secret enrollment

QNAP no longer requires manually entering the Linux worker IP address or port.
B broadcasts a signed UDP beacon every eight seconds to UDP port 31964.
QNAP listens only if the administrator has enabled auto-discovery.
When B starts or changes LAN IP, QNAP performs an authenticated TCP check
and registers the current address automatically. Repeated messages from an
unchanged address do not write the QNAP configuration. A powered-off B does
not affect normal NAS playback.

**One-tap QNAP setup and mandatory initial Linux trust enrollment:**
Do not trust unknown LAN devices merely because they are on the same network.

1. On QNAP A, open Settings > Linux高速変換サーバー and press
   **LANワーカーをワンタップ登録**. If no Web PIN exists, FrameDeck creates
   a cryptographically random PIN, logs this browser in using an HttpOnly
   SameSite cookie and shows the new PIN **once**. Save it securely: other
   browsers will need this PIN. Never enter it over an untrusted plaintext
   connection; use trusted HTTPS when possible.
2. The same button enables signed LAN auto-discovery. It displays a new
   **64-hex-character worker secret once**. This must be installed on
   **Linux B on the first enrollment** via a protected administrator
   connection as FRAMEDECK_CLUSTER_WORKER_SECRET, and Linux FrameDeck must
   be restarted. This step cannot be skipped securely: without any existing
   identity/key, there is no way for QNAP to prove an unknown LAN device
   is the owner's Linux workstation.
3. Thereafter the *same QNAP button* reconnects/rechecks the worker, and
   its IP changes and future boot announcements are automatic with NO
   further SSH/password/secret copying. The UI watches for signed workers
   for at most about 70 seconds, without repeatedly writing QNAP config.
   Old manual IP/secret enrollment remains available.
3. If B uses a port other than 9000, set its service environment variable
   FRAMEDECK_CLUSTER_WORKER_PORT to the actual TCP port. Allow outbound
   UDP broadcast to 255.255.255.255:31964 from Linux and inbound UDP 31964
   from the trusted LAN to QNAP, plus QNAP-to-Linux access to the TCP port.
   Do not create internet router port forwards.
4. A accepts only signed, recent beacons from private RFC1918 IPv4
   addresses. It also asks the announced TCP endpoint to prove possession
   of the same secret using a signed capability response. No unauthenticated
   address is saved, even if an attacker replays a captured UDP beacon.
5. For VLANs, guest Wi-Fi isolation or Tailscale-only paths that do not
   carry LAN broadcast, use the existing manual IP + secret form.

Discovery does not perform LAN scanning, command execution, file upload,
automatic media copying or arbitrary URL fetching. Initial browser-to-NAS
key transfer is sensitive on plaintext HTTP; use properly validated HTTPS
or a local protected administrative channel where possible. Disabling
auto-discovery stops new address registration but retains a previously
paired worker until explicitly unpaired.

## Legacy manual first-time configuration (not required for Web pairing)

1. Install the same FrameDeck version on A and B. B must have working FFmpeg
   for fMP4/HLS processing. Offload is OFF until explicitly enabled.
2. On Linux B generate a **64-hex-character shared secret**:

       python3 -c 'import secrets; print(secrets.token_hex(32))'

   Store it safely. Start Linux FrameDeck with the environment variable
   FRAMEDECK_CLUSTER_WORKER_SECRET set to that value, alongside its normal
   startup variables. Do **not** put the value in Git, logs, browser bookmarks,
   or process command-line arguments. Configure its service manager so it
   persists across restarts. The authenticated worker endpoint is closed if
   this variable is absent or invalid.
3. On QNAP A, the one-tap button can provision the initial Web PIN and
   start the browser session; LAN-based manual registration remains blocked
   if Web PIN is empty. The button does not authorize an untrusted worker
   or remove the initial Linux-side key enrollment requirement.
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
   normal files can be read and that a test write fails. The worker checks
   the Linux mount's OS-level read-only flag; writable mounts are rejected
   even if directory permissions appear restrictive.
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
   A short pause or buffering interval is allowed. Retry requests may set
   force_local=1 on A's fMP4 route, preventing a repeated attempt to use
   an unreliable worker for that playback. The QNAP CPU/resolution limits
   still apply whenever QNAP performs the actual conversion.

### Limits and data safety

- Stage 2 delegates only fMP4; **the later HLS stage** also delegates
  iOS/iPad native HLS including `original-aac` when enabled, healthy and
  source is mapped. See CLUSTER_HLS.md. Direct HTTP Range stays on A.
- HLS stage measures end-to-end segment arrival against segment playback
  demand and cools down slow workers for new sessions, but this is not an
  accurate ffmpeg FPS benchmark or a full A-vs-B CPU cost model. Running
  sessions are not moved just because the measured rate changes.
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
- QNAP metadata backup snapshots (7 daily + 4 weekly and two latest
  verified copies) are implemented separately. Revision-based cross-node
  state synchronization and off-NAS backup replication are not yet present.
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

1. **Complete processing offload**: HLS/iPad AAC worker delegation and
   segment-arrival speed monitoring are implemented. Still needed: hardware
   encode-FPS benchmarks, CPU/network cost comparison, and device validation.
2. **Automatic failback**: recover worker interruption on A with the
   video position or comic page retained as far as possible. A short pause
   is acceptable. B cannot read originals that exist only on a dead A.
3. **State and safety**: local A-authoritative SQLite online generations
   are implemented. Cross-node revisioned, idempotent update replay, offline
   event queue and conflict detection remain future work.
4. **Optional replication**: only if explicitly enabled, limited in space
   and bandwidth, verified before publishing files, with deletion safety.
   A outage recovery for new browser sessions needs a separate entrypoint.

## Verification

Run the new tests/test_cluster_worker.py and existing API/video/comic tests
before merging. These tests cover signatures, replay, response authentication,
secrets, unsafe URL rejection and standalone regression.

Do not claim that actual B conversion, backup or cluster failover is working
until those distinct PRs have been implemented and tested on devices.
