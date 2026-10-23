# Source-derived visual evidence

Local implementation only. Public deployment and installed Philemon qualification remain separate.

Open a processed video, expand **Extract evidence**, choose a screenshot or a clip, and select a source time or timeline mark. Screenshots offer 1280/1920 px or bounded source detail; the worker never enlarges source pixels. Clips are muted and follow the original audio playback clock, including source offsets, seeking, pause and speed. Screenshots can be enlarged for inspection. **More frames** exposes the remaining sparse references.

The source stays private. Browser viewers can inspect registered evidence; editors and owners can request/cancel extraction. A request never accepts a filesystem path, URL or output filename. Authorized media routes support byte ranges. Annotation pins do not authorize deletion.

## Limits and lifecycle

- Images use one selected frame; clips are positive intervals up to 30 seconds, sampled at up to the requested 1–30 fps without synthesizing frames. A source's available rate remains the ceiling.
- Standard width is at most 3840 px. Source-detail screenshots remain bounded by 8192 px per output axis and 16,777,216 decoded source pixels. Normalized display-space crop coordinates must fit the source.
- Input limit: 8 GiB. Runtime output limits: 20 MiB per image and 64 MiB per clip; protocol hard limit: 128 MiB. Source/frame/decode/time limits can require a shorter clip even within the 30-second limit. Errors suggest reducing the range or image size.
- Admission permits eight active tasks per user/project and 32 globally, with 128 MiB reserved per active task plus the configured free-space reserve. A recording retains at most 100 task identities. No automatic media pruning is introduced.
- Tasks use the same worker capacity as ASR/corrections/diarization, 120-second leases and at most three attempts. The worker releases its loaded Turbo model before video extraction. Stale or cancelled leases cannot read the source, upload or publish. Cancellation stops the subprocess and FFmpeg descendants.
- Cache identity includes recording, source hash, extraction version and recipe. Speaker/transcript/mark edits do not invalidate unchanged source pixels. Completed, intact media can be reused without the original. Missing originals require restoration for new extraction; missing derivatives return an explicit unavailable response and can be requested again.

Publication verifies media size/hash/type and bounded metadata before registering it and incrementing the extraction revision in one database transaction. Existing ASR artifacts and transcripts remain intact after extraction failure. Scratch output is lease-specific; it is never a public arbitrary-file endpoint.

## Timing and fidelity

Manifests distinguish requested time from actual source frame PTS, and retain source hash, dimensions, crop, sampling, rotation, sample/display aspect ratio, source origin and recipe version. Container-origin normalization aligns output with the original audio/player timeline, including a delayed video track. Output normalizes non-square pixels by shrinking an axis, preserving display shape without inventing spatial detail.

The disposable FFmpeg suite exercises real extraction/publication, VFR, crop/rotation/aspect, nonzero origins, delayed tracks, corrupt input and budgets. Chromium checks real output media, source-offset synchronization, queue cancellation and mobile controls. These are functional checks; text readability, adaptive-event generalization, private recordings and installed-host performance require their own evaluation evidence. The agent summarization layer must inspect actual images before making visual claims.
