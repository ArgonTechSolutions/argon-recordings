"""Run from a fresh clone to enable updates in an archive installation."""
import argparse
import importlib.util
from pathlib import Path
import sys

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
from scripts.git_updates import bootstrap, update

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('installation', type=Path)
args = parser.parse_args()
try:
    bootstrap(SOURCE, args.installation)
    spec = importlib.util.spec_from_file_location('recordings_bootstrap', SOURCE / 'recordings.py')
    installer = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = installer
    spec.loader.exec_module(installer)
    installer.ROOT = args.installation.resolve()
    update(installer, installer.load_config(installer.ROOT / 'recordings.toml'))
except Exception as exc:
    parser.exit(1, f'Bootstrap stopped: {exc}\n')
