"""Bounded optional CPU diarization subprocess; never downloads weights."""
import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import wave

FILES = {
    'sherpa-onnx-pyannote-segmentation-3-0/model.int8.onnx': 'd582f4b4c6b48205de7e0643c57df0df5615a3c176189be3fc461e9d18827b5d',
    'embedding.onnx': '1a331345f04805badbb495c775a6ddffcdd1a732567d5ec8b3d5749e3c7a5e4b',
}
MODEL = 'sherpa-onnx:1.13.8:seg-int8-d582f4b4:eres2net-1a331345:auto-0.9:v2'


def verify(directory):
    for name, expected in FILES.items():
        digest = hashlib.sha256()
        with (Path(directory) / name).open('rb') as stream:
            for block in iter(lambda: stream.read(1024**2), b''):
                digest.update(block)
        if digest.hexdigest() != expected:
            raise ValueError('Diarization model checksum mismatch')


def diarize(audio, directory, options):
    if importlib.metadata.version('sherpa-onnx') != '1.13.8':
        raise RuntimeError('Supply pinned optional sherpa-onnx 1.13.8 runtime')
    verify(directory)
    import numpy as np
    import sherpa_onnx
    with wave.open(str(audio)) as stream:
        if stream.getnchannels() != 1 or stream.getsampwidth() != 2 or stream.getframerate() != 16000:
            raise ValueError('Diarization requires retained 16kHz mono PCM16 audio')
        if stream.getnframes() > 16000 * 7200:
            raise ValueError('Diarization exceeds the two-hour inference memory budget')
        samples = np.frombuffer(stream.readframes(stream.getnframes()), dtype='<i2').astype('float32') / 32768
    fixed = options.min_speakers if options.min_speakers == options.max_speakers else -1
    config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                model=str(Path(directory) / next(iter(FILES))), window_shift_ratio=0.1), num_threads=2),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(Path(directory) / 'embedding.onnx'), num_threads=2),
        clustering=sherpa_onnx.FastClusteringConfig(num_clusters=fixed, threshold=0.9),
        min_duration_on=0.3, min_duration_off=0.5)
    if not config.validate():
        raise ValueError('Invalid pinned diarization configuration')
    turns = sherpa_onnx.OfflineSpeakerDiarization(config).process(samples).sort_by_start_time()
    count = len({t.speaker for t in turns})
    if count and not options.min_speakers <= count <= options.max_speakers:
        raise ValueError('Detected speaker count is outside the requested bounds')
    return {'model': MODEL, 'turns': [dict(start=max(0, t.start), end=min(len(samples)/16000, t.end), speaker=f'speaker-{t.speaker}') for t in turns]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audio', type=Path, required=True)
    parser.add_argument('--models', type=Path, required=True)
    parser.add_argument('--options', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    from .diarization import SpeakerOptions
    options = SpeakerOptions.model_validate_json(args.options.read_text())
    args.output.write_text(json.dumps(diarize(args.audio, args.models, options)))


if __name__ == '__main__':
    main()
