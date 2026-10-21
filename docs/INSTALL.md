# Install and test on Ubuntu

This local installation bundles the web app, API, FFmpeg, Turbo worker, and multilingual search runtime in one Docker container. You need **Linux x86-64, Python 3.11 or later, working Docker 28+, and at least 8 GiB free disk**. Ubuntu's existing Docker installation is sufficient; no host Python packages, Node, FFmpeg, Compose, systemd units, or Docker socket mounts are needed. Docker itself is not installed or reconfigured by the launcher.

The app remains single-user and loopback-only. Use an SSH tunnel for a remote laptop/server. This is an evaluation package; public release/license and team authentication remain pending.

## First run

Extract the source package into a stable `argon-recordings` directory, then:

```bash
cd argon-recordings
python3 recordings.py init
nano recordings.toml
python3 recordings.py up
```

`up` builds the image, prepares the selected pinned models, starts the container, and waits for API readiness. Later use `start` without rebuilding. All inference dependencies are installed inside Docker. Building on an older laptop can take several minutes; build-time resources are separate from the configured running-container budget. First download: about 1.62 GB for Turbo plus your embedding choice. Existing valid model files are reused.

The config is TOML:

```toml
[app]
port = 8787
name = "argon-recordings"

[models]
transcription = "turbo"
embedding = "large"

[resources]
cpus = 2
memory_gib = 6

[docker]
sudo = false
```

`port` accepts 1024–65535. Port and CPU/RAM changes require `stop` then `start`. Model or source changes require `stop`, `install`, then `start`; prepared weights and data are reused. Keep the installation directory and `app.name` stable across upgrades. Each installation directory owns its data/work Docker volumes; moving it changes that identity. No automatic import of another app's recordings database occurs.

If Docker requires sudo, set `sudo = true` and run `sudo -v` in the same terminal before each administrative batch. The launcher uses `sudo -n docker` and never reads or stores the sudo password. Do not run the whole Python launcher under sudo.

## Curated models

| Setting | Purpose | Model files | Choice |
| --- | --- | --- | --- |
| `transcription = "turbo"` | Spanish/English ASR | ~1.62 GB | Only curated speech profile; CPU INT8, two threads |
| `embedding = "large"` | Multilingual E5-large INT8 | ~579 MB | Recommended retrieval accuracy |
| `embedding = "base"` | Multilingual E5-base INT8 | ~296 MB | Lower CPU/memory use |
| `embedding = "small"` | Multilingual E5-small INT8 | ~135 MB | Smallest footprint; weaker local retrieval results |
| `embedding = "off"` | Keyword search only | None | Disables semantic inference |

See [the measured model comparison](EMBEDDING-EVALUATION.md). Downloads use immutable revisions and embedded SHA-256 checks; runtime only loads local files. Arbitrary model names/URLs are rejected. Changing the search profile rebuilds incompatible vectors when indexed/searched. Keep unused weights until you deliberately decide to remove them; the installer never prunes them.

To reuse the Philemon benchmark's existing Turbo files, add the actual prepared directory under `[models]` before `up`:

```toml
turbo_path = "/path/to/benchmark/data/models/large-v3-turbo"
```

The launcher verifies this directory read-only and does not redownload or overwrite it. Other paths resolve relative to `recordings.toml`. A copied installation without these files can omit `turbo_path` and download its own curated copy.

## Access Philemon from your machine

In a separate terminal on the machine with your configured Philemon SSH alias:

```bash
ssh -N -L 8787:127.0.0.1:8787 your-server-alias
```

Open **http://localhost:8787**. Replace both port numbers if you changed the config. Replace `your-server-alias` with your configured SSH alias. The container publishes only `127.0.0.1`; do not add a public or Tailscale bind address to bypass the current local profile.

## Philemon acceptance check

```bash
python3 recordings.py doctor
python3 recordings.py status
python3 recordings.py evaluate
```

`evaluate` runs the existing 16-query bilingual fixture in a separate disposable two-CPU container with the selected prepared search model. It prints recall, warm encoding time and peak process RSS on this host; these synthetic relevance results are not a representative accuracy benchmark. Stop the app before evaluation if you want to avoid competing with it for CPU/RAM.

In the app, upload a Spanish or English clip (up to five minutes / 256 MiB). The worker polls automatically, processes one transcription or correction at a time, and updates progress. Check actual text/audio/frame alignment, search for the topic in both languages, and try **Correct text** with a technical term. Select matching passages, wait for proposals, then accept or dismiss them. A hint guides Turbo; it does not force exact text. Capture the job result, elapsed time, and any error before judging the installation accepted.

For diagnostics and shutdown:

```bash
python3 recordings.py logs
python3 recordings.py stop
```

`logs` is bounded to 100 lines; `stop` preserves all data and weights. The container restarts after a Docker/host reboot unless explicitly stopped. It runs as UID/GID 10001 with a read-only root/models, disk-backed data/work volumes, a 64 MiB temporary mount, capped logs, configured CPU/RAM, and no swap. Docker enforces the aggregate runtime limit across API/search/worker. Turbo unloads after two idle minutes; the search session stays warm. A native worker call has an independent 30-minute deadline. Lease fences protect publication after a timeout; failed temporary work can remain on the work volume.

At startup the service generates a temporary worker credential in process memory and passes it only to its API/worker child processes. It is not written to config, images, Docker Config.Env, logs, or volumes. Restart generates a new credential. No credential management is required for the bundled profile.

## Updates and data

Keep the installation directory, `recordings.toml`, `models/`, and `.recordings/`. Stop before replacing source files, then run `install` and `start`. Back up the data volume identified by `status`/Docker before upgrades. The current code adds its search/correction tables; it does not prune sources or migrate unrelated databases. There is no destructive uninstall command. Container replacement preserves both named volumes. Public/team deployment and storage lifecycle work remain separate milestones.
