# Cited reports and evidence exports

Local P12 implementation, owned by Argon. No deployment, real agent grant or plugin installation is implied.

An agent returns a cited summary by default. When the user requests an importable report, the packaged `recording-summary` skill uses the shared `argon-summary-v1` contract to prepare `report-draft.json`. The editor opens the matching recording, switches to **Reports**, opens **More options**, chooses **Import draft** and **Save draft**. Saving verifies the current source hash, transcript/speaker/annotation/extraction revisions, report-list revision, canonical cited passages, registered image times and inspected-image SHA-256. The submitted draft cannot choose a reviewed state. Checking is optional: **Mark checked by me** records that an editor read the saved draft; it does not run another AI check or unlock features. Rename changes the title with a collection revision fence and keeps source evidence and check state. Stale reports cannot be newly reviewed. Viewers can inspect and export existing reports but cannot import, save or review them.

The helper and API use the same canonical `backend/argon_recordings/report_contract.py`; the private plugin builder includes that module beside its CLI wrapper. Use the prepared interpreter and actual generator metadata:

```sh
python scripts/agent-integration/plugin/skills/recording-summary/scripts/summary_contract.py \
  draft report.json evidence.json current-reference.json \
  --title "Recording summary" --generator-name "Connected agent" \
  --generator-version "actual version or unspecified" --model "actual model or unspecified" \
  > report-draft.json
```

This produces a local artifact, not an application write. No report save/review tool is added to the MCP grant. A source change requires reconciling the claims and evidence before preparing a new draft; replacing its reference alone is not semantic validation.

## ZIP contents and provenance

**Download report** saves one offline HTML document with images embedded beside the relevant content, source passages linked within the document, and responsive print styles. Open it in a browser and print/save as PDF. No app connection or external assets are needed; clips remain timestamped references, with playback in the app.

**Report details → Download evidence ZIP** saves the full provenance bundle:

- `report.md`, `report.html` and structured `report.json`, with claims and exact source intervals.
- `transcript.json` and `transcript.html`, containing only cited passages; HTML citations target local passage anchors.
- `speakers.json`, containing the scope-filtered automatic turns, manual overrides, relevant labels, model identity and stale/uncertainty provenance. Human labels are assertions rather than verified identities.
- `images/…jpg`, containing exactly the selected acknowledged screenshots, under generated safe names. Original recordings, audio, video, unrelated images and database files are excluded.
- `manifest.json` (`argon-evidence-bundle-v1`), with project/recording/source identity, source and report revisions, template/generator versions, review state, extraction recipes/manifests, selected-media hashes, and SHA-256/size for every payload file. The manifest itself is excluded from its checksum list to avoid a recursive digest.

HTML and Markdown expose provenance directly as well as through the manifest. HTML is static and escaped, uses a restrictive CSP and local image/citation paths, and needs no script, font service or external asset. Markdown escapes source markup; its generated evidence links use fixed relative paths. Neither filenames nor hrefs use caller-supplied titles or URLs. No host storage paths or credentials are generated into the export.

Snapshots remain immutable when source text or speaker/mark/extraction revisions change. The app then displays **Stale** and retains the historical text. An export can preserve that stale snapshot when its selected registered media still matches. The manifest records the reference and time used for the export's freshness comparison; it does not promise that the live source will remain unchanged after download. Missing or altered selected media produces an explicit unavailable/integrity error rather than silently substituting new pixels. Restoring the original recording is unnecessary when all selected exported media remains intact.

Coverage, generator metadata and actual image inspection are importer assertions. Backend validation proves references and literal assignment support, not complete reading, actual viewing or semantic truth. **Reviewed** means an editor explicitly chose that state; it is not an automated quality score. Report import does not mutate the transcript, speaker identities or evidence marks. Claim editing/chapters/task workflows remain the separately scoped P16 backlog.

## Bounds and lifecycle

Import JSON is limited to 1 MiB; captured metadata to 4 MiB; at most 16 selected JPEGs of 4 MiB each; and a ZIP to 64 MiB both before compression and encoded. A recording retains at most 100 reports. Two exports may run concurrently per API process, with configured free-space reserve checks. Export work uses a private temporary file and closes it on completion, cancellation or failure; it is not a persistent derivative cache. No retention/pruning or original deletion is introduced.

Report collection revisions fence save/review races independently from source-evidence versions. SSE refreshes status/staleness in other clients. Current project/account permission applies on every list, detail, save, review and export request; previous export authorization does not authorize a later download after access removal. A downloaded local ZIP is an independent copy under its recipient's control.

## Executed evidence and remaining gates

Six focused backend tests cover explicit draft/review, optimistic and stale conflicts, immutable historical snapshots, selected media checksums, malformed IDs, escaped hostile title/source/generator text, safe names, request/snapshot/archive caps, cleanup after failures/forced ASGI disconnect and viewer/revoked-access denial. The published SDK integration also reads actual evidence through MCP, explicitly saves/reviews through a disposable browser account and verifies the resulting ZIP and JPEG bytes.

Chromium exercised import/review/export, cross-tab review/staleness, 320/390/768 px controls, unpacked ZIP checksums, opening offline HTML and its actual JPEG, local transcript anchors, and hostile source strings without execution or external requests. The full backend suite passed **239 tests** with prepared E5-large and real FFmpeg; OpenAPI passed. The SDK/backend/package suite passed **25 tests**, including importable-draft CLI generation and real MCP → explicit browser report → ZIP. Accounts/MFA Chromium passed, including hidden report-write controls for viewers. An independent GPT-6.1 review and focused cleanup/provenance follow-up returned no material findings. Frontend production build/lint and the affected visual/report Chromium workflow passed.

These checks use disposable synthetic accounts/media and prove local behavior. Representative semantic summary quality, real-footage readability, owner pairing, installed Philemon/old-data upgrade and release qualification remain P13/P14 or separately named acceptance gates.

## In-app generation and video references

The Reports tab now also creates OpenAI-generated drafts and separate refinements, alongside the existing import/review/export workflow. Report v2 adds registered clip references for in-app playback while exports retain text references rather than video bytes. See [in-app report generation](AI-REPORTS.md) for source/inspection validation, runtime configuration, accounting and qualification boundaries. The historical checks above describe their original checkpoint; they do not establish deployment of this addition.


## Agent-authored report blocks (v3)

`argon-summary-v3` adds an ordered `blocks` array. Each block has `id`, `kind`, `title` and `item_ids`; content continues to live in validated, cited items. Widgets are declarative, with no arbitrary HTML, scripts, remote URLs or executable components:

- `narrative`: connected explanation and its evidence/media.
- `key_points`: concise list with expandable evidence.
- `actions`: owner/deadline table with expandable evidence.
- `calendar`: deliverables placed on literal ISO dates (`YYYY-MM-DD`); relative, ambiguous or missing dates remain in **Dates to confirm**. Desktop month grid becomes an agenda on mobile. Export retains an accessible deadline table.
- `figure`: inspected source images embedded with their explanation and citations; uninspected images cannot be used.

Each block references known item IDs once across the layout. Schedule blocks accept only actions; figures require image evidence. Items omitted from blocks still render, so no validated content silently disappears. v1/v2 drafts remain readable and importable. Agents receive the widget palette in the writer schema/instructions and choose blocks for the requested task. Refine an older report to request a new layout; source snapshots are retained.

Visual and custom report presets allow visual tools by default, with a user control to disable them. Summary/action presets stay text-first for cost; users can enable visuals. Media is optional and selected for explanatory value, not decoration. Useful screenshots are fetched from the recording, inspected, and cited before publication.


## Report management and inline media

Editors can use **Manage report → Archive report** to hide a report from the default list, **Show archived** to find it, and **Restore report** to return it. Archive is reversible. **Delete permanently…** opens an explicit irreversible-action warning; **Keep report** cancels. Confirmation permanently removes that report from the application, with optimistic collection-revision checking. It preserves the recording, transcript, shared source media and usage receipts; previously downloaded exports and existing backups are independent copies. Active refinement must finish or be cancelled before its input report can be deleted. Viewers cannot archive, restore or delete.

Figures are visible inline even inside key-point, action-table or calendar blocks; only supporting textual evidence is collapsed. HTML exports embed JPEG bytes in those same visible sections. Generation waits for requested visual jobs to finish instead of abandoning them after 90 polls, and remains cancellable while waiting. A failed/cancelled extraction is disclosed rather than illustrated with an uninspected image. Trace metadata records visual job/state and scheduling rejection status, without raw errors.

Adding derived media to the catalog does not itself stale an existing report. Source hash, transcript, speaker labels, annotations and scope still determine source freshness; selected image bytes and registered clip manifests remain checked for save/export. Historical reports with no selected images need a new generation/refinement to gain figures; existing saved contents are not silently rewritten.
