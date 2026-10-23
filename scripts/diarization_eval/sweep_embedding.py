#!/usr/bin/env python3
"""Tune only the predeclared two-clip development set for one embedding model."""
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
THRESHOLDS = (0.3, 0.5, 0.7, 0.9)


def main():
    directory = ROOT / "wespeaker-dev"
    rows = []
    for threshold in THRESHOLDS:
        clips = []
        start_total = time.monotonic()
        for item in json.loads((directory / "manifest.json").read_text()):
            name = item["id"]
            with wave.open(str(directory / f"{name}.wav"), "rb") as wav:
                rate = wav.getframerate()
                assert rate == 16000 and wav.getnchannels() == 1 and wav.getsampwidth() == 2
                pcm = wav.readframes(CLIP_SECONDS * rate)
            audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768
            duration = len(audio) / rate
            ref = [Turn(t.speaker, t.start, min(t.end, duration)) for t in read_rttm(directory / f"{name}.rttm").get(name, []) if t.start < duration]
            config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
                segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
                    pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                        model=str(ROOT / "sherpa-onnx-pyannote-segmentation-3-0/model.int8.onnx"), window_shift_ratio=.1),
                    num_threads=2),
                embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(ROOT / "wespeaker_en_voxceleb_resnet34.onnx"), num_threads=2),
                clustering=sherpa_onnx.FastClusteringConfig(num_clusters=-1, threshold=threshold),
                min_duration_on=.3, min_duration_off=.5)
            diarizer = sherpa_onnx.OfflineSpeakerDiarization(config)
            assert config.validate()
            turns = diarizer.process(audio).sort_by_start_time()
            hyp = [Turn(str(t.speaker), max(0, t.start), min(duration, t.end)) for t in turns if t.end > t.start]
            metrics = score(ref, hyp, duration=duration, collar=.25, ignore_overlap=True)
            clips.append({"id": name, "reference_speakers": len({t.speaker for t in ref}),
                          "hypothesis_speakers": len({t.speaker for t in hyp}),
                          "speaker_count_error": len({t.speaker for t in hyp})-len({t.speaker for t in ref}),
                          "der": metrics["der"], "miss_seconds": metrics["miss_seconds"],
                          "false_alarm_seconds": metrics["false_alarm_seconds"], "confusion_seconds": metrics["confusion_seconds"],
                          "reference_speech_seconds": metrics["scored_speech_seconds"]})
        weighted = sum(c["der"] * c["reference_speech_seconds"] for c in clips) / sum(c["reference_speech_seconds"] for c in clips)
        rows.append({"threshold": threshold, "correct_counts": sum(c["speaker_count_error"] == 0 for c in clips),
                     "weighted_der": weighted, "elapsed_seconds": time.monotonic()-start_total,
                     "clips": clips})
    result = {"model": "WeSpeaker VoxCeleb ResNet34 ONNX", "development_ids": [x["id"] for x in json.loads((directory/"manifest.json").read_text())],
              "clip_seconds": CLIP_SECONDS, "rows": rows, "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}
    (directory / "sweep.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
