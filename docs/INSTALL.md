# Install and test on Ubuntu

This local installation bundles the web app, API, FFmpeg, Turbo worker, and multilingual search runtime in one Docker container. You need **Linux x86-64, Python 3.11 or later, working Docker 28+, and at least 8 GiB free disk**. Ubuntu's existing Docker installation is sufficient; no host Python packages, Node, FFmpeg, Compose, systemd units, or Docker socket mounts are needed. Docker itself is not installed or reconfigured by the launcher.

The app remains single-user and loopback-only. Use an SSH tunnel for a remote laptop/server. This is a public source preview; license selection, release qualification, and team authentication remain pending.

## First run

Clone the source into a stable directory (`git clone https://github.com/ArgonTechSolutions/argon-recordings.git ~/argon-recordings`), then run the following commands there. Existing archive installations should use [Git updates](#git-updates) instead:

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
max_upload_mb = 256
name = "argon-recordings"

[models]
transcription = "turbo"
embedding = "large"

[resources]
cpus = 2
memory_gib = 6
job_timeout_seconds = 0

[docker]
sudo = false
```

`port` accepts 1024–65535. Set `app.max_upload_mb` to a positive whole MiB value, for example `2048` for 2 GiB. It defaults to 256; the upload dialog displays/enforces the actual server value. Upload-limit, port and CPU/RAM changes require `stop` then `start`. Model or source changes require `stop`, `install`, then `start`; prepared weights and data are reused. Keep the installation directory and `app.name` stable across upgrades. Each installation directory owns its data/work Docker volumes; moving it changes that identity. No automatic import of another app's recordings database occurs.

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

In the app, upload a Spanish or English clip (up to your configured per-file limit; no fixed duration limit). The worker polls automatically, processes one transcription or correction at a time, and updates progress. Check actual text/audio/frame alignment, search for the topic in both languages, and try **Correct text** with a technical term. Select matching passages, wait for proposals, then accept or dismiss them. A hint guides Turbo; it does not force exact text. Capture the job result, elapsed time, and any error before judging the installation accepted.

For diagnostics and shutdown:

```bash
python3 recordings.py logs
python3 recordings.py stop
```

`logs` is bounded to 100 lines; `stop` preserves all data and weights. The container restarts after a Docker/host reboot unless explicitly stopped. It runs as UID/GID 10001 with a read-only root/models, disk-backed data/work volumes, a 64 MiB temporary mount, capped logs, configured CPU/RAM, and no swap. Docker enforces the aggregate runtime limit across API/search/worker. Turbo unloads after two idle minutes; the search session stays warm. No default job deadline is applied. Set `resources.job_timeout_seconds` to a positive number if you want an operator deadline; zero disables it. Long recordings need proportionally more retained-audio disk space and CPU time; disk, memory and WAV format budgets still apply. Frame spacing adapts to keep at most 256 visual references. Lease fences protect publication after a timeout; failed temporary work can remain on the work volume.

At startup the service generates a temporary worker credential in process memory and passes it only to its API/worker child processes. It is not written to config, images, Docker Config.Env, logs, or volumes. Restart generates a new credential. No credential management is required for the bundled profile.

## Updates and data

Keep the installation directory, `recordings.toml`, `models/`, and `.recordings/`. Stop before replacing source files, then run `install` and `start`. Back up the data volume identified by `status`/Docker before upgrades. The current code adds its search/correction tables; it does not prune sources or migrate unrelated databases. There is no destructive uninstall command. Container replacement preserves both named volumes. Public/team deployment and storage lifecycle work remain separate milestones.


## Updating an existing Philemon installation

Stop before extracting the update into the same parent directory, then rebuild/start. Do not run `init` again and do not move the installation directory. Source archives exclude `recordings.toml`, `.recordings/`, `models/` and data; the existing settings, weights and named volumes are preserved. After updating, open the failed recording and choose **Retry recording** to reuse its retained upload. Refresh the browser to load the new UI. The old five-minute failure does not require another upload.

## Git updates

Prerequisites: host Git and Python 3.11+, plus the existing Docker installation. Public HTTPS pulls require no GitHub account or key.

For an existing archive installation on Philemon, keep its current directory. Convert it once using a fresh clone:

```bash
git clone https://github.com/ArgonTechSolutions/argon-recordings.git ~/argon-recordings-source
sudo -v
python3 ~/argon-recordings-source/scripts/bootstrap_git.py ~/argon-recordings
```

The bootstrap checks the archive against repository history before adding Git metadata. It refuses modified or unknown source. Configuration, local model directories, and named recording volumes stay in place. Keeping the installation path preserves the volume identity.

For subsequent updates:

```bash
cd ~/argon-recordings
sudo -v
python3 recordings.py update
python3 recordings.py status
```

The updater requires a clean `main` checkout with the official origin, fetches before stopping anything, and only fast-forwards. It stops the app for the build and restarts it if previously running; a stopped installation stays stopped. Back up the recording data volume before upgrades. Downloaded model caches are reused. Build-time resources remain separate from runtime caps.

If installation fails after advancing the source, the app remains stopped and data volumes are retained. Resolve the reported error, then run `python3 recordings.py install` and `python3 recordings.py start`. The updater does not reset source history or automatically roll back database changes. These commands have local disposable test coverage; the Philemon conversion must still be run by its operator.
