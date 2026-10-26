"""Run the shared report contract in a built plugin or the repository checkout."""
from pathlib import Path
import sys

try:
    from report_contract import VERSION, ContractError, Evidence, Reference, Report, main, validate_report
except ModuleNotFoundError as exc:
    if exc.name != "report_contract":
        raise
    # The package builder places the same module next to this script.
    sys.path.insert(0, str(Path(__file__).resolve().parents[6] / "backend"))
    from argon_recordings.report_contract import VERSION, ContractError, Evidence, Reference, Report, main, validate_report


if __name__ == "__main__":
    main()
