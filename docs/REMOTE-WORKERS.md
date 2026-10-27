# Remote worker installations

The optional split deployment runs the web app, accounts, recordings and search on a
coordinator, and exactly one Turbo CPU INT8 worker on another Linux host. Existing
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
embedding = "off"
# Retain the current turbo_path if configured; otherwise models/turbo is reused.
```

The transport reserves 0.1 CPU and 128 MiB on each host. Philemon's remaining budget
goes to one worker with two inference threads. Coordinator CPU/RAM includes its
transport; embedding models remain on the coordinator. Worker mode neither prepares
embedding models nor mounts the recordings database. Coordinator mode does not
prepare or mount Turbo. An existing installation's directories and named data/work
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
the recordings volume. The launcher reads the existing files and loads temporary
copies into a dedicated RAM-backed Docker volume. Real certificates and private
keys must be provisioned separately; installation does not generate them.

The coordinator transport requires TLS 1.3 and the pinned worker certificate. It
forwards only worker API requests to the private coordinator interface and injects
the API's temporary in-memory bearer credential. The bearer is reread per request,
so API restarts rotate it without reprovisioning Philemon. The media worker has an
internal Docker network and only its work volume and read-only Turbo model mount;
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

**Host reboot or stopping the last container that mounts the TLS volume clears its
RAM contents.** Docker restart alone cannot reload certificate files. Run
`python3 recordings.py stop` then `python3 recordings.py start` to reload the existing
host files. API or inference process restarts reconnect automatically while the
transport remains running. Unattended host-boot integration is a separate deployment
step; do not treat Docker's restart policy as full host-recovery automation.

Before moving Argon's existing instance: stop ingestion and workers, create and
verify a backup, migrate the complete coordinator data including accounts/MFA to
Vesta, and verify it before opening traffic. Keep Philemon's existing installation
path and model/work volumes. The launcher deliberately does not copy or adopt data
between hosts. Starting Vesta with an empty data volume is not a migration. A later
runbook must identify the actual source/destination volume names and rollback copy;
no production volumes should be removed during this transition.
