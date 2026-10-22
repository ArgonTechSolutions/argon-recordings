# Foundation status

## Implemented and verified locally

- Dedicated Git repository, conventional commits, owner-supplied shadcn preset applied successfully.
- Projects, bounded streaming uploads, durable SQLite queue, priority aging, worker authentication, lease fencing and idempotent completion.
- Turbo one-job adapter, source checksum verification, bounded decode, sparse frames, audio extraction and source-time alignment including leading audio gaps.
- Responsive light/dark UI, library, upload progress/cancellation, queue states, transcript/audio/frame navigation, bilingual hybrid search, guided corrections and transcript download.
- Live API/OpenAPI contract and generated frontend schema types.

## Evidence

- `python -m pytest -q backend/tests`: **45 passed** with the explicit prepared large embedding model (44 pass / 1 skipped without it). Covers upload failure/limits, worker authorization, local boundary, concurrent claims, lease expiry, source access, project-scoped search, duplicate publication, restart persistence, synthetic pull protocol, explicit frame seeks and long-input rejection. Additional tests cover actual cross-language ONNX retrieval, persisted indexes, accepted-correction reindexing, similar spelling candidates, selected retry scope, hint delivery and nonzero audio-crop offsets, cancellation fencing, stale correction revisions/leases, project isolation and duplicate semantic jump targets. Speech inference remains synthetic. One non-blocking upstream TestClient/AnyIO deprecation warning remains.
- `python scripts/export_contract.py --check`: passed.
- `npm run build` and `npm run lint`: passed with the preset's TypeScript 6 toolchain. API generation uses isolated TypeScript 5 tooling because the generator's peer contract does not support TypeScript 6.
- Coordinator test image, standalone worker image, and combined web/coordinator image: real Docker builds passed locally. The combined runtime uses UID/GID 10001.
- `scripts/check_media_runtime.py`: actual FFmpeg synthetic video/audio decode, source-relative frames at 0/15/30 seconds, delayed-audio alignment, INT8 capability and hotword API passed. No speech inference runs in this check. A 31-second AAC fixture decodes to 31.04 seconds due to encoder padding; the check accounts for that.
- `scripts/browser-check.mjs`: Chromium desktop 1440×1000 and mobile 390×844 checks passed. Synthetic design preview, dark appearance, navigation, no horizontal overflow, and zero observed JavaScript errors. Live temporary API checks passed project creation, source upload, queued status, SQLite keyword search, real synthetic audio seeking and transcript download. The test starts and terminates its own isolated loopback server; it is not a deployed application.
- Combined image smoke: non-root data-volume initialization, API/project creation and bundled HTML serving passed in a disposable container.

## Boundaries

No persistent app or worker service is running from this work; the disposable installer smoke starts and stops its own instance. No remote app deployment, public repository, code license, real-recording app speech inference, team authentication, or remote-worker installation has been accepted. The prior Turbo benchmark remains the model-selection evidence; it is not an app integration test.

Project-wide glossary management, per-range retention/pruning, bounded media cache, source expiry/re-upload, report/ZIP export, MCP, and team authorization are not implemented yet. The coordinator currently retains original uploads and failed artifact remnants; storage lifecycle work is a later milestone. Uploads default to 256 MiB per file and are configurable through `app.max_upload_mb`; there is no fixed duration limit. Retained PCM audio must fit the work disk reserve and WAV format capacity. Docker run limits must be applied by the operator; an API lease cannot instantly terminate a disconnected remote process.

License selection and publishing remain pending.


## Search and correction milestone

- Default: quantized multilingual E5-large on CPU, two threads, 220-token overlapping windows, persistent SQLite vectors, exact cosine search and keyword rank fusion. Smaller profiles are configurable. Explicit immutable-revision/SHA-256 model preparation; runtime does not download models.
- Local model evaluation: [EMBEDDING-EVALUATION.md](EMBEDDING-EVALUATION.md). Large INT8 had the intended passage in the top three for 15/16 synthetic bilingual questions, about 901 MiB peak process RSS, and 49 ms warm median query encoding on Vulcan under two CPUs. These numbers are not representative retrieval accuracy or Philemon timings.
- Stable transcript segment IDs, bounded hint-based selected-passage retries, one heavy-work lease across ASR/corrections, reviewable proposals, previous-text history, stale-revision rejection and automatic index invalidation/refresh.
- Compact search controls and correction dialog; redundant sidebar/reference/upload copy removed. Chromium desktop/mobile checks pass selection, queued retry, proposal acceptance and updated transcript. No observed JavaScript errors; no horizontal overflow in tested layouts. The worker proposal in the browser check is synthetic.
- Frontend production build/lint and OpenAPI contract check passed. Combined coordinator/web and worker image builds passed. The non-root combined-image smoke uses the real embedding model offline and disposable API data. No persistent service is installed or started.

Remaining: Philemon performance measurement for embeddings, real-speech hinted retry qualification, representative recording relevance evaluation, and the previously listed deployment/storage/authentication milestones. Candidate discovery uses spelling similarity, so entirely different misrecognitions or omissions may require manually selecting another passage. It is not acoustic error detection. Search currently exposes at most 30 matches per query.


## Bundled installation milestone

- Python-stdlib launcher and TOML configuration: init/install/up/start/stop/status/logs/doctor/evaluate. Curated Turbo transcription plus large/base/small/off search profiles; configurable localhost port and aggregate CPU/RAM caps.
- Self-contained non-root Docker image packages web/API, FFmpeg and supervised automatic worker polling. Two-minute idle Turbo unload, optional operator job watchdog (disabled by default), disk-backed volumes, read-only root/model mounts, no swap and bounded logs. Temporary worker credential generated only in process memory at each boot; no operator-managed secret file.
- Explicit model preparation uses pinned SHA-256 files, can verify/reuse benchmark Turbo weights read-only, and installs no host packages/services. Lifecycle commands refuse unrelated or differently named installation containers. Stopping/replacing the container preserves data/work volumes.
- Actual installer smoke passed: build, model reuse/preparation, configurable port readiness, anonymous worker denial, aggregate resource limits, actual Turbo runtime processing a synthetic five-second silent upload automatically, restart preserving project/recording/volume identity, and final shutdown. The temporary credential is absent from Docker Config.Env. This is an installation-path check, not real-speech accuracy evidence.
- All 34 backend/installer tests and OpenAPI checks passed; real embedding integration is included with explicitly prepared weights. The frontend is unchanged from the preceding verified build/lint/browser snapshot and the installation image builds that same frontend. The packaged evaluate command runs the real selected large model offline in a disposable container.
- Philemon installation/performance and real-recording speech/correction acceptance remain pending owner execution. Use INSTALL.md; no remote access or sudo changes were performed from this dev server.


## Long recordings and UI simplification

Removed the five-minute restriction from decode and completion, the five-minute retained-audio byte budget, and the default thirty-minute worker deadline. Long-video frames are bounded/adaptive; upload/disk/memory budgets remain. Failed sources can be retried without re-uploading and stale worker publication remains fenced. Actual FFmpeg regression decodes 601 seconds with a retained WAV above 12 MiB; simulated two-hour input exercises full duration and bounded frame extraction.

All application dropdowns use the generated base-sera shadcn Select. Removed decorative workspace indicators, marketing headings/footers and eyebrows, while retaining functional project/status/error controls and the labelled synthetic preview. Argon credit links to https://www.argon.com.pe.

Verification for the long-recording/UI change: 38 backend tests passed with real embedding integration; OpenAPI check, production frontend build and lint passed. Actual FFmpeg decoded 601 seconds without truncation and preserved delayed-audio alignment. Desktop/mobile Chromium passed all new Select interactions, removed-copy/Argon-link checks, correction flow and failed-source Retry with no observed JavaScript errors. The updated bundled image passed automatic Turbo silence processing, resource/credential checks, restart persistence and final shutdown. Real long-speech inference on Philemon remains owner-run.

Upload-limit setting: the validated TOML value is passed through `RECORDINGS_MAX_UPLOAD_MB`, exposed through API health, and used by the upload dialog for validation/help text. The default remains 256 MiB; positive whole MiB values support larger files. The Retry button has 20 px top margin.

Upload-limit verification: 45 backend tests and the OpenAPI check passed. Frontend production build/lint passed. Chromium verified a non-default server limit appears in the dialog, oversized files are rejected, smaller uploads still work, and the Retry button has at least 20 px top margin/gap. The test uses a 1 MiB limit rather than allocating multi-gigabyte fixtures; separate configuration/API checks verify the 2048 MiB byte conversion and container environment argument.

## Isolated workers and overlapping chunks

The installer now starts separate API/account and transcription containers. The default remains one worker; optional workers share the aggregate runtime budget and can process chunks of one recording. Pause-aware cores, two-second context on each side by default, wider-context boundary tasks, timestamp-based duplicate detection and conservative omission checks precede atomic publication. Retries reuse completed cores. Chunk heartbeats refresh aggregate progress and the frontend retains locally ticking elapsed time in the prepared state. See [WORKERS.md](WORKERS.md).

Validation for this slice:

- Full backend suite: **137 passed, 1 skipped** (opt-in real embedding test); two upstream deprecation warnings. Final launcher/isolation/update checks after the port-probe fix: **47 passed**.
- OpenAPI contract check, generated frontend types, production build and lint passed. Chromium regression passed, including prepared-state elapsed ticking, SSE, corrections and mobile overflow, with no page errors.
- Actual source-labelled installer image `f9248c8f9a49cd58` built successfully. A disposable two-worker installation processed a 64-second synthetic silence WAV through real FFmpeg/Turbo, distributing core/boundary tasks across both workers. This verifies integration, not spoken-audio accuracy or speed.
- Container probes passed: separate account/work/model/credential mounts, aggregate CPU/RAM caps, swap disabled, no credential in Docker environment, worker denial of browser/admin APIs, public denial of worker routes, and blocked worker external HTTP/DNS. API restart rotated credentials; full stop/start preserved the synthetic recording. Disposable services were stopped afterward.
- Independent GPT-6.1 security review found no remaining concrete blocker after bounding malicious word timelines/cut density and disabling legacy whole-job publication in isolated mode.

No Philemon deployment or public exposure occurred. Real-host resource/recording acceptance, public upload quotas and ingress qualification remain pending. Smart visual retention and automatic worker scaling remain future work.
