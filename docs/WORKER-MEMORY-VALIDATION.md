# Repeated worker memory measurements

Two bounded six-cycle experiments completed on application commit `e62b43d`. Each alternates real 30/60-second Turbo transcription with an actual visual-worker extraction in the same process. The no-swap run fits the tested four-GiB budget and shows no continuing resident-memory increase across these six cycles. No application or installed configuration change was needed for this result. The overall candidate remains quality blocked.

## Method

`scripts/check_worker_memory.py` uses the frozen public audio/video inputs from [ASR-REGRESSION.md](ASR-REGRESSION.md) and [PUBLIC-VISUAL-EVALUATION.md](PUBLIC-VISUAL-EVALUATION.md). Input sizes were fixed before inference: 30, 60, 30, 60, 30, 60 seconds. Every transcription is real CPU INT8 Turbo with two threads, one model worker, English, VAD and word timestamps. Each visual task goes through the real API queue and `visual_worker.run`, which clears the model reference before actual FFmpeg extraction. The next ASR call must reload it.

The TestClient API shares the measurement process. The visual-ready fixture is seeded with a real source/reference frame; no speech inference is claimed for that setup. Scheduling and restart are qualified separately in [WORKER-LIFECYCLE-VALIDATION.md](WORKER-LIFECYCLE-VALIDATION.md).

Both runs use two CPUs and four GiB RAM. The first allowed four GiB swap and showed swap use; the second explicitly sets `--memory-swap=4g` alongside `--memory=4g`, matching the installer's disabled-swap policy. Actual cgroup `memory.swap.max` was verified as 4,294,967,296 and zero respectively. Hash-locked inference installation and `pip check` passed in both temporary containers.

## Results

| Measure | Swap allowed | Swap disabled |
| --- | ---: | ---: |
| Completed ASR → video cycles | 6/6 | 6/6 |
| RSS after video, cycles 1–6, MiB | 1021.0, 1569.3, 1623.6, 1605.1, 1616.0, 1572.2 | 1070.2, 1325.5, 1327.1, 1200.4, 1191.0, 1190.4 |
| Process peak RSS, MiB | 2285.0 | 2287.7 |
| Cgroup memory peak, GiB | 2.444 | 2.806 |
| Largest sampled swap use, MiB | 108.6 | 0 |

Every repeated input produced identical text, timestamps and probabilities, including across the two environments. All twelve visual tasks completed, and every model reference was cleared afterward. Temporary data was removed and both containers terminated.

Memory remains substantially above initial process use after model clearing. In the no-swap experiment the post-video RSS rises initially, then declines; it does not increase on every reload. This does not support a claim of continual growth in the observed window, but also does not prove indefinite leak freedom or return to baseline memory. The retained-allocation cause was not established, so no allocator/model-release change was introduced speculatively.

The API is colocated and cgroup peak includes subprocesses/file cache; these are not worker-only RSS numbers or a two-GiB-worker certification. Host load was not isolated. Allocation patterns differ between runs, so differences are not attributed solely to swap policy or presented as a performance comparison. Long recordings and broader input shapes require their own evidence when relevant.

## Reproduction and evidence

With the hash-locked inference runtime prepared, run the evaluator inside the bounded two-CPU/four-GiB disposable container:

```sh
PYTHONPATH=backend python scripts/check_worker_memory.py \
  --audio models/validation/asr-real/run-01/input-240.wav \
  --video models/validation/public-visual/wikimedia-screencast.webm \
  --model models/turbo --output /evidence/run-03
```

Choose fresh output. The outer runner used a 900-second timeout and ownership-scoped cleanup. The no-swap invocation additionally supplies Docker `--memory-swap=4g`; do not assume a memory limit alone disables swap.

Ignored `models/validation/worker-memory/` retains both runs' per-cycle raw transcript JSON and RSS/anonymous/file/swap/cgroup samples, source/model/runtime hashes, exact container limits, installation logs and a cross-environment assessment. Total retained evidence is about 256 KiB. No media/model downloads, persistent services, real credentials, user recordings or Philemon configuration changes occurred. Diarization quality, representative semantic/readability evaluation, installed/private/live-client and final release gates remain separate.
