# Real CPU Turbo regression

The frozen direct-inference regression passed from `3d4eda727a4c9a689899e2be3afa330ebc75a13c` to application commit `2a18cc4`. This adds real-model stability and boundary-fallback evidence. P13/P14 remain incomplete; it is not an ASR accuracy, installed-host or release acceptance result.

## Inputs and execution

The fixture is the first 240 seconds of the retained public VoxConverse test recording `bgvvt`, chosen before ASR inference. The original-download/research-use provenance remains in [DIARIZATION-EVALUATION.md](DIARIZATION-EVALUATION.md). The [upstream page](https://www.robots.ox.ac.uk/~vgg/data/voxconverse/) currently says audio is unavailable there; this run reused the existing local file and did not download or redistribute corpus audio.

Retained full WAV SHA-256: `b9d43bf8ceb535d3efe4ba7d0424bdf502b5b3220836d167192775d9391fad3a`. The 240-second mono 16-kHz PCM prefix is `b7be79fd63934cccea366293d772d5c9f3cbce68bec846e871ad4648763c5181`.

`scripts/check_asr_regression.py` exports the baseline source from Git and runs it in a separate Python process. Both sources verify the same curated Turbo weights against their pinned manifests. The current process then repeats the full input with its model already loaded and transcribes the actual two core chunks plus the 24-second boundary context. There are six real inference calls in total; no model is mocked or downloaded.

Configuration: CPU INT8, two threads, one model worker, explicit English, beam 5, temperature 0, word timestamps and VAD enabled, no hint. Dependencies were installed inside the disposable container using `--require-hashes` and `backend/requirements-inference.lock`; `pip check` passed. Runtime versions: faster-whisper 1.2.1, CTranslate2 4.6.0, PyAV 16.0.1, ONNX Runtime 1.23.1. The temporary dependency installation disappeared with the container.

## Results

| Call | Input seconds | Wall seconds | Segments / words |
| --- | ---: | ---: | ---: |
| Baseline process, first call | 240 | 198.788 | 61 / 407 |
| Current process, first call | 240 | 192.493 | 61 / 407 |
| Current process, loaded-model repeat | 240 | 189.650 | 61 / 407 |
| Current core 0 | 181.2 | 169.823 | 55 / 329 |
| Current core 1 | 62.8 | 44.489 | 10 / 83 |
| Current boundary context | 24 | 14.191 | 4 / 25 |

The three full-input outputs are exactly identical, including segment/word text, times and probabilities. Canonical JSON SHA-256: `a4d420b31fd6d7d1091236706f895ede889485c8a34b4d8cf4da1a91bdf60448`.

The planner selected a core cut at 179.2 seconds, overlapping core inputs at 177.2–181.2 seconds and boundary context at 167.2–191.2 seconds. Real chunk outputs pass reconciliation. A controlled omission removes all context-result segments while retaining the actual inferred core results: reconciliation then exactly matches the core-only result. Serializing and replaying those saved results yields the same output with zero additional inference. Normal and omitted-context outputs both contain 407 words, but have different timestamps/grouping/result hashes; equal word count is not an accuracy claim.

This specifically tests that missing context does not cause **additional loss relative to the saved core result**. It does not prove that every spoken word was transcribed correctly. There is no human word reference here, so no WER or general speech-omission guarantee is reported. API retry publication remains covered separately by backend tests; this run directly invokes the inference/reconciliation implementations.

On Vulcan, the actual cgroup reported `cpu.max = 200000 100000`, `memory.max = 4294967296` and `memory.swap.max = 4294967296`. Largest process RSS was 1,621,756 KiB for baseline and 1,685,636 KiB for current. Model-manifest verification took 4.538/4.714 seconds before the respective first calls. First-call times include model loading and inference; they are not disk-cache-cold load times. Host load was not isolated, and the small timing difference is not presented as a performance improvement.

## Evidence and remaining gates

Ignored `models/validation/asr-real/run-01/` retains the input prefix, raw per-call outputs, source/model/runtime identities, shifted chunk results and both reconciliations. `models/validation/asr-real/container.json` binds the result, inference lock and actual container/cgroup configuration; `install.log` records the exact dependency installation. The area occupies about 7.9 MiB. Source/model files were read-only and all temporary project containers were removed.

To reproduce in the prepared inference runtime, choose a fresh output directory:

```sh
python scripts/check_asr_regression.py \
  --fixture models/diarization-evaluation/heldout/bgvvt.wav \
  --model models/turbo --output /evidence/run-02
```

Use a bounded disposable container with two CPUs, adequate memory and the hash-locked inference dependencies. Each source phase has a 900-second deadline; the outer runner must also have a wall-clock timeout. The run used the existing validation image with temporary inference packages and a 1,800-second outer deadline.

Cancellation/restart with actual inference, one-worker queue/resource contention, word-reference accuracy, Spanish/private recordings, real owner-client acceptance and the installed Philemon configuration remain separate unfinished gates. No installation, production migration, secret provisioning or deployment occurred.
