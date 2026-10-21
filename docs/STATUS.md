# Foundation status

## Implemented and verified locally

- Dedicated Git repository, conventional commits, owner-supplied shadcn preset applied successfully.
- Projects, bounded streaming uploads, durable SQLite queue, priority aging, worker authentication, lease fencing and idempotent completion.
- Turbo one-job adapter, source checksum verification, bounded decode, sparse frames, audio extraction and source-time alignment including leading audio gaps.
- Responsive light/dark UI, library, upload progress/cancellation, queue states, transcript/audio/frame navigation, bilingual hybrid search, guided corrections and transcript download.
- Live API/OpenAPI contract and generated frontend schema types.

## Evidence

- `python -m pytest -q backend/tests`: **18 passed** with the explicit prepared large embedding model (17 pass / 1 skipped without it). Covers upload failure/limits, worker authorization, local boundary, concurrent claims, lease expiry, source access, project-scoped search, duplicate publication, restart persistence, synthetic pull protocol, explicit frame seeks and long-input rejection. Additional tests cover actual cross-language ONNX retrieval, persisted indexes, accepted-correction reindexing, similar spelling candidates, selected retry scope, hint delivery and nonzero audio-crop offsets, cancellation fencing, stale correction revisions/leases, project isolation and duplicate semantic jump targets. Speech inference remains synthetic. One non-blocking upstream TestClient/AnyIO deprecation warning remains.
- `python scripts/export_contract.py --check`: passed.
- `npm run build` and `npm run lint`: passed with the preset's TypeScript 6 toolchain. API generation uses isolated TypeScript 5 tooling because the generator's peer contract does not support TypeScript 6.
- Coordinator test image, standalone worker image, and combined web/coordinator image: real Docker builds passed locally. The combined runtime uses UID/GID 10001.
- `scripts/check_media_runtime.py`: actual FFmpeg synthetic video/audio decode, source-relative frames at 0/15/30 seconds, delayed-audio alignment, INT8 capability and hotword API passed. No speech inference runs in this check. A 31-second AAC fixture decodes to 31.04 seconds due to encoder padding; the check accounts for that.
- `scripts/browser-check.mjs`: Chromium desktop 1440×1000 and mobile 390×844 checks passed. Synthetic design preview, dark appearance, navigation, no horizontal overflow, and zero observed JavaScript errors. Live temporary API checks passed project creation, source upload, queued status, SQLite keyword search, real synthetic audio seeking and transcript download. The test starts and terminates its own isolated loopback server; it is not a deployed application.
- Combined image smoke: non-root data-volume initialization, API/project creation and bundled HTML serving passed in a disposable container.

## Boundaries

No persistent app or worker service is running from this work. No remote app deployment, public repository, code license, real-recording app speech inference, team authentication, or remote-worker installation has been accepted. The prior Turbo benchmark remains the model-selection evidence; it is not an app integration test.

Project-wide glossary management, per-range retention/pruning, bounded media cache, source expiry/re-upload, report/ZIP export, MCP, and team authorization are not implemented yet. The coordinator currently retains original uploads and failed artifact remnants; storage lifecycle work is a later milestone. The first recording limit is five minutes and 256 MiB. Docker run limits must be applied by the operator; an API lease cannot instantly terminate a disconnected remote process.

License selection and publishing remain pending.


## Search and correction milestone

- Default: quantized multilingual E5-large on CPU, two threads, 220-token overlapping windows, persistent SQLite vectors, exact cosine search and keyword rank fusion. Smaller profiles are configurable. Explicit immutable-revision/SHA-256 model preparation; runtime does not download models.
- Local model evaluation: [EMBEDDING-EVALUATION.md](EMBEDDING-EVALUATION.md). Large INT8 had the intended passage in the top three for 15/16 synthetic bilingual questions, about 901 MiB peak process RSS, and 49 ms warm median query encoding on Vulcan under two CPUs. These numbers are not representative retrieval accuracy or Philemon timings.
- Stable transcript segment IDs, bounded hint-based selected-passage retries, one heavy-work lease across ASR/corrections, reviewable proposals, previous-text history, stale-revision rejection and automatic index invalidation/refresh.
- Compact search controls and correction dialog; redundant sidebar/reference/upload copy removed. Chromium desktop/mobile checks pass selection, queued retry, proposal acceptance and updated transcript. No observed JavaScript errors; no horizontal overflow in tested layouts. The worker proposal in the browser check is synthetic.
- Frontend production build/lint and OpenAPI contract check passed. Combined coordinator/web and worker image builds passed. The non-root combined-image smoke uses the real embedding model offline and disposable API data. No persistent service is installed or started.

Remaining: Philemon performance measurement for embeddings, real-speech hinted retry qualification, representative recording relevance evaluation, and the previously listed deployment/storage/authentication milestones. Candidate discovery uses spelling similarity, so entirely different misrecognitions or omissions may require manually selecting another passage. It is not acoustic error detection. Search currently exposes at most 30 matches per query.
