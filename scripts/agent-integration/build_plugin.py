"""Build only the explicit plugin sources; never include local grants, recordings or environments."""
import argparse
from pathlib import Path
import zipfile


ROOT = Path(__file__).resolve().parent
FILES = (
    "plugin/plugin.json", "plugin/mcp.json", "server.py", "requirements.txt",
    "plugin/skills/recording-summary/SKILL.md",
    "plugin/skills/recording-summary/agents/openai.yaml",
    "plugin/skills/recording-summary/references/output-contract.md",
    "plugin/skills/recording-summary/scripts/summary_contract.py",
)


def build(output):
    sources = []
    for relative in FILES:
        source = ROOT / relative
        if (source.is_symlink() or not source.is_file() or not source.resolve().is_relative_to(ROOT)
                or source.resolve() == Path(output).resolve()):
            raise ValueError("Plugin source must be a contained regular file distinct from the output")
        sources.append((source, relative.removeprefix("plugin/")))
    contract = ROOT.parents[1] / 'backend/argon_recordings/report_contract.py'
    if contract.is_symlink() or not contract.is_file() or contract.resolve() != contract or contract == Path(output).resolve():
        raise ValueError('Shared report contract must be a regular repository source file')
    sources.append((contract, 'skills/recording-summary/scripts/report_contract.py'))
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for source, target in sources:
            archive.writestr(target, source.read_bytes())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    build(args.output)
