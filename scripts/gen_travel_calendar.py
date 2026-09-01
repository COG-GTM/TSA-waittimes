"""Generate the static US travel-demand calendar."""
import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.travel_calendar import build_periods


def main() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--years",
        nargs="+",
        type=int,
        default=None,
        help="calendar years to generate (default: current year and next two)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=repo_root / "data" / "travel_calendar.json",
        help="output JSON path",
    )
    args = parser.parse_args()
    current_year = datetime.now(UTC).year
    years = args.years or list(range(current_year, current_year + 3))
    periods = build_periods(years)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps([period.to_json() for period in periods], indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
