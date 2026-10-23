#!/usr/bin/env python3
"""Evaluate frozen sherpa configuration on five fixed VoxConverse test excerpts."""
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
THRESHOLD = 0.9


def main():
    heldout = ROOT / "heldout"
    results = []
    total_duration = total_elapsed = 0.0
    for item in json.loads((heldout / "manifest.json").read_text()):
        name = item["id"]
        audio_path = heldout / f"{name}.wav"
        with wave.open(str(audio_path), "rb") as wav:
            rate, channels, width = wav.getframerate(), wav.getnchannels(), wav.getsampwidth()
            assert rate == 16000 and channels == 1 and width == 2
            pcm = wav.readframes(CLIP_SECONDS * rate)
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768
        duration = len(samples) / rate
        ref_file = heldout / f"{name}.rttm"
        ref = [Turn(t.speaker, t.start, min(t.end, duration)) for t in read_rttm(ref_file).get(name, []) if t.start < duration]
        config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
            segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
                pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                    model=str(ROOT / "sherpa-onnx-pyannote-segmentation-3-0/model.int8.onnx"),
                    window_shift_ratio=0.1), num_threads=2),
            embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(ROOT / "embedding.onnx"), num_threads=2),
            clustering=sherpa_onnx.FastClusteringConfig(num_clusters=-1, threshold=THRESHOLD),
            min_duration_on=0.3, min_duration_off=0.5)
        if not config.validate():
            raise RuntimeError("Candidate config failed validation")
        diarizer = sherpa_onnx.OfflineSpeakerDiarization(config)
        started = time.monotonic()
        output = diarizer.process(samples).sort_by_start_time()
        elapsed = time.monotonic() - started
        hyp = [Turn(str(t.speaker), max(0, t.start), min(duration, t.end)) for t in output if t.end > t.start]
        raw = heldout / f"{name}.hyp.rttm"
        raw.write_text("".join(f"SPEAKER {name} 1 {t.start:.3f} {t.end-t.start:.3f} <NA> <NA> {t.speaker} <NA> <NA>\n" for t in hyp))
        metrics = {}
        for policy, collar, ignore_overlap in (("plan", .25, True), ("zero_collar_overlap_inclusive", 0, False)):
            metrics[policy] = score(ref, hyp, duration=duration, collar=collar, ignore_overlap=ignore_overlap)
        total_duration += duration
        total_elapsed += elapsed
        results.append({"id": name, "duration_seconds": duration, "reference_speakers": len({t.speaker for t in ref}),
                        "hypothesis_speakers": len({t.speaker for t in hyp}), "speaker_count_error": len({t.speaker for t in hyp})-len({t.speaker for t in ref}),
                        "elapsed_seconds": elapsed, "rtf": elapsed/duration, "metrics": metrics,
                        "reference_turns": len(ref), "hypothesis_turns": len(hyp), "raw_hypothesis_rttm": raw.name})
    # Aggregate components by reference speech time, not by averaging clip DER.
    aggregate = {}
    for policy in ("plan", "zero_collar_overlap_inclusive"):
        keys = ("miss_seconds", "false_alarm_seconds", "confusion_seconds", "scored_speech_seconds")
        sums = {key: sum(row["metrics"][policy][key] for row in results) for key in keys}
        sums["der"] = sum(sums[k] for k in keys[:3]) / sums["scored_speech_seconds"] if sums["scored_speech_seconds"] else 0
        aggregate[policy] = sums
    report = {"corpus": "VoxConverse test v0.3", "split_frozen_before_inference": True, "clip_seconds_each": CLIP_SECONDS,
              "threshold_frozen_from_separate_control": THRESHOLD, "runtime": "sherpa-onnx 1.13.8", "cpu_threads": 2,
              "total_duration_seconds": total_duration, "total_elapsed_seconds": total_elapsed,
              "aggregate_rtf": total_elapsed/total_duration, "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
              "per_clip": results, "aggregate": aggregate}
    (heldout / "evaluation.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
