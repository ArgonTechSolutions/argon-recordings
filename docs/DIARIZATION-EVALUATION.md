# Diarization feasibility evaluation

Status: **speaker-count quality remains blocked**. Both original candidates fail the count gate. A subsequent English threshold calibration also fails: 0/2 exact development counts across the fixed grid, then 1/5 on both reserved and original regression sets. See [DIARIZATION-CALIBRATION.md](DIARIZATION-CALIBRATION.md). No automatic quality acceptance is claimed. Spanish and private-recording quality remain unverified.

## Candidate

- Runtime: `sherpa-onnx==1.13.8`, offline speaker diarization API.
- Segmentation: official sherpa-onnx `sherpa-onnx-pyannote-segmentation-3-0` release, `model.int8.onnx`. The release archive includes a LICENSE granting MIT terms, copyright CNRS 2022; its README says the model was converted from `pyannote/segmentation-3.0`. We used the public sherpa release and did not access gated Hugging Face files.
- Embeddings: official sherpa-onnx release `3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx`. Upstream 3D-Speaker repository declares Apache-2.0. The selected release is an ONNX conversion; preserve the upstream attribution and Apache notice with any distribution. This is Mandarin-trained, so English/Spanish transfer remains an explicit quality uncertainty.
- English alternative: official sherpa-onnx WeSpeaker VoxCeleb ResNet34 ONNX (`wespeaker_en_voxceleb_resnet34.onnx`, SHA-256 `5ef208a9da1453335308a6b6f4e6dfbd7e183a38b604de0a57664f45d257fe94`). The upstream WeSpeaker pretrained-model documentation identifies the VoxCeleb pretrained checkpoint as CC BY 4.0. The model was evaluated locally and is not included in Git or the product. Segmentation is the same model/runtime as above.
- Audio contract: mono float samples at 16 kHz (resample before inference); generated turns are recording-wide. This smoke invocation used fixed `num_clusters=4`; production must evaluate automatic clustering and uncertainty handling rather than assume speaker count.
- CPU/RAM: disposable `argon-recordings-worker:local` image, Python 3.11.13, `--cpus=2 --memory=2g`; embedding extractor set to 2 threads. Do not add this optional dependency to API or base ASR runtime without a separately measured integration decision.
- Automatic clustering threshold: `0.9`, selected only on the separate official four-speaker Mandarin smoke/control sample (thresholds .5/.7/.9 returned 7/6/4 IDs respectively). The held-out test split was not used for tuning.

## Smoke run

Official sherpa four-speaker sample: 56.8607 seconds, 16 kHz. Inference produced 10 turns and four speaker IDs in 8.552 seconds (RTF 0.1504), peak process RSS 275,124 KiB. The sample was run with a known speaker count of four, so this only confirms that the model/runtime executes and emits plausible turn structure; it is not a reference-scored quality result.

SHA-256:

| Artifact | SHA-256 |
|---|---|
| `embedding.onnx` | `1a331345f04805badbb495c775a6ddffcdd1a732567d5ec8b3d5749e3c7a5e4b` |
| `sample.wav` | `bedf036caed208386c67b4ef4b11f83d74dd0d420b102163a1c33cd09cde7010` |

## Frozen held-out result

Corpus: VoxConverse test v0.3. The dataset website states it is available for research purposes under CC BY 4.0 and notes that video copyright remains with original owners. Use here was local evaluation only; no corpus media is included in the product or Git. The official test WAV archive was verified against its published MD5 `834558bbd9b1ffd2d4893181556ceddd`. Five recording IDs (`lbfnx`, `sebyw`, `ofbxh`, `kgjaa`, `bgvvt`) were selected from the held-out test split before inference; the first 240 seconds of each yielded five real clips totaling 1,200 seconds. Threshold 0.9 was frozen from the distinct Mandarin control before this test run.

| Measure | Result | Proposed P02 pilot gate |
|---|---:|---:|
| Aggregate DER, 250 ms total collar (125 ms per side), overlap excluded | 5.59% | <=25% pass |
| Miss / false alarm / confusion | 34.72 / 5.62 / 18.99 sec | report all components |
| Aggregate zero-collar, overlap-inclusive DER | 6.80% | report, no threshold |
| Aggregate RTF | 0.341 | <=1.0 pass |
| Peak RSS | 578,104 KiB | <=2 GiB pass |
| Correct speaker count | 1 / 5 | >=4 / 5 **fail** |

Per-clip speaker counts and DER:

| Test ID | Reference → hypothesis speakers | DER, plan policy | DER, zero-collar overlap-inclusive |
|---|---:|---:|---:|
| `lbfnx` | 4 → 7 | 3.23% | 5.36% |
| `sebyw` | 3 → 4 | 4.07% | 4.41% |
| `ofbxh` | 3 → 4 | 6.72% | 7.37% |
| `kgjaa` | 5 → 7 | 8.86% | 10.22% |
| `bgvvt` | 2 → 2 | 4.90% | 6.56% |

Raw audio excerpts, references, hypotheses, manifest, and machine-readable metrics reside in ignored `models/diarization-evaluation/heldout/`. They are local evaluation data only and must not be committed. DER quality is promising on English audio, but count over-segmentation is a blocking product-quality defect; do not conceal it by fixing expected speaker counts.

The earlier draft reported 5.12% plan DER and 6.80% zero-collar DER by treating 0.25 seconds as the half-width on each side of a reference boundary (a 500 ms total mask). Those figures are retained as historical, nonstandard results in `evaluation.nonstandard-500ms-mask.json`; the table above uses pyannote's 250 ms total collar convention.

Cluster-duration inspection found that several excess labels were tiny (for example `lbfnx` had four clusters below 3.2 seconds, `sebyw` one 1.1-second cluster, and `ofbxh` one 0.44-second cluster). However, `kgjaa` also had excess clusters lasting 27.61 and 22.88 seconds. A blanket short-cluster filter would remove valid speech along with spurious labels and does not correct the material oversegmentation. No duration pruning was applied. Preserve the raw turns and let users review/reassign them; any further clustering tuning makes this test set development evidence and requires a new disjoint held-out set.

## English embedding comparison

The Mandarin-trained embedding's count failure prompted one bounded comparison with the ungated English VoxCeleb ResNet34 model. Two distinct development excerpts (`aepyx`, `aggyz`, 240 seconds each) were used to choose one threshold. The predeclared decision rule prioritized exact speaker-count hits, then DER: threshold 0.5 was the only candidate with any exact-count hit (1/2); its weighted DER was 23.94%. Thresholds 0.3, 0.7, and 0.9 had 0/2 exact counts and weighted DER 22.43%, 49.89%, and 62.61%. The 0.5 threshold was frozen before evaluating the held-out set and was not changed afterward.

The final held-out set contains five disjoint 240-second excerpts (`aorju`, `auzru`, `bidnq`, `bjruf`, `bmsyn`), totaling 1,200 seconds, with human RTTM references. `aiqwk` was excluded before final-set inference because its verified audio span is 198.848 seconds, below the fixed excerpt length. The replacement was selected as the first lexicographic unused test ID with at least 240 seconds of verified audio and RTTM span: `bmsyn` (702.16 seconds of annotated span; the WAV is longer). The four unaffected clips reused their already-generated 0.5-threshold turns and were re-scored with the corrected scorer; only `bmsyn` was newly inferred. The result artifact records this selection and exclusion.

| Measure | Result | Proposed P02 pilot gate |
|---|---:|---:|
| Aggregate DER, 250 ms total collar (125 ms per side), overlap excluded | 22.91% | <=25% pass |
| Miss / false alarm / confusion | 22.03 / 26.72 / 184.83 sec | report all components |
| Aggregate zero-collar, overlap-inclusive DER | 28.50% | report, no threshold |
| Aggregate RTF | 0.243 | <=1.0 pass |
| Peak process RSS | 248,756 KiB | <=2 GiB pass |
| Exact speaker-count hits | 1 / 5 | >=4 / 5 **fail** |

| Test ID | Reference → hypothesis speakers | DER, plan policy | DER, zero-collar overlap-inclusive |
|---|---:|---:|---:|
| `aorju` | 4 → 3 | 48.89% | 50.47% |
| `auzru` | 3 → 3 | 0.07% | 1.07% |
| `bidnq` | 3 → 4 | 9.85% | 16.52% |
| `bjruf` | 2 → 6 | 40.51% | 46.98% |
| `bmsyn` | 5 → 11 | 19.55% | 27.88% |

This candidate trades the low DER observed for the Mandarin embedding on its separate test set for a higher DER on this separate set. It substantially undercounts `aorju` and oversegments `bjruf` and `bmsyn`; a favorable aggregate does not resolve the count failure. The two development excerpts are small and their 1/2 count result is weak calibration evidence. Do not infer Spanish or private-recording quality from VoxConverse. The raw final-set turns and metrics are in ignored `models/diarization-evaluation/wespeaker-heldout-final/`; the earlier short-clip run remains preserved separately in `wespeaker-heldout/` as exploratory evidence.

The earlier draft reported 21.07% plan DER and 28.50% zero-collar DER with a 500 ms total boundary mask. These figures remain in `evaluation.nonstandard-500ms-mask.json` for traceability; the table above uses the standard 250 ms total collar. Speaker counts in both updated reports are computed from the full 240-second clipped RTTMs, independent of collar or overlap exclusions.

## API/worker integration smoke

`scripts/diarization_eval/api_worker_smoke.py` uses FastAPI `TestClient`, a temporary local recording, the actual `diarization_worker.run`, its subprocess runtime, and real sherpa inference on the official sample. The job reached `completed`, published 10 turns across four predicted speaker IDs, and the transcript payload remained byte-for-byte equivalent at the JSON object level. This verifies local queue/runtime/publication plumbing only; its transcript fixture is synthetic and does not constitute ASR or held-out diarization quality evidence.

Weights and sample are stored under ignored `models/diarization-evaluation/`; they must not be committed. Recreate the runtime with a disposable container, overriding the worker entrypoint:

```sh
python3 scripts/dev_container.py --timeout 7200 diarization-eval -- \
  --cpus=2 --memory=2g --entrypoint /bin/sh \
  -v "$PWD:/repo" argon-recordings-worker:local -lc '
  cd /repo/models/diarization-evaluation
  python -m pip install sherpa-onnx==1.13.8 soundfile librosa
  # Use sherpa_onnx.OfflineSpeakerDiarizationConfig with the two model paths;
  # see the upstream example linked below. Set embedding num_threads=2.
  '
```

## Scoring and remaining acceptance gap

P02 requires at least five held-out real clips totaling at least 20 minutes, with human RTTM references, plus a single-speaker control. Score DER components and speaker-count error under both a 250 ms total collar (125 ms on either side of reference boundaries) with overlap excluded and a zero-collar overlap-inclusive policy. Report aggregate and per-clip results, runtime/RTF, peak RSS, and environment. The proposed pilot gates are DER <=25%, correct speaker count on at least four of five clips (with explained exceptions), RTF <=1.0, and peak RSS <=2 GiB. These thresholds are not relaxed by the smoke run.

`scripts/diarization_eval/score.py` provides RTTM parsing and permutation-invariant DER components. It uses a 10 ms frame grid and applies a 250 ms total collar as 125 ms on each side of every reference boundary, matching pyannote's collar convention. Speaker counts use full clipped RTTMs and are unaffected by metric masks. Eight automated tests cover all-silent input, miss/false-alarm direction, overlap attribution, speaker-label permutation, undercount confusion, collar width and counts, and both overlap policies. An isolated check against `pyannote.metrics==3.2.1` matched DER components on collar, overlap, and undercount fixtures within the 10 ms grid precision; the collar fixture differed from exact pyannote durations by 5 ms in missed speech. The custom Hungarian assignment matched `scipy.optimize.linear_sum_assignment` on 49 deterministic rectangular matrices. `rescore_saved.py` re-scores the exact frozen references and hypotheses without rerunning inference. The corpus is English; no Spanish reference quality claim can be made yet. The pilot is not representative of Philemon/private recordings.

## Sources

- [sherpa-onnx official diarization model documentation](https://k2-fsa.github.io/sherpa/onnx/speaker-diarization/models.html)
- [sherpa-onnx official Python inference example](https://github.com/k2-fsa/sherpa-onnx/blob/master/python-api-examples/offline-speaker-diarization.py)
- [sherpa-onnx release archive](https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2)
- [3D-Speaker repository and license declaration](https://github.com/modelscope/3D-Speaker)
- [WeSpeaker pretrained models and license notes](https://github.com/wenet-e2e/wespeaker/blob/master/docs/pretrained.md)
- [pyannote.metrics collar and overlap implementation](https://github.com/pyannote/pyannote-metrics/blob/develop/src/pyannote/metrics/utils.py)

## Storage checkpoint

Unused comparison downloads and the full corpus archive were retired after dependency checks. Frozen reports, references, hypotheses and evaluation excerpts remain available. See [MODEL-STORAGE.md](MODEL-STORAGE.md) for the retained models and regeneration boundary.
