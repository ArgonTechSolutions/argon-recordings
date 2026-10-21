# Argon Recordings

A self-hosted workspace for finding the useful moments in recordings. Built by Argon with React, shadcn/ui, and a small Python coordinator. No agent or generative-model account is required for the application workflow.

**Current stage: local foundation, not a public alpha.** Code license selection and public repository publication are pending. Do not treat this checkout as a licensed public release yet.

## Implemented

- Projects and recording library, with a clearly labelled synthetic design preview.
- Streaming uploads up to 256 MiB, with upload progress/cancellation and a durable queue.
- SQLite-backed worker leases, fencing, idempotent publication, one active lease, and bounded retry after worker loss. Urgent work gets a five-minute priority advantage; older normal work can overtake it.
- One-job CPU INT8 Turbo worker using explicit, checksum-verified local model files. No automatic model downloads.
- Up to five-minute recordings in this foundation profile. Audio extraction, source-relative transcript/word timestamps, and JPEG visual references at explicit 15-second seeks.
- Audio/transcript navigation, nearest retained frame with time offset, keyword search, and transcript text download.
- Light/dark appearance and responsive layouts using the supplied shadcn preset.

Semantic/hybrid search, glossary/correction workflows, range retention/pruning, original expiry, re-upload restoration, reports/ZIP exports, MCP, and team authorization are later milestones. Originals are retained in this version; nothing automatically deletes them. There is no idle-aware scheduler or job-cancel endpoint yet. See [the plan](docs/PLAN.md) and [validation status](docs/STATUS.md).

## Local build and preview

Prerequisites: Docker on Linux x86-64. No host Node/Python inference packages are necessary. Commands below are for an explicitly started **local, single-user preview**, not a production installation.

```bash
docker build -t argon-recordings:local .
docker run --rm --name argon-recordings-local \
  --cpus=1 --memory=1g --memory-swap=1g \
  -p 127.0.0.1:8000:8000 \
  -v argon-recordings-data:/data \
  argon-recordings:local
```

Open http://localhost:8000. `/?preview=1` shows synthetic example content, with uploads disabled. Stop the container to end the preview; the named volume retains your data. No service is installed automatically. Build-time resources are separate from run-time limits.

The current coordinator has loopback-host and same-origin checks but **no user authentication**. Bind/publish only to loopback and do not expose it on a LAN, Tailscale, or the public internet. Worker authentication is separate; its endpoints are disabled unless `RECORDINGS_WORKER_TOKEN` is supplied privately to the coordinator. There is no generated or committed credential. Team access needs the later authorization milestone.

## Worker integration

Build the worker independently:

```bash
docker build -f backend/Dockerfile.worker -t argon-recordings-worker:local .
```

The one-job CLI accepts `--api-url`, `--model-dir`, and `--work-dir`. Supply the same worker credential privately through the environment. It must reach the coordinator through a loopback endpoint, such as an explicitly configured private tunnel. A split-host installation has not been qualified yet.

Use the Turbo model directory produced by the benchmark package, including `benchmark-manifest.json`; its repository, immutable revision, and all required file hashes are checked. The selected model is `mobiuslabsgmbh/faster-whisper-large-v3-turbo` at `0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf`.

Run the worker container with two CPU equivalents, two inference threads, 6 GiB RAM, no swap, read-only root/model mounts, a disk-backed writable work directory, and no Docker socket. The image has an independent 30-minute job deadline. Model downloads are not part of the worker. No persistent polling process is started by the one-job CLI. Keeping one worker instance within an aggregate resource budget is an operator responsibility; an expired lease fences publication but cannot instantly terminate a remote native inference call.

## Development and validation

Frontend: Node 24, npm lockfile, React/TypeScript/Vite. Backend: Python 3.11 container and declared dependencies.

```bash
cd web
npm ci
npm run build
npm run lint
```

Backend tests use temporary data and synthetic inference:

```bash
docker build -f backend/Dockerfile -t argon-recordings-api:local .
docker run --rm --cpus=2 --memory=1g --memory-swap=1g \
  -v "$PWD":/workspace -w /workspace -e PYTHONPATH=/workspace/backend \
  argon-recordings-api:local \
  sh -lc 'python -m pytest -q backend/tests && python scripts/export_contract.py --check'
```

`docs/openapi.json` is generated from the real FastAPI routes. To update it, run `PYTHONPATH=backend python scripts/export_contract.py`. Generate `web/src/lib/schema.d.ts` with isolated `openapi-typescript@7.13.0` and `typescript@5.9.3`; the frontend retains the preset's TypeScript 6. This avoids an incompatible code-generator peer override.

The disposable Chromium harness is `scripts/browser-check.mjs`, with its runtime in `scripts/Dockerfile.browser`. It runs a temporary loopback server entirely inside its container and terminates it on completion. Its live-API checks seed synthetic audio/transcripts and do not establish speech quality or deployment acceptance. Configure `EVIDENCE_DIR` to a writable, disk-backed directory; the root defaults to `/evidence`. Use a bounded container command such as `timeout 120s node scripts/browser-check.mjs`.

The exact supplied frontend command was:

```text
npx shadcn@latest init --preset b5xcvlbv6 --template vite --pointer
```

It resolved to shadcn 4.22.1 during initialization. Installed dependencies are recorded in the npm lockfile. Component style: base-sera; base color: mauve; Inter font; Lucide icons; pointer cursors. Preserve these design choices when expanding the interface.
