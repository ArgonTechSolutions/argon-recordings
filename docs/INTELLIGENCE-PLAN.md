# Speakers, visual evidence, and agent workflows

Planning baseline: 2026-10-23. Owner: Argon. Repository: ArgonTechSolutions/argon-recordings, clean `main` at `3d4eda7` when inspected. This document is a ticket preview, not implementation or deployment evidence.

YouTrack project **REC / Argon Recordings** and its Kanban board have been created with owner authorization. Board: https://youtrack.argon.com.pe/agiles/196-10. Project ID: `0-14`. On 2026-10-23 the owner approved ticket creation and P01–P14 implementation. Epic: [REC-1](https://youtrack.argon.com.pe/issue/REC-1). P01–P22 map in order to REC-2–REC-23 (ticket number = P number + 1). Parent/dependency links are created; all 23 cards were verified in sprint `210-10`. P01/REC-2 and epic REC-1 are in Develop. No KB article has been created.

## Confirmed product scope

Implementation checkpoint (local source; not installed/deployed): evidence snapshots, revision-checked marks/history/undo, exact source links and durable shared-capacity diarization are implemented. Speaker review supports recording-scoped names, range reassignment, unknown/overlap, filtering, reset/undo and revision-pinned pagination. Manual edits survive reruns; ambiguous model cluster changes do not inherit a person's name. SSE refreshes other open clients. Only verified online model workers enable analysis in the UI. Installer/model provisioning remains pending. Backend regression: 181 passed, 1 optional integration skipped; OpenAPI check, frontend production build/lint and disposable Chromium passed, including cross-tab edits and viewer denial. P01/P02/P03 remain in Develop; P04/REC-5 and P05/REC-6 are in Staging (local qualification awaiting integration, not deployment). Two real CPU model candidates both passed only 1/5 held-out speaker counts, below the 4/5 gate. Standard DER with overlap excluded and a 250 ms total collar was 5.59% for the frozen Mandarin embedding and 22.91% for the English alternative. See `docs/DIARIZATION-EVALUATION.md`; automatic diarization quality is not accepted.

Visual implementation checkpoint: P06/P07 add source-bound durable screenshot/muted-clip jobs, shared worker admission, integrity-checked publication and private range serving. Browser controls extract from playhead/marks, zoom screenshots, synchronize clips to the original audio clock and expose all sparse frames. Backend core regression: 185 passed; five focused visual queue tests passed including isolated worker/restart fencing and missing-derivative regeneration. The combined actual FFmpeg runtime/publication/analysis suite passed 24 tests, including nonzero source origins, delayed tracks, crop/rotation/SAR and a source-resolution still above 1920 px. Production build/lint, existing Chromium regression and new real-media Chromium workflow passed (offset/seek/pause/speed/cancel and 320/390/768 px). Evidence remains local under ignored `models/validation/`. P06/P07 are locally qualified pending shared integration (Staging, not deployment). Adaptive analysis has a tested runtime (frozen generated coverage 4/4 events, one unmatched and two duplicate proposals), with application integration, real-footage generalization and text readability still outstanding. Agent access/MCP, summary packaging, exports and release qualification remain outstanding.

Agent implementation checkpoint (2026-10-26, local source/runtime): account-scoped expiring/revocable grants, bounded evidence API and twelve-tool stdio MCP adapter are implemented. Backend regression passed 222 tests including prepared E5-large and real FFmpeg; OpenAPI check, frontend production build/lint and account/MFA Chromium passed. The mobile grant dialog overflow is fixed at 320/390 px. Four published-SDK checks passed, including actual grant → API → extraction queue → FFmpeg worker → native JPEG retrieval/decoding and revocation. No real owner credential or remote connection was provisioned. P09/P10 are locally qualified pending shared integration; P08 application integration, P11 summary skill/plugin, P12 exports and P13/P14 integrated quality/release gates remain incomplete. Diarization speaker-count quality remains blocked as reported above. See `docs/AGENT-ACCESS.md`.

Summary checkpoint (2026-10-26): P11 now has a repository-owned cited-summary skill, portable private plugin ZIP builder, credential-free connection example and versioned report/evidence schema. The adapter also forwards project/search pagination and uses the actual paginated search response shape. The combined SDK/backend/package suite passed 24 tests; an independent GPT-6.1 forward-test inspected a real FFmpeg JPEG, ignored hostile quoted instructions and produced a cited Spanish draft that passed reference/coverage/literal-assignment checks. This was captured disposable evidence, not a live host-loaded plugin or representative semantic benchmark. P11 remains Develop pending adaptive/integrated workflow qualification. No plugin installation or real grant provisioning occurred. See `docs/SUMMARY-WORKFLOW.md`.

Adaptive integration checkpoint (2026-10-26, supersedes earlier P08 application gaps): durable analysis jobs, shared extraction/ASR admission, strict source/lease/result publication and explicit proposal acceptance/rejection are integrated with the production-built UI. Manual pin/policy precedence, mark undo, restart and role denial pass API tests; actual FFmpeg worker results and two-tab decisions/undo/cancellation pass Chromium at 320/390/768 px with no console errors. Fractional/sub-frame intervals and 30-second motion caps are covered. The final backend suite passed 233 tests and the OpenAPI check; production build/lint and the 24-test SDK/summary regression passed. P08 remains in Test for real-footage/readability qualification. P12 exports and P13/P14 integrated quality/release gates remain incomplete; the diarization speaker-count gate, owner pairing, private recordings and Philemon qualification remain separate. See `docs/VISUAL-EVALUATION.md`.

- Local CPU diarization with generic speaker labels and manual naming; no paid inference service.
- Visual handling optimized for screen recordings, code, slides, and demonstrations.
- A usable end-to-end workflow: speakers → marked video → agent summary with screenshots and source citations.
- Run actual model-quality tests, not just mocked inference, during the authorized overnight implementation. Download openly licensed evaluation media and ungated models; verify their actual licenses and record provenance first. Private-recording acceptance remains separate.
- Prepare a candidate that the owner can assess for deployment. Public deployment stays in the original chat. No Philemon, Forculus, DNS, Tailscale, production configuration, or public ingress changes in this work.

## Product behavior

Open a processed recording and see speaker-attributed passages. Rename “Speaker 1” to a known participant without changing the recognized words. Select a point or range on the shared timeline, choose a readable still or motion clip, and pin useful evidence. Automatic analysis proposes stable screenshots for slides/code and denser coverage for demonstrations. Manual choices win.

An authorized agent can find a recording, read a bounded transcript, inspect speaker labels and visual evidence, request a missing screenshot or short clip, and produce a summary that links each substantive claim to source time ranges. Decisions and action items distinguish what was explicitly said from inference. Speaker names remain user assertions, not verified identities.

The first release does not need an embedded chat interface or a hosted LLM subscription. The connected agent performs synthesis. The application provides evidence, permissions, bounded media jobs, and optional reviewed report storage/export.

## Verified starting architecture

- Python backend with FastAPI/Pydantic, SQLite/FTS5, local ONNX embeddings, pinned faster-whisper/Turbo inference; React/Vite/TypeScript frontend with npm lockfile and shadcn preset `b5xcvlbv6`.
- `backend/argon_recordings/worker.py::extract` retains 16 kHz mono audio and at most 256 JPEG frames, spaced at least 15 seconds, at a maximum width of 1280. It does not retain playable video derivatives.
- `contracts.py` permits only `audio` and `frame` artifact kinds and narrowly named files. New assets require coordinated protocol, validation, storage, API, and UI changes.
- `store.py` persists word timestamps, but has no speaker turns, visual annotations, or report versions. Word timelines are a useful attribution input, not a guarantee of precise alignment.
- `api.py` exposes registered audio/JPEG artifacts; original uploads remain worker-only. `App.tsx` synchronizes an audio player and nearest retained frame and shows only the first 20 strip frames.
- Durable preparation/chunk/correction leases and isolated worker identities already exist. Boundary reconciliation and saved-result retry must remain intact.
- `accounts.py` authorizes browser routes by user/project role. Its URL/method-based browser checks must not be assumed to protect a new MCP principal automatically.
- Local `models/` contains Turbo and E5 models, approximately 3.6 GiB total. No representative audio/video or reference diarization corpus was found in the tracked tree. No diarization weights were identified in the inspected model directory listing.
- One worker is the Philemon default. Turbo remains CPU INT8, two threads. Host CPU limits are unverified; the intended 2-CPU worker plus 0.5-CPU API allocation is not a measured host fact.

## Technical decisions and bounded uncertainties

### Diarization

Keep ASR and diarization separate. Run recording-wide speaker clustering so independently processed chunks do not invent incompatible speaker identities. Persist recording-scoped speaker IDs, raw turns including overlap, an attribution version, and manual overrides. Align words to turns where possible; use unknown/overlap rather than forced attribution when ambiguous. Do not rewrite segment text or IDs merely to change speakers. Keep optional enrichment failure separate from transcript readiness.

Evaluate a CPU/ONNX pipeline first and compare another legally available local candidate if useful. Sherpa-ONNX exposes separate segmentation and embedding models, but the licenses of every selected weight must be checked independently from the library license. Pyannote Community-1 is a reference candidate: its official card documents offline CPU inference and exclusive turns, but access conditions require human acceptance. Do not accept those conditions, create an account/token, or substitute a noncommercial model for Argon's production candidate autonomously.

Choose the production candidate using measured attribution error, speaker counting, runtime, peak memory, and installation complexity. Freeze the model/revision/checksums and evaluation split before final scoring. A model-access block must not prevent independent video/MCP work; it does prevent claiming automatic diarization is complete.

### Visual intelligence

Separate spatial detail from temporal detail. High-resolution stills suit small text; short higher-frame-rate clips suit motion. Never invent detail through upscaling or interpolation. Derivatives cannot exceed the available source information. Preserve source time, actual presentation timestamp, dimensions, frame-rate metadata, source hash, recipe, and extraction version.

Use a cheap bounded analysis pass for scene changes, stable intervals, localized changes, and motion. Scene cuts alone miss typing, scrolling, small code edits, and animation. Add region-aware change metrics and suppress cursor-only noise where practical. Start with transparent heuristics; optional OCR/VLM reasoning is a later enhancement. Automatic choices are proposals, not deletion authority.

Proposed first limits, to verify against measured cost: still width up to 1920 without upscaling; a bounded source-resolution still option subject to decoded pixel limits; clips up to 30 seconds, 15 fps by default and 30 fps on explicit request when supported by the source; maximum 16 images per agent call; bounded scan/decode time, bytes, pixels, disk reservation, and per-user/project queue admission. Requests exceeding limits return a clear reduction suggestion. Baseline sparse frames stay usable when enhancement fails.

### MCP, skill, and packaging

Build an application-backed MCP surface and a reusable summarization skill. Package those together as a private plugin after the tool contract is validated; no new Sites hosting, universal-directory publication, or public deployment is implied.

The initial local client path is stdio with an account-scoped adapter to the existing API. Browser session cookies, worker credentials, and admin MFA cannot be repurposed as agent credentials. Design explicit user-granted project scopes, expiry/revocation, and optional separate derived-media permission. A remote Streamable HTTP endpoint requires its own OAuth/resource metadata and client compatibility work; OIDC browser sign-in alone does not supply that. Do not claim cloud ChatGPT connectivity to a private host without actual network/auth evidence.

Read-only tools return already authorized data. Generating a missing image/clip is a bounded derived-media job, not an unconstrained read. Distinguish it in permissions, annotations, progress, cancellation, and audit. Transcript edits, member changes, source upload/deletion, and report publication are unavailable in the first agent surface. No arbitrary paths, shell commands, input URLs, or outbound fetch tool.

Do not issue or store real credentials during unattended work without the owner's explicit approved mechanism and destination. Implement and test grant/revocation with disposable test identities; prepare a secure interactive connection step for the owner. This is a live-connection acceptance gate, not a reason to weaken authentication.

## Milestones and dependency order

| Milestone | Outcome | Tickets | Exit evidence |
| --- | --- | --- | --- |
| M0 | Contracts and measured model choice | P01–P02 | Stable timeline/auth decisions; real baseline scores or explicit model block |
| M1 | Speaker-attributed review | P03–P04 | Durable enrichment, manual correction, actual inference evidence |
| M2 | Marked visual evidence and adaptive extraction | P05–P08 | Point/range marks, screenshots/clips, playback and heuristic evaluation |
| M3 | Agent summary workflow | P09–P11 | Authorized MCP reads/extraction plus cited summary skill and packaging |
| M4 | Deployable candidate assessment | P12–P14 | Report/evidence export, integrated quality report, compatibility/security checks |

Critical path: P01 → P05 → P06 → P10 → P11 → P13 → P14. Diarization path: P02 → P03 → P04 → P13. Video UI P07 depends on P05/P06; automatic proposals P08 depends on P06. Agent grants P09 depends on P01 and gates P10. P12 consumes P10/P11 evidence contracts. These are dependency relations, not permission to begin unapproved writes.

## Ticket previews

All tickets are children of the proposed epic **“Recording intelligence: speakers, adaptive visuals, and agent evidence”**. All start in **Backlog**. Each implementation owner owns discovery, focused tests, and related fixes for that slice. Parent owns integration and final diff. Logical types are recorded in descriptions because the new project schema has Priority, Stage, and Assignee, but no Type field.

Epic preview: logical type Epic; parent none; project REC; Priority Major; scope P01–P14 and linked future backlog P15–P22. Acceptance: the complete core workflow operates with actual local inference and authorized evidence retrieval, manual overrides persist, and required compatibility/quality gates pass. Validation: P13/P14 evidence manifest plus a reproducible demonstration; production/private-recording/live-client gaps are recorded separately and do not receive acceptance credit. Rollback: disable optional intelligence/integration features while preserving original recordings and historical transcripts; installed upgrades remain operator-gated. The epic cannot be Done while a core gate is blocked.

### P01 — Define versioned timeline, enrichment, and evidence contracts

- Type: Task. Milestone: M0. Depends on: none. Priority: Major.
- Scope: recording revision/source hash; separate transcript, speaker, annotation, extraction and report versions; stable source-relative point/range references; capability/status endpoints; additive storage design and resource accounting. Preserve existing segment IDs and worker/publication contracts.
- Acceptance: old recordings remain readable with enrichment absent; stale updates fail predictably; API types distinguish requested and actual frame times, automatic/manual data, unknown speakers, unavailable source, and stale reports. Document manual-overrides precedence and per-role permissions before dependent implementation.
- Validation: contract tests for old/new payloads, invalid ranges/NaN, stale revisions and access denial; generated OpenAPI/frontend schema checked.
- Rollback: feature disabled returns existing transcript/audio/frame behavior. Schema changes first run only against disposable copies; no installed-database migration is authorized.

### P02 — Select and benchmark a local CPU diarization pipeline

- Type: Research task. M0. Depends on: none. Priority: Major.
- Scope: verify model/component licenses and access; pin ungated candidates; prepare a bounded public corpus with human reference labels, attribution and checksums; benchmark at two CPU threads under explicit CPU/RAM limits. Include a short single-speaker control and real multi-speaker/overlap examples. Separate tuning and held-out evaluation.
- Acceptance: actual inference outputs, DER components, speaker-count errors, runtime/RTF, peak RSS and exact environment recorded. Prefer Spanish and English natural conversations; if Spanish references cannot be obtained legally and ungated, report that quality gap rather than substituting synthetic speech as acceptance. At least five held-out real clips totaling 20 minutes for the initial smoke benchmark; this is not a representative production benchmark.
- Initial proposed gate: aggregate DER ≤25% with 250 ms collar/excluding overlap; also report zero-collar overlap-inclusive DER without hiding it. Correct speaker count on at least four of five clips, counting differences explained. Runtime ≤1.0 RTF and peak RSS ≤2 GiB in the declared two-CPU evaluation environment. These are pilot targets, not claimed results or Philemon guarantees. Freeze before final scoring; do not relax to pass.
- Validation: reproducible scorer tests with known RTTM examples, permutation-invariant labels and fixture durations; candidate manifest and raw outputs. A failed target produces a model-quality blocker, not a fabricated pass.
- Rollback: optional model dependencies stay out of the API/base ASR runtime. No gated download/term acceptance without the owner.

### P03 — Add durable recording-wide diarization enrichment

- Type: Feature. M1. Depends on: P01/P02. Priority: Major.
- Scope: optional post-ASR task, retained audio input, recording-wide clustering, word/turn attribution, bounded leases/retries/cancellation, independent SSE progress, resource scheduler and reproducible manifests. Existing recordings can opt in without ASR rerun.
- Acceptance: transcript stays usable if diarization fails; restart resumes safely; stale leases/results cannot overwrite current versions; speaker IDs are consistent across ASR chunks; overlapping speech and uncertain intervals are represented. Same source/model/config reuses a valid result. ASR and model inference respect the aggregate worker budget; do not load concurrent large models into the one-worker default without measured headroom.
- Validation: concurrency/retry/stale-publication tests plus real inference from P02; legacy boundary-reconciliation and saved-result retry regressions pass.
- Rollback: disable enrichment scheduling; retain existing transcript, labels and raw source without destructive cleanup.

### P04 — Review, name, and correct speakers in the transcript

- Type: Feature. M1. Depends on: P01/P03. Priority: Normal.
- Scope: minimal speaker badges/filter, rename, reassign selected turns/ranges and undo; optional speaker-count hint for reprocessing. No biometric identity recognition or cross-recording voice profiles.
- Acceptance: project viewers inspect, editors correct; renaming preserves text/timestamps/search; manual turn changes survive model reruns until explicitly reset; unknown/overlap display honestly; old unlabelled recordings still work. Keyboard/mobile controls remain usable with preset components.
- Validation: backend permission/conflict/history tests; disposable Chromium interaction for rename, reassignment, navigation, undo and viewer denial; frontend build/lint.
- Rollback: hide speaker controls; original text and correction history remain intact.

### P05 — Mark timeline points and ranges for visual evidence

- Type: Feature. M2. Depends on: P01. Priority: Major.
- Scope: point/range marks, optional short label, manual policy Auto / Readable still / Motion / Pin, revisioned edits, and source-relative links. Pin marks evidence protection; it does not authorize pruning.
- Acceptance: select from playhead/transcript, adjust start/end, inspect evidence state, edit/delete with undo/history; viewer/editor permissions enforced; zero/invalid/out-of-duration ranges rejected; audio-only recordings support notes but do not offer video extraction. Existing sparse references remain available.
- Validation: revision/permission/range tests; keyboard/touch selection and source-link browser checks; no changes to unrelated layout conventions.
- Rollback: disable new controls; annotations remain stored and existing player works.

### P06 — Extract bounded screenshots and short video clips

- Type: Feature. M2. Depends on: P01/P05. Priority: Major.
- Scope: durable derived-media jobs using retained source, high-detail stills, optional crop, short motion clips; validated generalized artifact manifests; decode/byte/pixel/time/queue/disk limits, content hashes, idempotency, cancellation, lease fencing and publication.
- Acceptance: source never becomes an arbitrary browser file endpoint; image/clip assets are registered atomically, correctly typed and authorized. Store actual PTS and source transform metadata; respect rotation/aspect ratio and VFR; no resolution/fps invention. Cache by source/recipe/version and survive restart; missing source gives explicit unavailable/restore-needed state. Failed extraction cannot corrupt existing artifacts or ASR.
- Validation: actual FFmpeg fixture runs for text slides, scrolling, fast transitions, VFR, portrait/rotation, corrupt input and interrupted jobs; path traversal/cross-project/range/byte-limit tests; bounded decode of a long input with useful error output.
- Rollback: stop new jobs and disable extraction; do not delete accepted media, originals or model files.

### P07 — Add synchronized clip playback and evidence navigation

- Type: Feature. M2. Depends on: P05/P06. Priority: Normal.
- Scope: play selected motion clips, inspect readable screenshots, map local clip time to source time, navigate/paginate all retained references, zoom to readable detail, jump between transcript/marks/frames.
- Acceptance: one active playback clock controls selection; audio does not double-play; seeking/stopping/changing recordings clears old state. Source-time offsets stay correct across clip boundaries, buffering and VFR inputs. Exact evidence timestamp differs clearly from current playhead. Minimal copy, preset components, responsive controls.
- Validation: disposable browser tests for synchronized seek, clip offset, cancellation, range requests/authorization, keyboard and narrow mobile width; build/lint.
- Rollback: retain existing audio-plus-still presentation as fallback.

### P08 — Propose adaptive visual quality and temporal coverage

- Type: Feature. M2. Depends on: P06. Priority: Normal.
- Scope: bounded low-resolution analysis; scene/stability/motion/local-change metrics; select a settled readable frame for static content and short denser clips for useful motion. Store score/reason, recipe version and budgets; allow accept/reject/override.
- Acceptance: code edits and scrolling are not dependent solely on full-scene cuts; cursor movement does not create unlimited clips; repeated static slides deduplicate while meaningful changes survive. Manual pins and policies win. Exhausted budgets fall back to sparse references and report partial analysis. No automatic source deletion or external vision calls.
- Initial quality gate: deterministic generated sequences with known transitions recover ≥90% of designated meaningful visual events within one second, remain within all byte/frame/decode caps, and never override a pin. Score text readability on known-text fixtures separately from event coverage. Add held-out openly licensed real demo/slides footage for measured generalization; do not claim perfect semantic importance detection.
- Validation: compare sparse baseline versus adaptive coverage/storage/runtime on frozen fixtures; record missed/false events and real-footage limitations; manual overrides and restart tests.
- Rollback: disable auto proposals; manually requested screenshots/clips stay available.

### P09 — Define and enforce account-scoped agent grants

- Type: Feature. M3. Depends on: P01. Priority: Major.
- Scope: authorization shared by API and MCP adapters; user/project-bound scopes for read and optional derived-media request; expiry/revocation; membership/disabled-user checks on every operation; audit without content/credentials. Prepare secure interactive client connection; no real credential provisioning in unattended execution without explicit mechanism/destination approval.
- Acceptance: no implicit administrator content access, worker-token reuse, cookie scraping, or elevation above user/project roles. Membership removal/revocation immediately denies later reads and artifact requests. Test identities only during automation. Explain stdio local connectivity and reserve remote OAuth integration for P15.
- Validation: cross-account/project enumeration, revoked grant, changed role, disabled user, malicious scope and media authorization tests; named independent GPT-6.1 review of the new agent trust boundary and affected fixes if delegation is approved.
- Rollback: disable agent endpoint/adapter and revoke its grants; browser accounts/MFA remain usable.

### P10 — Implement evidence-oriented MCP tools

- Type: Feature. M3. Depends on: P06/P09. Priority: Major.
- Scope: bounded/paginated `list_projects`, `list_recordings`, `search_recordings`, `get_recording`, `get_transcript`, `get_speakers`, `list_marks`, `list_visual_evidence`, `get_image`, plus separately authorized `request_visual_evidence` and `get_job`. Final names freeze with P01. Return structured metadata, native image content where supported, and source links.
- Acceptance: initialize/discovery/call/errors through an actual SDK client; bounded input/output/context sizes; transcript pages have stable revision/cursors; extraction returns job state rather than blocking indefinitely. Existing authorized images are viewable by the agent without unauthenticated public URLs. Clips include metadata/poster frames because generic MCP clients may not consume video natively. No arbitrary path/URL/shell or administration tools.
- Validation: protocol interoperability, images decoded by a client, pagination, invalid/stale cursors, cross-project access and job polling/cancellation limits; permission annotations match actual behavior. Local disposable principal only is labelled honestly until owner pairing is tested.
- Rollback: disable MCP adapter; shared underlying API continues serving browser users.

### P11 — Package a cited-summary skill and private plugin

- Type: Feature. M3. Depends on: P10/P04/P08. Priority: Normal.
- Scope: repository-owned skill, client connection examples without credentials, minimal private plugin manifest, summary templates for technical demos/meetings, tool orchestration and claim/evidence schema. Preserve `backend/`, `web/`, `docs/`, `scripts/`; use a clearly scoped integration directory under `scripts/` and documentation links.
- Acceptance: workflow resolves scope/language, reads enough transcript without silent truncation, inspects actual images, requests useful missing evidence, and produces summary/chapters/decisions/actions with timestamps and screenshot IDs. Owner/due dates are only asserted when supported; unresolved points stay explicit. Treat transcript/OCR as untrusted content, never instructions. Check revisions before final output and disclose missing or failed evidence. No invented visual observations. Default output is returned to the user, not silently saved or published.
- Validation: installed/package structure checks; scripted contract tests for tools and bounded context; adversarial fixtures for prompt injection, stale evidence and unsupported owner/date claims. Distinguish deterministic workflow tests from a real agent end-to-end run. No paid API key acquisition or charges without authorization; use the current authorized agent client when available, otherwise report live-client gate.
- Rollback: remove/disable optional plugin connection; no host-global plugin changes during unattended work unless explicitly approved.

### P12 — Export a portable cited report and evidence bundle

- Type: Feature. M4. Depends on: P10/P11. Priority: Normal.
- Scope: Markdown/HTML report plus selected images, transcript/speaker snapshot and JSON provenance manifest in ZIP. Saving a generated report is an explicit editor action with expected revision; draft and reviewed status distinguished. Local agent-generated files may precede in-app report storage if the end-to-end contract remains clear.
- Acceptance: report identifies source hash, transcript/diarization revisions, generator/template versions, claim citations and extraction recipes. Export contains only selected authorized evidence, bounded total size, safe names/relative links and no tokens/paths from the host. Escaped HTML and sanitized Markdown links; report becomes stale when relevant source annotations/transcript change. No source recordings included by default.
- Validation: open HTML and unpack ZIP in an isolated directory; verify citations/assets/checksums, malicious text/filenames, permission changes, stale report behavior and size caps; no zip-slip or external asset dependence.
- Rollback: disable report save/export; existing evidence and drafts remain readable.

### P13 — Execute integrated model and visual quality evaluation

- Type: Validation task. M4. Depends on: P03/P04/P07/P08/P10/P11. Priority: Major.
- Scope: freeze candidate/model/config identities; rerun affected held-out real inference corpus, baseline ASR regression, visual quality/budget suite, and agent summary workflow. Write machine-readable results plus an honest human-readable report and failure examples. Include cold/warm load, cancellation/restart and one-worker resource contention.
- Acceptance: report per-clip and aggregate diarization metrics with scoring policy; speech content unchanged by speaker edits; no recurrence of omitted boundary words; visual meaningful-event coverage/readability/storage/latency measured; summary claims linked to inspected evidence with unsupported-claim failures recorded. Never use the summarizer itself as sole ground truth. Exact citations/source integrity are automated; semantic summary quality requires labelled references or explicit human acceptance. Public English evidence does not prove Spanish/private-recording quality.
- Validation: execute real model binaries and FFmpeg under bounded containers, not mocks; preserve hashes, hardware/container limits, commands and logs outside Git. Stop runaway jobs, do not fill disk, and fail the model gate rather than lowering targets. Record missing Spanish/real-agent fixtures as incomplete evidence.
- Rollback: keep automatic features opt-in if their quality gate fails; report the blocker and preserve passing independent work.

### P14 — Qualify the local release candidate and handoff

- Type: Validation task. M4. Depends on: P12/P13 and all implemented core tickets. Priority: Major.
- Scope: final diff, complete backend suite and OpenAPI/schema checks, production frontend build/lint, affected browser and worker-isolation tests, model packaging/installer compatibility and disposable pre-change data upgrade test. Verify no recordings, weights, mutable data or secrets enter Git. Conventional commits per coherent passing slice if commit authorization is granted.
- Acceptance: evidence manifest binds checks to exact source/model/config/artifact identity. All consequential agent authorization/media/report findings resolved or left blocking. Candidate is explicitly one of: source complete, local runtime qualified, quality blocked, or ready for owner deployment assessment. Do not move tickets to Done based only on authored code. Preserve installation path `/home/argon/argon-recordings`, config/data/model identities and single-worker defaults in installer behavior.
- Validation: disposable old-version snapshot → new application start → historical account/project/recording reads → new feature exercise; no migration against installed data. Host resource and actual recording tests remain separate unless authorized later. No deployment runbook or production migration executes here.
- Rollback: disable new features and retain compatible evidence; if schema changes prevent safe source rollback, document that restriction and require backup restore using matched source/database. Never claim arbitrary old-code rollback is safe.

## Additional suggested tickets (not the first overnight scope)

All are children of the same epic, initially Backlog, Priority Normal unless stated. Their acceptance and validation below are previews for later approval.

| Alias / logical type / title | Dependencies and scope | Acceptance and validation | Rollback |
| --- | --- | --- | --- |
| P15 / Feature / Remote MCP OAuth and client connection | P09/P10; private Streamable HTTP, OAuth metadata/PKCE, approved client path; deployment remains separate | Cross-client auth/revocation tests and actual private network reachability; no browser OIDC or localhost pass promoted as remote proof | Disable remote listener; local client works |
| P16 / Feature / Chapters, decisions and action-item review | P11/P12; editable navigable chapters and evidence-linked decisions/tasks | Every item has citations; assignee/date unknown unless supported; reviewer edits retained; tests against labelled examples | Hide generated layer, preserve source |
| P17 / Feature / Search visible text and screenshots | P06/P08; local OCR indexed by time/region, code identifiers and Spanish/English queries | Search finds known on-screen strings absent from speech; OCR errors/provenance visible; permission and stale-index tests | Disable OCR index without touching transcript search |
| P18 / Feature / Project glossary and terminology | P01/P04; reviewed project terms, spelling/aliases and optional ASR hints | No silent historical replacement; provenance/version; spelling/search/reprocessing tests | Disable hints; text remains |
| P19 / Feature / Re-upload restoration | P01/P06; hash-matched source restore and explicit handling of changed sources | Identical source restores derivation ability without retranscribing; changed hash cannot inherit timestamps silently; restart/permission tests | Retain old evidence, reject mismatched source |
| P20 / Feature / Storage dashboard and retention preview | P06/P12/P19; source/audio/frame/clip accounting, projected savings and pinned evidence exclusions | Dry-run explains every proposed action and protects reports/pins; byte accounting/race tests. Destructive execution requires separate explicit approval | Preview-only, no deletion |
| P21 / Feature / Low-confidence review queue | P03/P08/P13; uncertain speakers, weak ASR, unreadable frames, missing citations | Targeted review jumps to evidence; confidence source distinguished from calibrated probability; labelled prioritization tests | Disable queue, retain data |
| P22 / Feature / Private report sharing with redaction | P12; reviewed redaction of transcript/images and explicit audience | Unredacted artifacts unavailable to shared audience; expiring access/revocation/cache tests and separate security gate | Revoke shares, private workspace unchanged |

Highest-value follow-ups: P16 (turn meetings/demos into useful outcomes), P17 (find code/errors/slides never spoken aloud), P18 (technical vocabulary), then P19/P20 (storage lifecycle). Avoid bundling all of them into an overnight promise.

## Board operation

Verified template columns are **Backlog → Develop → Review → Test → Staging → Done**, using custom field **Stage**. Keep this existing template rather than assuming a nonexistent State field. Board's current sprint was generated as “First sprint”. On ticket approval, attach the epic and all children explicitly to the board/current sprint where needed and verify membership through the board API; project membership alone is not proof that cards appear.

Proposed logical epic contains the objective, milestones, bounds and definition of done. Child descriptions include logical type, milestone, dependencies, scope, acceptance, validation, rollback and status. Use native dependency and parent links, plus priority. A blocked card stays at its actual Stage with a clear blocker in the description/comment; do not invent an unconfigured Blocked value. No public-deployment ticket in this epic.

Develop means active implementation; Review means diff/risk check; Test means executing required gates; Staging means locally qualified candidate awaiting remaining acceptance, not an actual deployed environment; Done means the ticket's declared acceptance/evidence passed. If no separate review is required, move directly to Test. Keep at most two independently owned implementation slices active when delegation is used.

## Proposed overnight execution contract

The owner approved this objective on 2026-10-23; the goal is active. P15–P22 remain future backlog only.

> Deliver and validate the first local Argon Recordings workflow for CPU speaker diarization, editable speaker labels, marked and adaptively selected visual evidence, and account-scoped MCP/skill summaries with screenshots and source citations. Execute P01–P14 in dependency order, including actual model/FFmpeg evaluation using licensed public fixtures and ungated weights. Preserve all existing transcription/retry/search/account/MFA behavior, the shadcn preset, and two-thread CPU Turbo. Keep the REC board accurate with evidence and blockers. Produce a conventional-commit candidate and reproducible handoff for the owner's deployment assessment. Do not deploy, alter production, provision secrets, accept gated terms, delete sources, or treat local/public-fixture tests as Philemon/private-recording acceptance. Stop at this goal; do not implement P15–P22 without expanding approval.

Execution priorities when runtime/quality uncertainty is material:

1. Resolve P02 model feasibility early while P01 proceeds. Spend no more than a bounded evaluation cycle on a failing model before recording a blocker and progressing independent work. Never hide missing actual diarization behind a stub.
2. Complete coherent slices; keep the existing app usable. First vertical proof uses one real public multi-speaker recording, a deterministic screen-demo fixture with known events, and actual MCP-client image retrieval.
3. Implement remaining core behavior and run the integrated gates. No fixed overnight completion promise: model downloads, hardware performance and actual defects determine progress.
4. Leave passing work reviewable and precise blockers if the full gate cannot be achieved. Do not mark the goal complete if required work remains.

Default is direct execution. If approved, at most two concurrent independent writers: parent/shared backend and a bounded UI/skill workstream after contracts freeze. Optional single independent GPT-6.1 review targets the new agent/media trust boundary; review budget is not a per-ticket ceremony. Workers never create/update YouTrack or secrets independently. Shared API/store changes remain under one owner.

Only bounded build/test/evaluation processes; no persistent dev server or installed service starts. Temporary test servers inside disposable test harnesses terminate on completion. No SSH or server administration is required for this local scope. Philemon read-only inspection requires a later explicit scope and uses the configured alias; no host limits assumed.

No clock duration, token budget, goal, automation, or overnight wakeup is implied by this draft. On approval, explicitly create the requested goal and choose any requested time/token bound. Stop-on-blocker means stop only the blocked dependency chain and keep doing authorized independent work; report an overall blocker when no remaining meaningful authorized work exists.

## Validation commands and evidence boundaries

- Backend: documented bounded Docker test invocation in README, `python -m pytest -q backend/tests`, with `PYTHONPATH=backend` as appropriate. New model-specific runtime tests run in their pinned disposable image with the selected model mount and explicit CPU/memory limits.
- Contract: `python scripts/export_contract.py --check`; regenerate intentional OpenAPI changes and `web/src/lib/schema.d.ts` using the documented isolated code-generator versions, then check again.
- Frontend: `npm ci` when needed, `npm run build`, `npm run lint` in `web/`.
- Browser: bounded disposable `scripts/browser-check.mjs` and affected account/MFA harnesses, extending only for meaningful new flows.
- Worker isolation: existing targeted tests plus actual packaged resource/mount/network checks when worker/model topology changes. No API access to diarization/transcription weights or writable worker directories added inadvertently.
- Quality: pinned dataset/model recipes; actual inference, reference scoring, raw timing/RSS, source/PTS visual checks and summary evidence checks. Preserve logs/artifacts outside Git; commit scripts, manifests without media/credentials, and concise aggregate results only.
- Reuse passing evidence until the relevant diff/config/model/environment changes; rerun affected gates after fixes. Final combined artifact needs combined integration validation.
- Three separate reports: automated source tests; real local inference/quality results; unresolved owner/private-recording/Philemon/live-client acceptance. “Ready to deploy” requires the latter gates to be resolved in the proper deployment scope, not just successful npm build.

## Sources consulted for the design

- OpenAI skills: https://developers.openai.com/plugins/concepts/skills — skills complement MCP tools with repeatable workflow guidance.
- OpenAI packaging: https://developers.openai.com/plugins/build/plugins — verify the chosen manifest format against target host during P11; do not assume all hosts accept the same variant.
- MCP tools: https://modelcontextprotocol.io/specification/2025-11-25/server/tools — structured outputs, image content and tool interfaces.
- MCP authorization: https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization — HTTP authorization and stdio transport distinction.
- Pyannote Community-1: https://huggingface.co/pyannote/speaker-diarization-community-1 — CPU/offline use, overlap/exclusive output, access conditions.
- Sherpa-ONNX diarization: https://k2-fsa.github.io/sherpa/onnx/speaker-diarization/index.html — segmentation/embedding model options; individual weight licenses still require verification.
- VoxConverse candidate corpus: https://github.com/joonson/voxconverse — reference annotations and dataset access/license must be verified before the evaluation fetch. Corpus selection is not completed by this plan.
- YouTrack project API: https://www.jetbrains.com/help/youtrack/devportal/resource-api-admin-projects.html — project created using the Kanban template and then verified through API.
