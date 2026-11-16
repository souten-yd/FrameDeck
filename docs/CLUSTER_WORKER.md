# FrameDeck LAN Linux worker — Stage 1: authenticated pairing

Tracking: https://github.com/souten-yd/FrameDeck/issues/62

## Architecture and scope

- **A: QNAP TS-253Be** is the always-on owner of the video/comic originals,
  library metadata, ratings and reading/playback state.
- **B: Linux PC** is an optional, intermittently powered processing worker.
  Users always access A's URL, and A works normally if B is off.
- This PR implements **manual pairing, capability/availability checking,
  request signing and status UI only**. **Video transcoding, comic processing,
  media sharing, session failover, database synchronization, replication and
  backups have not been enabled by this PR.** The UI explicitly reflects that.
- Both hosts run the **same** FrameDeck codebase. Default is standalone mode.
  No LAN scanning, WOL, uploads, source sharing, automatic installs,
  remote execution, or scheduled backups are introduced.

## LAN auto-discovery (preferred)

QNAP no longer requires manually entering the Linux worker IP address or port.
B broadcasts a signed UDP beacon every eight seconds to UDP port 31964.
QNAP listens only if the administrator has enabled auto-discovery.
When B starts or changes LAN IP, QNAP performs an authenticated TCP check
and registers the current address automatically. Repeated messages from an
unchanged address do not write the QNAP configuration. A powered-off B does
not affect normal NAS playback.

**First-time trust enrollment remains mandatory.** Do not trust unknown LAN
devices merely because they are on the same local network.

1. Set the existing Web PIN on QNAP A. Open Settings >
   Linux高速変換サーバー > LAN自動検出を有効化.
2. The button shows a newly generated 64-hex-character secret **once**.
   Send it to Linux B using a protected admin connection, and set the service
   environment variable FRAMEDECK_CLUSTER_WORKER_SECRET to that value.
   Restart Linux FrameDeck after modifying the service environment.
   Do not commit the secret to Git, screenshots or browser storage.
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

1. **Processing offload**: choose NAS direct Range vs Linux conversion,
   safely provide read-only source data to the worker, handle backpressure,
   HLS/iPad audio, cancellation and encoder capability decisions.
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
