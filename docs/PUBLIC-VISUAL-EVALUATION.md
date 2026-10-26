# Public screencast evaluation

The frozen visual candidate at application commit `9c6f746` was executed on a real public desktop demonstration. This provides measured extraction cost, temporal checkpoint coverage and an OCR readability proxy. It does **not** pass the representative visual-quality gate.

## Fixture and frozen references

Toolboxx8's [Wikimedia Screencast](https://commons.wikimedia.org/wiki/File:Wikimedia_Screencast.webm), dated 17 December 2014, is declared the author's own work under [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/). Credit and license accompany the retained source and sampled/resized/reencoded derivatives. No media is committed to Git.

The downloaded original is 3,060,157 bytes, SHA-256 `0cabfba7f372013acc1050fb98c4454d89cd88dc88214f092baae54f299270f3`. Runtime probe: 87.128 seconds, 960×720 including letterboxing, 29.97 fps. The recording demonstrates editing a Phabricator task.

Before running the candidate, the agent inspected original reference frames and froze eight coarse screen-state checkpoints at 0, 20, 30, 40, 50, 60, 70 and 80 seconds, with five-second temporal tolerance. Ten expected text phrases across two frames were frozen for OCR comparison. References, attribution and policy are in `scripts/visual_eval/wikimedia-reference.json`. There was no threshold tuning on this fixture.

These are agent-labelled checkpoints, not human-adjudicated event onsets. A nearby proposal may show a different state; temporal matches must not be reported as semantic event recall. The reference does not establish representative code/slide coverage.

## Measured result

| Measure | Result |
| --- | --- |
| Analysis | Complete; 1.187 seconds; 174 sampled frames; 4,810,752 analysis bytes |
| Proposals / rendered derivatives | 11 / 11: ten stills and one eight-second clip |
| Temporal checkpoint matches | Adaptive 6/8; sparse 0/60-second baseline 2/8 |
| Raw adaptive derivative bytes | 834,777 |
| Unique-recipe derivative bytes | 698,883 across nine recipes; two exact timestamp/recipe duplicates |
| Sparse derivative bytes | 143,940 across two native-size stills |
| OCR phrase recovery, 480 px | 0/10; 48,906 image bytes across two stills |
| OCR phrase recovery, 960 px | 2/10; 134,223 image bytes across two stills |
| Total evaluation wall time | 10.141 seconds, excluding OCR package installation |
| Largest child-process peak RSS | 193,228 KiB; not aggregate container memory |

The raw adaptive output costs 5.80 times the sparse baseline while supplying different evidence coverage. This is not a storage-savings claim. Unique-recipe aggregation measures the generated files after removing repeated recipes; it is not an installed-API storage measurement. Opening and 80-second checkpoints have no proposal within the frozen tolerance. Final-state evidence could occur earlier, so those misses do not prove semantic omission.

Tesseract 5.3.0, English, sparse-text mode 11, assessed the exact extracted images. Native-size text recovery is better in this narrow proxy but remains poor. A visually readable image can still fail this OCR configuration; these counts are neither human reading accuracy nor proof that the source text is unrecoverable. OCR tuning, calibrated readability selection and representative human acceptance remain unqualified. No OCR dependency or automatic resolution decision was added to the application.

The run used the existing validation image in a disposable two-CPU, 2-GiB container on Vulcan's Intel i7-9700T. Host load was not isolated. Analysis stayed within the 45-second/602-frame/22,228,992-byte limits. Tesseract was installed only inside that temporary container and removed with it. No persistent service, model download, private recording or production configuration was involved.

## Evidence and reproduction

Ignored evidence: `models/validation/public-visual/run-01/` contains `analysis.json`, `result.json`, `assessment.json`, derivative manifests, actual images/clip and OCR text. The parent folder retains the original, reference frames, attribution and OCR installation log. Source/reference hashes and runtime-code hashes bind the result; the container sidecar records the base image and limits.

The evaluator accepts an already downloaded local source with the exact frozen hash. With backend dependencies, FFmpeg and the separate Tesseract English evaluator installed, run:

```sh
PYTHONPATH=backend python scripts/visual_eval/evaluate_public.py \
  --source models/validation/public-visual/wikimedia-screencast.webm \
  --reference scripts/visual_eval/wikimedia-reference.json \
  --output models/validation/public-visual/run-02
```

Choose a fresh output directory. Rendering has a 240-second admission deadline between tasks, in addition to each subprocess limit; the outer disposable container must also have a wall-clock timeout. Partial rendering and extraction failures remain explicit in the result.

Next quality work needs precise independently adjudicated event labels, code and slide demonstrations, a validated readability evaluator and acceptance targets, and timing/storage comparisons at matched evidence coverage. Private recordings and installed Philemon performance remain separate. This first real fixture narrows those gaps without closing P08/P13/P14.
