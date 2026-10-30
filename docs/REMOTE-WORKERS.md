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
