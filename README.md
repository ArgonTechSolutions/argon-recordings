# Argon Recordings

A self-hosted workspace for finding the useful moments in recordings. Built by Argon with React, shadcn/ui, and a small Python coordinator. No agent or generative-model account is required for the application workflow.

**Current stage: local foundation, not a public alpha.** Code license selection and public repository publication are pending. Do not treat this checkout as a licensed public release yet.

## Implemented

- Projects and recording library, with a clearly labelled synthetic design preview.
- Streaming uploads with a configurable per-file limit (256 MiB by default), with upload progress/cancellation and a durable queue.
- SQLite-backed worker leases, fencing, idempotent publication, one active lease, and bounded retry after worker loss. Urgent work gets a five-minute priority advantage; older normal work can overtake it.
- One-job CPU INT8 Turbo worker using explicit, checksum-verified local model files. No automatic model downloads.
- No fixed recording-duration limit. Audio extraction and source-relative timestamps are preserved; visual references use explicit seeks with adaptive spacing and at most 256 frames per recording. Uploads use the configured per-file limit; retained audio must fit available work space and the WAV format budget.
- Audio/transcript navigation, nearest retained frame with time offset, multilingual semantic/keyword search, and transcript text download.
- Mark misheard text, find similar spelling variants in the recording, retry selected audio passages with a hint, and review proposals before acceptance. Prior text is retained in correction history.
- Light/dark appearance and responsive layouts using the supplied shadcn preset.

Project-wide glossary management, range retention/pruning, original expiry, re-upload restoration, reports/ZIP exports, MCP, and team authorization are later milestones. Originals are retained in this version; nothing automatically deletes them. There is no idle-aware scheduler or general job-cancel endpoint yet. Failed recordings can be retried from their retained source without re-uploading. See [the plan](docs/PLAN.md) and [validation status](docs/STATUS.md).

## Easy installation

On Linux x86-64 with Python 3.11+ and Docker 28+/cgroup v2:

```bash
python3 recordings.py init
# Edit recordings.toml: app port, curated search model, Docker sudo if needed.
python3 recordings.py up
```

The launcher builds and installs dependencies inside Docker, prepares verified local models, and starts the web/API/background worker on your configured localhost port. Set `app.max_upload_mb = 2048` for 2 GiB uploads (default 256 MiB); restart after changing the limit. Search choices are `large` (default), `base`, `small`, or `off`; transcription stays Turbo CPU INT8. `stop` preserves data, `start` reuses the installation, and `evaluate` measures the selected embedding model on the host. Temporary internal worker credentials live only in process memory. See [installation and Philemon testing](docs/INSTALL.md) for Docker permissions, benchmark model reuse, SSH access, resource caps and upgrades.

## Local build and preview

Prerequisites: Docker on Linux x86-64. No host Node/Python inference packages are necessary. Commands below are for an explicitly started **local, single-user preview**, not a production installation.

```bash
# Explicit, checksum-verified embedding model download (~579 MB).
docker build -f backend/Dockerfile -t argon-recordings-api:local .
docker run --rm --cpus=2 --memory=1g --memory-swap=1g \
  -v "$PWD":/workspace -w /workspace -e PYTHONPATH=/workspace/backend \
  argon-recordings-api:local python scripts/prepare_embeddings.py models/e5-large large

docker build -t argon-recordings:local .
docker run --rm --name argon-recordings-local \
  --cpus=2 --memory=2g --memory-swap=2g \
  -p 127.0.0.1:8000:8000 \
  -v argon-recordings-data:/data \
  -v "$PWD/models/e5-large":/models/embeddings:ro \
  -e RECORDINGS_EMBEDDING_DIR=/models/embeddings \
  argon-recordings:local
```

Open http://localhost:8000. `/?preview=1` shows synthetic example content, with uploads disabled. Stop the container to end the preview; the named volume retains your data. No service is installed automatically. Build-time resources are separate from run-time limits.

The current coordinator has loopback-host and same-origin checks but **no user authentication**. Bind/publish only to loopback and do not expose it on a LAN, Tailscale, or the public internet. Worker authentication is separate; its endpoints are disabled unless `RECORDINGS_WORKER_TOKEN` is supplied privately to the coordinator. There is no generated or committed credential. Team access needs the later authorization milestone.

## Search and corrections

The default embedding profile is quantized **multilingual E5-large**. English, Spanish, and cross-language queries use token-aware overlapping chunks and hybrid ranking. Vectors live in SQLite; retrieval is exact cosine similarity, with no separate vector service. See [model/backend evaluation](docs/EMBEDDING-EVALUATION.md) for measured tradeoffs and limits. Initial indexing and model loading can make the first search slower; new publications and accepted corrections refresh their index automatically. Text queries remain local.

To use a smaller profile, prepare `models/e5-base base` or `models/e5-small small`, mount that directory, and set `RECORDINGS_EMBEDDING_PROFILE=base` or `small`. With no `RECORDINGS_EMBEDDING_DIR`, keyword search remains usable. Model changes rebuild incompatible stored vectors. Retained audio, transcript text, and embeddings stay on the coordinator's disk-backed data volume.

Select a word in the transcript and choose **Correct text**, or enter the misheard text manually. Provide the correct term/hint, select matching passages, and queue the retries. Discovery searches spelling variants throughout the same recording; it cannot identify every acoustic error or recover wholly omitted words. Each selected passage must be at most 60 seconds; a request may select up to ten. The existing one-job worker processes queued corrections when no original transcription is claimable. It crops the retained audio and supplies the hint as Turbo `initial_prompt` and `hotwords`. Hints guide recognition; they do not guarantee a replacement. Review the proposed text and **Accept** or **Dismiss** it. Accepted changes preserve previous text, update keyword search, and rebuild the recording's embedding index. Queued/running retries can be dismissed; stale workers cannot publish afterward.

This milestone adds only new SQLite tables/indexes, leaving the foundation tables and sources intact. Unknown schema versions still fail closed. Back up the data volume before upgrading an installation. No remote installation or real-data upgrade has been performed as part of development.

## Worker integration

Build the worker independently:

```bash
docker build -f backend/Dockerfile.worker -t argon-recordings-worker:local .
```

The one-job CLI accepts `--api-url`, `--model-dir`, and `--work-dir`. Supply the same worker credential privately through the environment. It must reach the coordinator through a loopback endpoint, such as an explicitly configured private tunnel. A split-host installation has not been qualified yet.

Use the Turbo model directory produced by the benchmark package, including `benchmark-manifest.json`; its repository, immutable revision, and all required file hashes are checked. The selected model is `mobiuslabsgmbh/faster-whisper-large-v3-turbo` at `0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf`.

Run the worker container with two CPU equivalents, two inference threads, 6 GiB RAM, no swap, read-only root/model mounts, a disk-backed writable work directory, and no Docker socket. There is no default job deadline. The bundled installer accepts an optional `resources.job_timeout_seconds` (zero disables it). Model downloads are not part of the worker. No persistent polling process is started by the one-job CLI. Keeping one worker instance within an aggregate resource budget is an operator responsibility; an expired lease fences publication but cannot instantly terminate a remote native inference call.

## Development and validation

Frontend: Node 24, npm lockfile, React/TypeScript/Vite. Backend: Python 3.11 container and declared dependencies.

```bash
cd web
npm ci
npm run build
npm run lint
```

Backend tests use temporary data and synthetic speech inference. The real embedding integration test is opt-in:

```bash
docker build -f backend/Dockerfile -t argon-recordings-api:local .
docker run --rm --cpus=2 --memory=1g --memory-swap=1g \
  -v "$PWD":/workspace -w /workspace -e PYTHONPATH=/workspace/backend \
  argon-recordings-api:local \
  sh -lc 'python -m pytest -q backend/tests && python scripts/export_contract.py --check'
```

For the real embedding check, add a 3 GiB container memory limit and `-e TEST_EMBEDDING_DIR=/workspace/models/e5-large` to the test command after preparing the weights. `scripts/evaluate_embeddings.py models/e5-large --profile large` reproduces the bounded bilingual retrieval fixture; its timings must be measured on Philemon before claiming Philemon performance.

`docs/openapi.json` is generated from the real FastAPI routes. To update it, run `PYTHONPATH=backend python scripts/export_contract.py`. Generate `web/src/lib/schema.d.ts` with isolated `openapi-typescript@7.13.0` and `typescript@5.9.3`; the frontend retains the preset's TypeScript 6. This avoids an incompatible code-generator peer override.

The disposable Chromium harness is `scripts/browser-check.mjs`, with its runtime in `scripts/Dockerfile.browser`. It runs a temporary loopback server entirely inside its container and terminates it on completion. Its live-API checks seed synthetic audio/transcripts and do not establish speech quality or deployment acceptance. Configure `EVIDENCE_DIR` to a writable, disk-backed directory; the root defaults to `/evidence`. Use a bounded container command such as `timeout 120s node scripts/browser-check.mjs`.

The exact supplied frontend command was:

```text
npx shadcn@latest init --preset b5xcvlbv6 --template vite --pointer
```

It resolved to shadcn 4.22.1 during initialization. Installed dependencies are recorded in the npm lockfile. Component style: base-sera; base color: mauve; Inter font; Lucide icons; pointer cursors. Preserve these design choices when expanding the interface.
