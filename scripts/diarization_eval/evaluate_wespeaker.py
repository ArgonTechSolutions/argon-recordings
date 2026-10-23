#!/usr/bin/env python3
"""Frozen WeSpeaker candidate evaluation on five test excerpts disjoint from dev/prior run."""
import json
import resource
import time
import wave
from pathlib import Path

import numpy as np
import sherpa_onnx

from score import Turn, read_rttm, score

ROOT = Path(__file__).resolve().parents[2] / "models" / "diarization-evaluation"
CLIP_SECONDS = 240
THRESHOLD = 0.5  # Frozen from dev set aepyx/aggyz before this held-out run.
FINAL_IDS = ("aorju", "auzru", "bidnq", "bjruf", "bmsyn")


def main():
    heldout = ROOT / "wespeaker-heldout-final"
    prior = json.loads((ROOT / "wespeaker-heldout/evaluation.json").read_text())
    prior_by_id = {row["id"]: row for row in prior["per_clip"]}
    manifest = json.loads((heldout / "manifest.json").read_text())
    assert tuple(item["id"] for item in manifest) == FINAL_IDS
    assert all(item["duration"] >= CLIP_SECONDS for item in manifest)
    for item in manifest:
        refs = read_rttm(heldout / f"{item['id']}.rttm").get(item["id"], [])
        assert refs and max(turn.end for turn in refs) >= CLIP_SECONDS
    assert all((heldout / f"{name}.{suffix}").is_file()
               for name in FINAL_IDS for suffix in ("wav", "rttm"))
    assert all((ROOT / "wespeaker-heldout" / f"{name}.hyp.rttm").is_file()
               for name in FINAL_IDS if name != "bmsyn")
    rows = []
    elapsed_total = duration_total = 0.0
    for item in json.loads((heldout / "manifest.json").read_text()):
        name = item["id"]
        with wave.open(str(heldout / f"{name}.wav"), "rb") as wav:
            rate = wav.getframerate()
            assert rate == 16000 and wav.getnchannels() == 1 and wav.getsampwidth() == 2
            pcm = wav.readframes(CLIP_SECONDS * rate)
        audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768
        duration = len(audio) / rate
        ref = [Turn(t.speaker, t.start, min(t.end, duration)) for t in read_rttm(heldout / f"{name}.rttm").get(name, []) if t.start < duration]
        cfg = sherpa_onnx.OfflineSpeakerDiarizationConfig(
            segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
                pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                    model=str(ROOT / "sherpa-onnx-pyannote-segmentation-3-0/model.int8.onnx"), window_shift_ratio=.1),
                num_threads=2),
            embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(ROOT / "wespeaker_en_voxceleb_resnet34.onnx"), num_threads=2),
            clustering=sherpa_onnx.FastClusteringConfig(num_clusters=-1, threshold=THRESHOLD),
            min_duration_on=.3, min_duration_off=.5)
        assert cfg.validate()
        raw = heldout / f"{name}.hyp.rttm"
        if name == "bmsyn":
            started = time.monotonic()
            turns = sherpa_onnx.OfflineSpeakerDiarization(cfg).process(audio).sort_by_start_time()
            elapsed = time.monotonic() - started
            hyp = [Turn(str(t.speaker), max(0, t.start), min(duration, t.end)) for t in turns if t.end > t.start]
            raw.write_text("".join(f"SPEAKER {name} 1 {t.start:.3f} {t.end-t.start:.3f} <NA> <NA> {t.speaker} <NA> <NA>\n" for t in hyp))
        else:
            # These four clips were already inferred at the same frozen threshold.
            # Re-score their unchanged raw turns with this scorer and retain measured runtimes.
            source_hyp = ROOT / "wespeaker-heldout" / f"{name}.hyp.rttm"
            raw.write_text(source_hyp.read_text())
            hyp = read_rttm(raw).get(name, [])
            elapsed = prior_by_id[name]["elapsed_seconds"]
        metrics = {policy: score(ref, hyp, duration=duration, collar=collar, ignore_overlap=ignore)
                   for policy, collar, ignore in (("plan", .25, True), ("zero_collar_overlap_inclusive", 0, False))}
        rows.append({"id": name, "duration_seconds": duration,
                     "reference_speakers": len({t.speaker for t in ref}), "hypothesis_speakers": len({t.speaker for t in hyp}),
                     "speaker_count_error": len({t.speaker for t in hyp})-len({t.speaker for t in ref}),
                     "elapsed_seconds": elapsed, "rtf": elapsed/duration, "metrics": metrics,
                     "reference_turns": len(ref), "hypothesis_turns": len(hyp), "raw_hypothesis_rttm": raw.name})
        elapsed_total += elapsed; duration_total += duration
    aggregate = {}
    for policy in ("plan", "zero_collar_overlap_inclusive"):
        sums = {k: sum(r["metrics"][policy][k] for r in rows) for k in ("miss_seconds", "false_alarm_seconds", "confusion_seconds", "scored_speech_seconds")}
        sums["der"] = sum(sums[k] for k in ("miss_seconds", "false_alarm_seconds", "confusion_seconds"))/sums["scored_speech_seconds"]
        aggregate[policy] = sums
    output = {"model": "wespeaker_en_voxceleb_resnet34.onnx", "split": "VoxConverse test v0.3",
              "development_ids": [x["id"] for x in json.loads((ROOT/"wespeaker-dev/manifest.json").read_text())],
              "heldout_ids": [r["id"] for r in rows], "clip_seconds": CLIP_SECONDS,
              "excluded_before_inference": [{"id": "aiqwk", "verified_audio_seconds": 198.848,
                                              "reason": "clip is shorter than the predeclared 240-second excerpt"}],
              "replacement_selection": "First lexicographic unused VoxConverse test ID with verified audio and RTTM span >=240 seconds; bmsyn (702.16 seconds).",
              "threshold_frozen_before_heldout": THRESHOLD, "runtime": "sherpa-onnx 1.13.8",
              "cpu_threads": 2, "total_duration_seconds": duration_total, "total_elapsed_seconds": elapsed_total,
              "aggregate_rtf": elapsed_total/duration_total,
              "peak_rss_kib": max(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                                   prior["peak_rss_kib"]),
              "replacement_inference_peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
              "per_clip": rows, "aggregate": aggregate}
    (heldout / "evaluation.json").write_text(json.dumps(output, indent=2))
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
