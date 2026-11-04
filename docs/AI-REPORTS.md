# In-app report generation

Argon Recordings has a separate **Reports** tab for each ready recording. A selector opens one saved document at a time. **New** offers Summary, Action list, Visual report and Custom request, with optional focus, language, visuals and source range. **Refine** supplies the selected draft as context and creates a separate report; it never overwrites or automatically reviews the original. Stale reports require fresh generation.

Custom request requires **What should this report do?**, independently of the optional **Anything to focus on?** field. Submission shows Sending, then queued/running status; completion opens the saved draft. Rejected submissions keep the modal and its inputs with an error. Background failures remain visible with the estimated API usage and an explicit edit/retry action; they do not automatically repeat paid calls. Loading and action failures are shown in the Reports panel.

Time inputs accept fractional source seconds without rounding the recording duration. The form reports invalid ranges visibly, including when the range controls are collapsed. This avoids native hidden-field validation silently blocking recordings whose duration is not a multiple of 0.1 seconds. The same precision applies to speaker ranges, marks, visual extraction and analysis ranges. Passage-boundary expansion preserves the requested video bounds, including silence before or after speech.

Desktop shows a compact source panel beside the document. Mobile starts with the source collapsed above the document; a citation opens it at the referenced moment. Source audio and report clips pause each other. Screenshots appear alongside the sections that cite them. Existing transcript correction, speakers and visual tools remain in the Transcript tab. Import is under More options.

## Execution and evidence

The coordinator calls the OpenAI Responses API (`gpt-6.1-sol`, standard service tier, low reasoning effort, `store=false`). No API key is sent to browsers or transcription workers. The provider boundary is implemented using the already pinned HTTPX dependency. No remote MCP exposure or public recording URLs are required.

The model can read canonical transcript pages, list registered visuals, inspect actual JPEG pixels, and request screenshots or clips through the existing bounded visual worker queue. Every operation is scoped to the recording and rechecks current requester membership. Visuals can be disabled for a run. Images sent to OpenAI are logged by registered ID/hash, not by pixels or storage paths, in the local run's tool trace. Source text, images, speaker labels and previous reports are untrusted data, not instructions.

The API verifies source/transcript/speaker/annotation revisions, canonical passages, supplied-image IDs/hashes, scope, explicit assignment quotes and report collection revisions. Extraction versions can advance during a run so its own derived media can be used. The final report must reference the current extraction snapshot. Generated reports are always drafts. Review remains an explicit editor action and is not a model-quality guarantee.

A model-supplied `inspected` assertion alone is insufficient: image citations must belong to the set actually supplied to that run's model requests. Full transcript coverage requires all scoped passages to have been retrieved. These controls establish evidence availability and structural/literal consistency, not semantic truth or proof of model attention.

`argon-summary-v1` remains accepted. `argon-summary-v2` adds up to eight `video_references`, each attached to a report item and bound to a registered asset, source hash, media SHA-256 and actual interval of at most 30 seconds. Clip playback checks the saved hash and current project access. Video references are playback aids; the model assesses still frames and transcript, not native video. ZIPs include the references, captions and intervals, but omit video bytes. Selected JPEGs retain the existing portable export behavior.

## Runtime configuration contract

- `OPENAI_API_KEY`: private coordinator process environment, or
- `RECORDINGS_OPENAI_KEY_FILE`: private regular file, up to 4096 bytes, containing the raw key or one `OPENAI_API_KEY=...` assignment. Symlinks and group/world-readable files are rejected. The coordinator process must be able to read the file. The application does not automatically load developer env files.
- `RECORDINGS_AI_BUDGET_USD`: application-wide estimated USD allowance, default **10**, range 0–1000. Zero disables generation. The accounting period is the UTC calendar month, independent of the provider's credit renewal date.

This feature is disabled without a key. The Docker installer accepts an opt-in `[ai]` table in `recordings.toml`: `key_file` is a private existing file path (relative to the installation directory or absolute), and `budget_usd` defaults to 10. The file is bind-mounted read-only at `/run/openai/key` only in the API container; only its path and budget are environment metadata. Worker-only configurations reject `key_file`. The installer never reads, creates or changes the secret file. A rootful Docker deployment uses container UID 10001, so the operator must provision a private file readable by that UID; rootless deployments require their corresponding UID mapping. No secret mount is added by default. Deployment configuration and server secret placement are separate operator actions. Env files are excluded from Git and Docker build contexts.

## Bounds and recovery

Two jobs may be queued/running across the instance, one per recording. Each reserves **$2** atomically against the allowance, then reconciles estimated cost using provider token usage. A conservative precharge is recorded before every paid call and retained when its outcome is uncertain. Interrupted calls are never automatically replayed. Unused reservation is released on completion, cancellation or failure; interrupted work is marked failed at restart, while queued work can resume. Reviewed reports and transcription data remain untouched.

Each run has a six-minute deadline, at most twelve model requests and 32 tool calls, 6000 output tokens per request, at most two format/evidence correction rounds, an 80 KB text-history bound, eight inspected images and eight requested visual derivations. Transcript ranges expand to containing passage boundaries for canonical quotations. Choose a shorter range when these limits prevent complete analysis. The provider response is bounded to 2 MiB; each image retains the existing 4 MiB bound. Visual requests wait at most 90 seconds each within the overall deadline. Cancellation stops further model work and draft publication; an already shared worker derivation may finish independently.

Estimates use $2.50 per million input tokens (including cache-write premium conservatively) and $10 per million output tokens for the fixed model, checked against the [official model reference](https://developers.openai.com/api/docs/models/gpt-6.1-sol) during implementation. This ledger is not the OpenAI invoice, credit balance, or an organization-wide spending limit. Requests outside this application are not included. Pricing changes require revalidation of these constants.

## Validation boundary

`backend/tests/test_report_generation.py` substitutes only the provider transport and exercises the real routes, storage, tool inputs, image bytes, reports, budgets, permissions, cancellation, refinement and export paths. `scripts/report-browser-check.mjs` uses the real application with the explicitly synthetic upstream in `scripts/report_browser_app.py`; it exercises generation, mobile layouts, document switching, source navigation and cancellation without paid requests. The browser fixture is not an application entrypoint.

The existing visual-browser check retains real FFmpeg fixture publication, cross-tab review/staleness and offline export checks. A separate live smoke check used a tiny synthetic English transcript, created a cited action draft using the real OpenAI API and recorded estimated cost around $0.01. A second real API smoke check supplied an actual synthetic color-pattern JPEG through the image tool; the model described the visible bars/stripe and saved one selected image in an unreviewed cited draft, at an estimated $0.016 for that successful run. Representative private-recording quality, adaptive selection on real footage and deployment acceptance remain separate from these local checks.
