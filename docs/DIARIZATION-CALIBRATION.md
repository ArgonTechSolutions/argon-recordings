# English threshold calibration and result

This experiment addresses the current speaker-count failure using the existing pinned ERes2Net embedding and INT8 segmentation weights. All 19 native inference cases completed, but the quality gates failed. Application default/model identity remain unchanged. No models were downloaded or new model terms accepted.

The pre-inference protocol ID is `ff36f0698e9ca560722b1df7b1f6613a8e6988bf064b98a2594d924e6edbf933`. Its machine-readable plan fixes input/reference hashes, split membership, thresholds, native configuration, selection and acceptance rules. The [official sherpa usage documentation](https://k2-fsa.github.io/sherpa/onnx/speaker-diarization/models.html) explains that larger clustering thresholds produce fewer speakers; its 0.9 example is for a particular Mandarin control, not an English calibration guarantee.

| Role | Fixed recordings / policy |
| --- | --- |
| Development | `aepyx`, `aggyz`; first up to 240 seconds, preserving the shorter source length |
| Threshold grid | 0.9, 1.0, 1.1, 1.2 |
| Selection | Most exact speaker counts on development; then lowest weighted plan DER; then lowest threshold |
| Single-speaker control | `aepyx`, seconds 52–72, selected from reference labels before inference; one reference speaker and at least ten annotated speech seconds |
| Candidate-specific reserved evaluation | `aorju`, `auzru`, `bidnq`, `bjruf`, `bmsyn`; 240 seconds each |
| Original failure regression | `bgvvt`, `kgjaa`, `lbfnx`, `ofbxh`, `sebyw`; 240 seconds each |

The selected threshold and calibration artifact hash are frozen before control/reserved/regression inference. Reserved recordings were previously used to evaluate a different embedding. They are disjoint from this calibration and the original ERes2Net test recordings; they are not globally unseen project data. No reserved ERes2Net predictions select the threshold. The original failed ERes2Net set is explicitly a known-failure regression, not presented as fresh held-out evidence.

Clustering uses `num_clusters=-1` throughout. No oracle speaker count, short-cluster removal, per-recording threshold or altered reference-count policy is applied. All raw turns are retained. The current validated scorer reports the 250-ms total collar/overlap-excluded policy and zero-collar/overlap-inclusive components. Aggregates sum speech-time-weighted components.

Acceptance preserves the pilot bar on **each** five-clip set: at least 4/5 exact counts, plan DER ≤25%, RTF ≤1.0; peak RSS ≤2 GiB and one predicted speaker for the control. Both sets must pass to support a count-failure fix. An evaluator exiting normally does not constitute acceptance; inspect `gates_passed` in its result.

`scripts/diarization_eval/calibrate_english.py` writes resumable raw inference checkpoints with complete model/input/config/runtime identities and turn hashes. Reuse requires matching identities. Metrics are recomputed from the raw turns, and a resumed frozen selection must match its calibration hash. No existing evaluation reports or input media are overwritten.

```sh
python scripts/diarization_eval/calibrate_english.py --prepare-only \
  --output models/validation/diarization-english-grid

# In the pinned optional sherpa-onnx 1.13.8 runtime:
python scripts/diarization_eval/calibrate_english.py \
  --output /evidence
```

The run used the existing validation image plus temporary sherpa-onnx/core 1.13.8, two effective CPUs, two GiB RAM, swap disabled and a 3,600-second outer deadline. Eight scorer tests and `pip check` passed before inference. Actual cgroup limits and plan identity are recorded in ignored `models/validation/diarization-english-grid/container.json`. The native evaluator source is commit `ccc9a2d`; a subsequent reporting fix preserves the cached native memory peak during scoring-only resume, checked against all 19 actual checkpoints without additional inference.

Per-case JSON, `calibration.json`, `frozen-candidate.json` and final results reside in that ignored evidence directory. Source/private recordings, installed Philemon behavior, Spanish quality and actual owner-agent acceptance remain separate; no application model default or production configuration is changed by this experiment.

## Completed development checkpoint

All eight development inferences completed. Exact-count hits are 0/2 for every tested threshold; weighted plan DER is 20.71%, 23.00%, 34.92% and 62.18% respectively. Increasing the threshold improves the initially oversegmented four-speaker example before merging real speakers, while worsening undercount on the crowded thirteen-speaker example. This grid did not identify a count-quality improvement.

The deterministic rule selected 0.9, with calibration SHA-256 `154d154cdb3a5e92bc7aa9be3982d14ea98c2ad9373487518ce073f33514831a`. The subsequent automatic single-speaker control returned one speaker (plan DER 5.87%). The grid and acceptance bar were preserved after the negative development result.

## Completed evaluation

| Set | Exact counts | Plan DER | Zero-collar overlap-inclusive DER | Processing RTF |
| --- | ---: | ---: | ---: | ---: |
| Candidate-specific reserved, 20 minutes | 1/5 | 22.69% | 29.96% | 0.339 |
| Original failure regression, 20 minutes | 1/5 | 5.59% | 6.80% | 0.319 |

Both sets fail the required 4/5 exact counts. DER, processing RTF and peak process RSS (440,116 KiB) pass their declared limits; they do not compensate for count failures. Processing RTF excludes model construction/scoring. The result explicitly records `gates_passed: false` despite normal evaluator completion.

Failure examples include `bjruf` (2 reference speakers → 12 predicted, plan DER 63.34%) and `bmsyn` (5 → 14, DER 20.02%). The original regression reproduces the prior count failure. A larger single global threshold did not resolve the competing over/undercount cases in development, so no clustering change was promoted. Broader embedding/model work remains for a future resumed session.

All owned temporary containers were removed. Runtime/source/model/result hashes and raw turns remain in the ignored evidence directory; original recordings, evaluation reports and model weights are intact. The owner requested pausing after this task; further experiments and implementation are deferred.
