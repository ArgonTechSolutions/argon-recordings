# Visual proposal analysis evaluation

Status: local runtime, durable job/API integration and browser review are implemented and tested. No proposal is automatically accepted, extracted, pinned, or used to delete a source. Real-footage generalization and text readability remain separate gates.

## Runtime contract

`backend/argon_recordings/visual_analysis.py` exposes:

```python
analyze(source: Path, *, start: float = 0.0, end: float | None = None) -> dict
```

The worker supplies a trusted retained local source path; this runtime accepts no URL, arbitrary output name, or external vision endpoint. Its subprocess form is:

```sh
PYTHONPATH=backend python -m argon_recordings.visual_analysis \
  --source /trusted/retained/source.mp4 \
  --output /worker/job/analysis.json [--start 0] [--end 300]
```

The result contains source-relative requested/scanned/decoded intervals, scored proposals (`image` or `clip`), short human-readable reasons, transparent motion/change/stability metrics, recipe versions, bounded-resource measurements, and `complete`/`partial` status. Decoded timestamps retain container-origin and delayed-video-track offsets, aligned to the recording timeline. A proposal is a review suggestion only. If the scan is truncated, times out, decodes no frames, or produces no adaptive candidates, the result provides a sparse fallback at 60-second spacing (up to 32 points) and identifies any incomplete coverage.

The analysis pass samples at 2 fps and 256×144 grayscale. It compares 24×14 spatial cells, combines immediate and persistent localized changes, detects larger scene changes, groups related changes, and suppresses small transient changes consistent with cursor/blink activity. Static-scene signatures deduplicate repeated slides; localized edits bypass slide deduplication. A still proposal waits for two nearly unchanged samples when possible. Sustained motion yields a bounded short-clip interval. Scores are heuristic rankings, not calibrated probabilities or claims about semantic importance.

Limits are 300 seconds scanned per invocation, 602 sampled frames, 22,228,992 decoded analysis bytes, a 45-second wall-clock limit, 256×144 pixels, at most 48 adaptive proposals, and 32 sparse fallback points. FFmpeg/ffprobe reuse the P06 `file,pipe` protocol and media-format whitelists. No OCR, VLM, outbound fetch, source deletion, or derivative creation occurs here.

## Frozen generated-fixture evidence

The candidate was calibrated on a 30-second synthetic screen sequence with different transition times and patterns. After calibration, a disjoint 40-second fixture was frozen with four designated events: slide appearance at 3s, a code-area edit at 10s, scrolling at 16s, and a slide/demo transition at 24s. The held-out fixture used different pixel patterns, event locations, timing, colors, and scrolling motion; no thresholds were changed after freezing it.

Under the disposable `argon-recordings-worker:local` image (`sha256:fe1090143d788076ae9059b36c399baf423695c55b0d4a357d7c4e1a20e2baf5`; FFmpeg 5.1.9-0+deb12u1), with 2 CPUs and 2 GiB memory, the frozen fixture recovered **4/4 events (100%) within one second**, with **0 missed**, **1 unmatched proposal**, and **2 extra proposals near already-recovered events**. Proposal timestamps were 3.5s, 10.5s, 15.5s, and 24.5s; the extra candidates duplicate opening/transition evidence. One repeated static slide was deduplicated. Cursor movement produced 67 suppressed transition samples.

The scan decoded 80 frames and 2,949,120 analysis bytes in 0.267 seconds; it stayed below 602 frames, 22,228,992 bytes, and 45 seconds. The sparse baseline for a 40-second recording would contribute one reference at the configured 60-second spacing. Adaptive analysis returned seven proposals, including the duplicate and unmatched candidates. This compares coverage candidates and analysis cost only: it did not render derivative assets, so derivative storage/egress savings were not measured.

These are deterministic generated pixels, not semantic review labels. The fixture establishes that the chosen basic transitions, a localized edit, and scrolling can be found within the stated time tolerance while cursor noise is suppressed. It does not establish text readability or reliable visual importance ranking.

## Reproduction

The tests generate their MP4 fixtures directly with FFmpeg and do not download or retain media:

```sh
python3 scripts/dev_container.py --timeout 180 visual-eval -- \
  --cpus=2 --memory=2g --network=none \
  -v "$PWD:/workspace:ro" -w /workspace -e PYTHONPATH=/workspace/backend \
  argon-recordings-validation:local python -m pytest -q -s -p no:cacheprovider \
    backend/tests/test_visual_analysis_ffmpeg.py backend/tests/test_analysis_jobs.py
```

The development fixture is used only by the calibration regression. The held-out test asserts event recovery, motion-to-clip selection, cursor suppression, score/reason presence, and the budget ceilings. Runtime and actual API→visual-worker publication tests live in `test_visual_runtime_ffmpeg.py` and `test_visual_worker_ffmpeg.py`.

## Integrated review checkpoint (2026-10-26)

The shared worker now executes the analysis subprocess through durable source-bound jobs. Source checksums, worker identity, expiring/retried leases, cancellation, interval/version/result budgets and idempotent publication are checked before accepting results. Analysis and extraction share queue limits, free-space reservation and ASR worker capacity. Cache identity uses the source hash, requested interval and analysis recipe independently of transcript edits.

Editors can analyze an explicit interval, inspect the heuristic reasons/scores, keep a proposal as a normal editable/undoable mark, or reject it. Pinned and non-auto-policy marks block overlapping acceptance and are never modified by analysis. Acceptance and rejection advance annotation state so other clients refresh through SSE. Repeating an accepted decision after undo does not recreate the removed mark. There is no automatic extraction or pruning.

The final local regression passed **233 backend tests** with real FFmpeg and prepared E5-large, plus the OpenAPI check. The frontend production build/lint passed. Chromium exercised real analysis worker results, two-tab acceptance/rejection/undo updates, cancellation and 320/390/768 px layouts alongside existing visual playback/extraction. Console errors were empty. The existing MCP/summary/package suite passed **24 tests** against this integrated backend.

Integration exposed a fractional-time coverage bug: calculating scan coverage from `requested_start + frame_count / fps` disagreed with quantized source PTS. The runtime now separates the completed scan interval from actual decoded PTS, excludes samples outside the selected interval and clamps rounded proposal times to its bounds. Actual FFmpeg tests cover fractional and sub-frame intervals, long motion episodes capped at 30 seconds, partial fallback, API publication, manual precedence, lease takeover, isolated-worker identity, restart and viewer/outsider denial.

These are functional/generated-fixture checks. Historical timings above are from the named original image; the current regression uses the reusable `scripts/Dockerfile.validation` image and does not requalify runtime performance under isolated host load. Local browser evidence is retained under ignored `models/validation/analysis-browser-final/`.

## Remaining evidence gaps

- No openly licensed real screen demo/slides fixture was selected for this bounded slice. Real-footage generalization is unverified; no production-quality claim is made.
- Known-text readability is not measured because this heuristic runtime performs no OCR.
- Acceptance/rejection, pin precedence and durable restart now pass integration tests; representative recording acceptance remains pending.
- The seven generated proposals are not rendered screenshots/clips, so actual encoded size, playback quality, and storage impact remain unmeasured.
