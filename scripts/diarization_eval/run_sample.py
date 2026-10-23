#!/usr/bin/env python3
"""Run the pinned sherpa-onnx smoke sample; outputs turns and resource metrics."""
import hashlib
import json
import resource
import time
from pathlib import Path

import sherpa_onnx
import soundfile as sf


ROOT = Path(__file__).resolve().parents[2] / "models" / "diarization-evaluation"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> None:
    segmentation = ROOT / "sherpa-onnx-pyannote-segmentation-3-0" / "model.int8.onnx"
    embedding = ROOT / "embedding.onnx"
    audio_path = ROOT / "sample.wav"
    audio, sample_rate = sf.read(audio_path, dtype="float32", always_2d=True)
    if audio.shape[1] != 1:
        audio = audio[:, 0]
    else:
        audio = audio[:, 0]
    config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                model=str(segmentation), window_shift_ratio=0.1
            )
        ),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=str(embedding), num_threads=2
        ),
        clustering=sherpa_onnx.FastClusteringConfig(num_clusters=4, threshold=0.5),
        min_duration_on=0.3,
        min_duration_off=0.5,
    )
    if not config.validate():
        raise RuntimeError("Invalid model configuration")
    diarizer = sherpa_onnx.OfflineSpeakerDiarization(config)
    started = time.monotonic()
    turns = diarizer.process(audio).sort_by_start_time()
    elapsed = time.monotonic() - started
    duration = len(audio) / sample_rate
    print(json.dumps({
        "runtime": "sherpa-onnx 1.13.8",
        "sample_rate": sample_rate,
        "duration_seconds": duration,
        "elapsed_seconds": elapsed,
        "rtf": elapsed / duration,
        "max_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "speaker_ids": sorted({turn.speaker for turn in turns}),
        "turn_count": len(turns),
        "sha256": {p.name: sha256(p) for p in (segmentation, embedding, audio_path)},
        "turns": [{"start": t.start, "end": t.end, "speaker": t.speaker} for t in turns],
    }, indent=2))


if __name__ == "__main__":
    main()
