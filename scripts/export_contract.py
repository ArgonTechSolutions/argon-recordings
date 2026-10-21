"""Run with PYTHONPATH=backend; check or write the actual OpenAPI contract."""
import argparse
import json
from pathlib import Path
from argon_recordings.api import create_app

parser = argparse.ArgumentParser()
parser.add_argument('--check', action='store_true')
args = parser.parse_args()
path = Path(__file__).resolve().parents[1] / 'docs' / 'openapi.json'
content = json.dumps(create_app().openapi(), indent=2) + '\n'
if args.check:
    if path.read_text() != content:
        raise SystemExit('OpenAPI contract changed; regenerate it and frontend schema types')
else:
    path.write_text(content)
