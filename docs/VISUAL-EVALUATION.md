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

## Source timeline qualification

### Source timeline regression (2026-10-26)

Actual FFmpeg fixtures exposed two errors for nonzero-origin media: a four-second MP4 starting at PTS 5 reported format duration 9, and analysis with accurate input seeking plus `copyts` discarded the beginning and truncated the end. The visual probe now uses the latest audio/video stream end minus the container origin when all timed streams have durations; Matroska's segment-end duration is normalized separately. Other fallback format spans are preserved. Analysis seeks without automatic accurate-seek trimming and explicitly trims on retained source PTS before sampling, without output `-t` interpreting a different clock.

Regression fixtures compare original and copied MP4/Matroska media: identical extracted JPEG hashes, identical complete-scan intervals and proposals, and rejection past the real end. Longer audio tails and delayed video tracks retain their recording-relative offsets. This checkpoint also identified a single-GOP MPEG-TS seek failure, addressed by the bounded recovery checkpoint below. These checks do not qualify real-footage quality.

The Matroska behavior is supported by FFmpeg's [muxer duration calculation](https://ffmpeg.org/doxygen/5.1/matroskaenc_8c_source.html#l02820): it retains the maximum packet timestamp plus packet duration. No universal subtraction of `start_time` from every format is assumed. Historical cached analysis results retain their original status and are not rewritten by this correction.

Validation: **245 backend tests passed**, including prepared E5-large and actual FFmpeg; OpenAPI remained unchanged and passed its check. The affected Chromium suite passed real analysis worker publication/review, two-tab decisions, mobile controls, report review/staleness and ZIP/offline evidence checks with no console errors. Browser artifacts are retained under ignored `models/validation/timeline-browser/`. This adds source-timeline fidelity evidence, not model-quality acceptance.

### Bounded transport-stream recovery (2026-10-26)

The reproduced single-GOP MPEG-TS fixture now extracts the exact original JPEG and a clip with identical decoded pixels, frame count and relative PTS after remuxing. When ordinary seeking yields no frames, a packet-capped prefix probe and PTS-trimmed decode retry from the beginning. Admission retains the 120-second prefix ceiling, 5,000-frame and 1.5-billion-pixel budgets, metadata readiness and subprocess timeouts. Over-budget prefixes fail before output creation, and insufficient clip coverage is not published. This qualifies this bounded fallback, not arbitrary seeking in every transport stream or long-GOP source.

The comparison exposed two additional errors. Probe intervals now use an absolute end because a relative duration is measured from the actual seek landing, as specified in the [official ffprobe interval contract](https://ffmpeg.org/ffprobe.html#Main-options). Clip selection tolerates only nanosecond floating-point noise at cadence boundaries. Actual inter-frame PTS replace container average rate as the over-rate check; otherwise a valid five-frame 5-fps clip with a short final packet was rejected. Tests include one-frame output and rejection of repeated, reversed or excessively close output timestamps.

Validation: **253 backend tests passed**, zero skipped, with actual FFmpeg and prepared E5-large; OpenAPI passed. The focused extraction suite passed 28 tests, and affected visual/adaptive/report Chromium passed with no console errors. Browser evidence is retained under ignored `models/validation/media-browser/`. The real-footage, readability, model-quality and installed-host gates below remain open.

## Remaining evidence gaps

- One CC BY-SA real desktop screencast has now been evaluated: 11/11 derivatives rendered, coarse temporal coverage 6/8 versus sparse 2/8, measured encoded bytes and OCR phrase recovery 2/10 at native size versus 0/10 at 480 px. These results do not establish semantic event recall, representative generalization or human readability. See [PUBLIC-VISUAL-EVALUATION.md](PUBLIC-VISUAL-EVALUATION.md).
- A validated readability evaluator and representative human acceptance remain missing; OCR is a separate evaluation tool and is not part of the application.
- Acceptance/rejection, pin precedence and durable restart now pass integration tests; representative recording acceptance remains pending.
- Real-fixture encoded costs are now measured; matched-coverage storage savings, deployment egress and broader playback/readability quality remain unqualified.
