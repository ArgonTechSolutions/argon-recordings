"""Prepare or verify the curated Turbo model without host inference dependencies."""
import argparse
import hashlib
import json
from pathlib import Path
import urllib.request
from argon_recordings.models import TURBO_FILES, TURBO_REPO, TURBO_REVISION


def checksum(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024**2), b''):
            digest.update(block)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--verify-only', action='store_true')
    args = parser.parse_args()
    root = args.directory
    root.mkdir(parents=True, exist_ok=True)
    for name, expected in TURBO_FILES.items():
        target = root / name
        if target.is_file() and checksum(target) == expected:
            continue
        if args.verify_only:
            raise RuntimeError(f'Missing or incorrect curated Turbo file: {name}')
        temporary = target.with_suffix('.download')
        try:
            with urllib.request.urlopen(f'https://huggingface.co/{TURBO_REPO}/resolve/{TURBO_REVISION}/{name}', timeout=120) as response, temporary.open('wb') as out:
                while block := response.read(1024**2):
                    out.write(block)
            if checksum(temporary) != expected:
                raise RuntimeError(f'Checksum mismatch: {name}')
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
    manifest = dict(repo=TURBO_REPO, revision=TURBO_REVISION, files=TURBO_FILES)
    path = root / 'benchmark-manifest.json'
    if args.verify_only:
        existing = json.loads(path.read_text())
        if any(existing.get(k) != v for k, v in manifest.items()):
            raise RuntimeError('Model manifest does not match curated Turbo')
    else:
        temporary = root / 'benchmark-manifest.json.part'
        temporary.write_text(json.dumps(manifest, indent=2) + '\n')
        temporary.replace(path)
    print('Turbo model verified')


if __name__ == '__main__':
    main()
