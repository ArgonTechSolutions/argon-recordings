"""Explicit, checksum-verified download. Usage: PYTHONPATH=backend python scripts/prepare_embeddings.py DIRECTORY"""
import hashlib
from pathlib import Path
import sys
import urllib.request
from argon_recordings.embeddings import PROFILES

def checksum(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024**2), b''):
            digest.update(block)
    return digest.hexdigest()


root = Path(sys.argv[1])
REPO, REVISION, FILES = PROFILES[sys.argv[2] if len(sys.argv) > 2 else "large"]
for name, expected in FILES.items():
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and checksum(target) == expected:
        continue
    temporary = target.with_suffix('.download')
    try:
        with urllib.request.urlopen(f'https://huggingface.co/{REPO}/resolve/{REVISION}/{name}', timeout=120) as response, temporary.open('wb') as out:
            while block := response.read(1024**2):
                out.write(block)
        if checksum(temporary) != expected:
            raise RuntimeError(f'Checksum mismatch: {name}')
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
print('Embedding model prepared and verified:', root)
