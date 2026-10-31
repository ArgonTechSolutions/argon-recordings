# Remote worker installations

The optional split deployment runs the web app, accounts, recordings and search on a
coordinator, and exactly one Turbo CPU INT8 / E5 worker on another Linux host. Existing
installations default to `deployment.mode = "local"`; updating source does not move
recordings, models or change deployment mode.

For Argon's planned installation, Vesta is the coordinator and Philemon initiates
connections to Vesta over Tailscale. This document describes supported configuration;
it does not authorize or perform a production migration, network-policy change or
certificate provisioning.

## Configuration

Merge these sections into the existing configuration, retaining existing app,
authentication and model settings. Do not replace an existing configuration wholesale.

Coordinator example (Vesta):

```toml
[deployment]
mode = "coordinator"

[transport]
bind_address = "100.70.97.127"
port = 8788
certificate = "tls/vesta.crt"
private_key = "tls/vesta.key"
peer_certificate = "tls/philemon.crt"

[workers]
count = 1
chunk_seconds = 180
overlap_seconds = 2
```

Overlap is in seconds on each side of a chunk.

Worker example (Philemon):

```toml
[deployment]
mode = "worker"

[transport]
origin = "https://100.70.97.127:8788"
certificate = "tls/philemon.crt"
private_key = "tls/philemon.key"
peer_certificate = "tls/vesta.crt"

[resources]
cpus = 2.5
memory_gib = 6
job_timeout_seconds = 0

[workers]
count = 1

[models]
transcription = "turbo"
embedding = "large"  # Must match the coordinator profile.
# Retain the current turbo_path if configured; otherwise models/turbo is reused.
```

The transport reserves 0.1 CPU and 128 MiB on each host. Philemon's remaining budget
goes to one worker: Turbo uses two CPU threads and E5 has a separate single-thread
inference lane for interactive queries. Both remain inside the same container CPU/RAM
limits. Coordinator CPU/RAM includes its transport. E5 model preparation and inference
run on the worker; the coordinator keeps the SQLite index and performs ranking and
project authorization. Neither inference model is mounted on the coordinator. The
worker never mounts the recordings database. An existing installation's directories and named data/work
volumes are preserved, including when the mode changes. Account commands are
available only on local/coordinator installations.

## Transport identity and network boundary

Supply an independently generated, dedicated self-signed leaf certificate and
private key on each host. Each certificate must have BasicConstraints CA:FALSE and
an ExtendedKeyUsage of serverAuth (coordinator) or clientAuth (worker). The
coordinator certificate's SAN must match the configured HTTPS origin, for example
IP:100.70.97.127. Use an appropriate validity period and track expiry. Each peer
trusts exactly the other host's public certificate; this is not a shared CA setup.

The documented `tls/` directory is excluded from Git and Docker build contexts.
Only exchange `.crt` files. Keep each private key on its own host, owned by the
launcher user with mode 0600. File paths are relative to the config directory (or
absolute). Keys are never placed in Docker environment variables, images, logs or
the recordings volume. The transport mounts the three existing host files read-only
and loads them on every Docker start, including after reboot. Only this transport
runs as the launcher UID (container UID 0 for rootless Docker), with group 10001 to
read the coordinator’s RAM-only bearer. It retains dropped capabilities and a read-only
root filesystem. API and inference processes keep UID 10001. No extra persistent
copy of any key is created. Certificates and keys must be provisioned separately;
installation does not generate them. Recreate the transport with stop/start after
replacing TLS files, especially when replacing a file atomically.

The coordinator transport requires TLS 1.3 and the pinned worker certificate. It
forwards only worker API requests to the private coordinator interface and injects
the API's temporary in-memory bearer credential. The bearer is reread per request,
so API restarts rotate it without reprovisioning Philemon. The media worker has an
internal Docker network and only its work volume and read-only Turbo/E5 model mounts;
it cannot directly access the internet, accounts, private keys or the data volume.
A separate constrained transport streams requests to the fixed coordinator origin.

Permit only Philemon → Vesta TCP 8788 in the Tailscale policy. Do not expose this
port through the public reverse proxy. Browser access remains on localhost:8787;
public HTTPS and trusted proxy routing are a separate deployment step. Linux Docker
28+ and cgroup v2 remain prerequisites. The transport supports both length-delimited
and chunked streaming, with a 4 GiB request ceiling; raising the app upload setting
above that does not raise the worker transport ceiling.

## Lifecycle and recovery

Use the existing launcher commands from each installation directory:

```sh
python3 recordings.py doctor
python3 recordings.py install
python3 recordings.py start
python3 recordings.py status
python3 recordings.py logs
python3 recordings.py stop
```

Stop an installation before changing its mode or rebuilding. `install` builds the
same package and prepares only the models needed by that host. `start` validates
certificate files and starts the role's two services. A worker can start while its
coordinator is offline and retries the connection automatically. Completed chunks
and lease fencing use the existing durable coordinator state; transport disconnects
do not erase completed chunks. Keep the same coordinator database and Philemon work
volume to retain that state.

Host TLS material survives reboot in the existing owner-managed files. Docker
restarts reload those read-only mounts and reconnect automatically once the
coordinator is reachable. A host reboot still needs a live verification of Docker
and Tailscale startup; no service configuration is changed by this update.

Before moving Argon's existing instance: stop ingestion and workers, create and
verify a backup, migrate the complete coordinator data including accounts/MFA to
Vesta, and verify it before opening traffic. Keep Philemon's existing installation
path and model/work volumes. The launcher deliberately does not copy or adopt data
between hosts. Starting Vesta with an empty data volume is not a migration. A later
runbook must identify the actual source/destination volume names and rollback copy;
no production volumes should be removed during this transition.


## Updating an existing split installation

Keep the installation directories, TLS paths, named volumes and `turbo_path` unchanged.
On both hosts set `[models] embedding` to the same curated profile (`large` for the
existing Argon deployment). An older worker configuration may say `off`; change it
before updating. Coordinator mode retains its old model directory but no longer reads
or modifies it. Worker `install` verifies/reuses existing `models/e5-large` weights;
only missing or mismatched artifacts are downloaded. Public model files are normalized
to 0644 and their model directories to 0755 so the unprivileged worker can read them.
No recording, index, account, work volume or existing model directory is deleted.

Stop and update the worker first, then update the coordinator. Use `recordings.py update`
on each Git installation, or fetch/fast-forward and run `install` followed by `start`.
Both services must use the new protocol before semantic search is available. A missing
or mismatched embedding worker fails promptly; it does not silently replace semantic
search with keyword results. Keyword search remains available. Existing indexes retain
the same model identity, chunking and vector format, so an update does not require
reindexing or retranscribing recordings.

The worker pulls bounded, short-lived inference requests through its existing mTLS
connection; it opens no inbound port. Text/token windows and batches of at most four
embeddings travel over that connection. The coordinator holds at most 16 pending
requests with 120-second deadlines, and allows four active searches across browser and
agent APIs. Queries take priority over queued indexing batches; current inference is
not interrupted. Background refresh skips a busy index lock; a later search rebuilds
stale indexes. Query vectors have a 64-entry memory cache. Temporary requests disappear
on restart and are rebuilt on the next search; durable SQLite indexes stay intact.

Docker restart policies reload host TLS files without the launcher. The API regenerates
its bearer in RAM and the transport reads the current bearer on every request. The
coordinator alone permits group-read access to that RAM credential; local installations
retain owner-only permissions. Old RAM TLS volumes are left untouched and unused.
This does not configure Docker/Tailscale service boot ordering or renew certificates:
keep Docker enabled and monitor certificate expiry. Philemon retries Vesta while its
network is unavailable. Validate an actual host reboot separately after updating.


## Internal service discovery and IP reassignment

The private Docker network owns an `api` service alias. On the coordinator it names
its API; on the worker host it names the outbound TLS transport. Consumers resolve
that alias through Docker's embedded DNS, with no saved IP in `ExtraHosts`. Local
installations use the same discovery path. Existing installation/network identities,
volumes, model paths, TLS files, and remote worker count remain unchanged.

Inference workers and the coordinator transport retain `--dns 127.0.0.1`: embedded
Docker service names resolve, while ordinary external DNS forwarding is blocked.
The worker transport retains normal resolution for its fixed HTTPS origin. This
change does not open an inbound worker port or remove TLS/private-interface checks.

Run the normal update on Philemon first, then Vesta. The launcher must recreate each
host's containers once to register aliases and remove stale `/etc/hosts` entries;
restarting old containers alone cannot apply the fix. Keep the existing verified
backups, recordings.toml and installation directories. Do not remove data/work volumes
or either host's model directories. Retest semantic search and then each host reboot.
The separate host-port/Tailscale boot-order prerequisite still applies on Vesta.

A reproducible local IP-reassignment check uses existing model weights and a supplied
install image, with current backend code mounted read-only. It requires the personal
rootless Docker context and creates only labelled synthetic fixture resources:

```sh
python3 scripts/check_service_discovery.py --image YOUR_LOCAL_INSTALL_IMAGE
```

It runs one worker, a real E5-large semantic query over mTLS, forces both internal
upstream IPs to change while consumers remain running, verifies another uncached
query and blocked worker external DNS, then restarts existing containers with a
rotated RAM bearer and checks an unchanged durable synthetic index. Fixture cleanup
checks ownership labels before removing its containers, networks and volumes. It
never mounts real recordings and does not reboot the host or prove boot ordering.


## Coordinator private-listener boot recovery

Docker's restart policy cannot repair a container whose host-port publishing fails
before its process starts. If Tailscale's configured address is not present when
Docker restores the coordinator transport, the API can be healthy while no worker
can connect. This is separate from Caddy's LAN bind retry and private Docker DNS.

On a rootful Linux coordinator, opt in once after updating and starting the app:

```sh
sudo -v
python3 recordings.py setup-boot-recovery
```

Run as the installation's normal nonroot owner, using sudo for the prompted system
setup. No credentials are generated. The command validates the owned API/transport,
rootful daemon and exact Tailscale-only binding. It verifies and installs a root-owned
stdlib helper snapshot plus manifest, and enables an installation-specific systemd
oneshot service after Docker, Tailscale and network-online. It does not change the
Caddyfile, firewall, Docker unit, public ports, recordings, models or worker count.

The helper retries every five seconds only for the recorded host-port
`cannot assign requested address` failure, once the configured address exists. It
starts the already-existing transport by immutable container ID after verifying
its installation/role labels, private binding and restart policy. It never creates
containers or starts an absent/stopped API. Normal manual stops and unrelated runtime
failures are left unchanged. Start/stop and recovery share a persistent lifecycle
lock, so a concurrent operator stop cannot be undone by the helper.

Executable snapshots live under `/usr/local/lib/argon-recordings/boot/IDENTITY/`;
the shared lock is under `/var/lib/argon-recordings/boot/IDENTITY/`. Systemd executes
only root-owned code, never files from the user's checkout. Setup stages and verifies
the unit, backs up a previous unit under `/var/backups/argon-recordings/transport-boot-*`,
and restores its prior unit/enablement/active state on publication failure. It retains
unreferenced helper generations for rollback. Standard app updates keep the same
container names/identity, so registration survives them. Rerun setup when changing
the installation owner, private binding or recovery helper version.

For Argon's existing installation:

```sh
systemctl --no-pager status argon-recordings-a06fd327a1985e6d-boot-recovery.service
sudo -n journalctl -u argon-recordings-a06fd327a1985e6d-boot-recovery.service -b -n 30 --no-pager
```

Successful oneshot completion normally shows `active (exited)`. A missing address
shows retrying until it becomes available. Verify a real Vesta reboot afterward:
Caddy active, loopback health OK with one online worker, Forculus origin HTTP200,
semantic search and private admin MFA still working, without manual container starts.

To disable only this optional recovery helper:

```sh
sudo -n systemctl disable --now argon-recordings-a06fd327a1985e6d-boot-recovery.service
```

This leaves all application containers, data, model files and Caddy settings intact.
The feature currently supports rootful Docker and a Tailscale IPv4 coordinator only.
Local/worker-only roles and rootless Docker do not install a system recovery service.


## Automatic update backups and retention

After installing the backup-capable updater, the existing single command performs
backup, verification, installation, startup and retention:

```sh
sudo -v
python3 recordings.py update
```

For split deployments update Philemon first, then Vesta. Run when no recording is
processing. The one-time installation of this updater is executed by the old updater:
it preserves the existing verified manual backup; automatic snapshots begin with the
following actual update/reinstall. An already-current installation produces no extra
backup or downtime. Keep the existing verified backup through that first rollout.

Every actual update snapshots the existing deployment's named volumes: `/data` on
Vesta/coordinators, `/work` on Philemon/worker-only hosts, and both on local deployments.
Local additional worker workspaces are included. Models, RAM worker credentials and
TLS key mounts are excluded. Each backup includes private copies of recordings.toml,
installation.json, a verified Git source bundle, and a manifest of the source revision,
volume/image identities and SHA-256 payload hashes. Backups live in private
`.recordings/backups/update-TIMESTAMP-ID/` directories; they contain account/MFA data
and must not be published or committed. Existing backup/update directories must be
owned by the installation user and must not be group/world writable.

The updater estimates required space before stopping anything and preserves the
previous backup while writing the new one. Under the installation lifecycle lock it
stops all owned containers, creates uncompressed tar archives from read-only mounts
as application UID10001, compares each archive against the stopped volume, hashes all
payloads and flushes files/directory publication to disk. Only then does it advance
source/build. Lack of space, unreadable files, archive comparison/checksum or durability
failure blocks source changes and attempts to restart only the originally running
container IDs. Partial stop failures use the same recovery path. No automatic database
restore is attempted on a failed application install/start.

After install/start succeeds, it rechecks the kept backup's hashes before pruning
recognized older verified backups of the same installation/role whose volumes are
covered by the new snapshot. Recognized old `remote-inference-*` backups are adopted
and removed after a verified replacement. Failed updater-owned partial snapshots are
also removed on the next successful update. Unknown, damaged or foreign backups and
Caddy/system-helper rollback files are preserved. Any failed update keeps the new
backup and all older backups; retention only runs after a later successful update.

Updates are serialized. Manual install/start/stop commands queue behind an update;
a queued stop executes afterward. Installer subprocesses inherit the parent's checked
lock descriptor so startup does not deadlock. The separate lifecycle lock fences
backup consistency against private-listener boot recovery. Do not edit configuration
or use direct Docker lifecycle commands while the updater is working.

The manifest and Git bundle describe the pre-update rollback point, not the current
live application after it accepts new recordings. Restore only with the installation
stopped and an explicit recovery procedure. No model weights or live volumes are
deleted by retention.

Developer reproduction with personal rootless Docker and a supplied local install
image (no downloads, synthetic data only):

```sh
python3 scripts/check_update_backups.py --image YOUR_LOCAL_INSTALL_IMAGE
```

This creates actual UID10001-owned 0700 directories/0600 files for coordinator and
worker volumes, verifies snapshots by tar comparison, restores into separate disposable
volumes, checks content/permissions, and confirms one-backup retention. Only fixture-
labelled containers and volumes are removed at the end.
