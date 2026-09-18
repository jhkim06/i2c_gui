#!/usr/bin/env python3
"""Draw IV curves from an IVHistory SQLite database over a date range.

This script makes one IV plot per measurement run (run_timestamp), using the same
plot style as smu_etroc_calibration_loop.py::plot_iv_curve.

Examples:
  python plot_iv_history_date_range.py --start-date 2026-09-14 --end-date 2026-09-14
  python plot_iv_history_date_range.py --date 2026-09-14 --chip-name W03_79
  python plot_iv_history_date_range.py --start-date 2026-09-01 --end-date 2026-09-14 --sweep both
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any

DEFAULT_DB = Path("/home/ellie/ETL/i2c_gui/helpers/output/IVHistory_knumtd.sqlite")
DEFAULT_OUTPUT_DIR = Path("/home/ellie/ETL/i2c_gui/helpers/output/iv_history_plots")


def safe_filename_part(text: str) -> str:
    """Return a filesystem-friendly label."""
    cleaned = re.sub(r"[^A-Za-z0-9_.+-]+", "_", str(text).strip())
    cleaned = cleaned.strip("._")
    return cleaned or "unknown"


def parse_date(value: str | None, *, option_name: str) -> dt.date | None:
    if value is None:
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{option_name} must be YYYY-MM-DD, got {value!r}") from exc


def run_date_from_timestamp(run_timestamp: str) -> dt.date:
    """Parse timestamps like 20260914_163621 into a date."""
    try:
        return dt.datetime.strptime(run_timestamp, "%Y%m%d_%H%M%S").date()
    except ValueError as exc:
        raise ValueError(f"run_timestamp has unsupported format: {run_timestamp!r}") from exc


def select_iv_sweep_rows(rows: list[dict[str, Any]], sweep: str) -> list[dict[str, Any]]:
    """Return rows for the requested IV sweep direction.

    Same behavior as smu_etroc_calibration_loop.py:
      - up: initial increasing-HV-magnitude part
      - down: high-voltage turning point through decreasing-HV-magnitude part
      - both: all valid rows
    """
    valid_rows = [
        row
        for row in rows
        if row.get("before_current_A") is not None and row.get("applied_voltage_V") is not None
    ]
    if sweep == "both" or len(valid_rows) < 2:
        return valid_rows

    hv = [abs(float(row["applied_voltage_V"])) for row in valid_rows]
    turn_index: int | None = None
    for idx in range(1, len(hv)):
        if hv[idx] < hv[idx - 1]:
            turn_index = idx
            break

    if turn_index is None:
        return valid_rows
    if sweep == "up":
        return valid_rows[:turn_index]
    if sweep == "down":
        return valid_rows[turn_index - 1 :]
    raise ValueError(f"Unsupported IV sweep selection: {sweep!r}")


def plot_iv_curve(
    rows: list[dict[str, Any]],
    plot_path: Path,
    *,
    chip_name: str,
    save_notes: str,
    sweep: str = "up",
) -> bool:
    """Draw an IV curve with the same style as smu_etroc_calibration_loop.py."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    selected_rows = select_iv_sweep_rows(rows, sweep)
    if not selected_rows:
        return False

    points = sorted(
        (
            abs(float(row["applied_voltage_V"])),
            abs(float(row["before_current_A"])) * 1e6,
        )
        for row in selected_rows
    )
    hv = [point[0] for point in points]
    current_uA = [point[1] for point in points]

    fig, ax = plt.subplots(figsize=(8, 6))
    ax.plot(hv, current_uA, "o-", color="#4285F4", linewidth=2.0, markersize=4)
    ax.set_xlabel("HV (V)")
    ax.set_ylabel(f"{chip_name} current (µA)")
    title = f"{chip_name} vs HV (V)"
    if save_notes:
        title += f"\n{save_notes}"
    valid_count = len([row for row in rows if row.get("before_current_A") is not None])
    title += f"\nIV sweep: {sweep} ({len(selected_rows)}/{valid_count} points)"
    ax.set_title(title)
    ax.grid(True, color="#d9d9d9", linewidth=1.0)
    ax.set_axisbelow(True)
    fig.tight_layout()
    plot_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(plot_path, dpi=150)
    plt.close(fig)
    return True


def load_rows(db_path: Path) -> list[dict[str, Any]]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT *
            FROM iv_measurements
            ORDER BY run_timestamp, step
            """
        ).fetchall()
    return [dict(row) for row in rows]


def group_runs(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    runs: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        runs[str(row["run_timestamp"])].append(row)
    return dict(runs)


def run_label(rows: list[dict[str, Any]]) -> tuple[str, str]:
    chip_name = str(next((row.get("chip_name") for row in rows if row.get("chip_name")), "chip"))
    run_timestamp = str(rows[0].get("run_timestamp", "run"))
    return chip_name, run_timestamp


def title_note_for_run(rows: list[dict[str, Any]], *, include_run_timestamp: bool = True) -> str:
    """Build a compact title note.

    The stored save_notes can vary by voltage step, so we avoid putting every
    per-step note in the title. The run timestamp keeps each measurement clear.
    """
    parts: list[str] = []
    if include_run_timestamp:
        parts.append(f"run_timestamp={rows[0].get('run_timestamp', '')}")
    step_times = [str(row.get("before_time")) for row in rows if row.get("before_time")]
    if step_times:
        parts.append(f"{min(step_times)} to {max(step_times)}")
    return " | ".join(parts)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Draw one IV curve per measurement from IVHistory SQLite for a date range."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help=f"IVHistory SQLite path. Default: {DEFAULT_DB}")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help=f"Directory for PNG plots. Default: {DEFAULT_OUTPUT_DIR}")
    parser.add_argument("--date", help="Single date to plot, YYYY-MM-DD. Equivalent to matching start/end date.")
    parser.add_argument("--start-date", help="First run date to include, YYYY-MM-DD. Default: no lower bound.")
    parser.add_argument("--end-date", help="Last run date to include, YYYY-MM-DD. Inclusive. Default: no upper bound.")
    parser.add_argument("--chip-name", help="Only plot runs with this chip_name.")
    parser.add_argument("--sweep", choices=["up", "down", "both"], default="up", help="IV sweep to draw. Default: up")
    parser.add_argument("--list-runs", action="store_true", help="List matching runs without drawing plots.")
    return parser


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()

    if args.date and (args.start_date or args.end_date):
        parser.error("Use either --date or --start-date/--end-date, not both.")

    if args.date:
        start_date = end_date = parse_date(args.date, option_name="--date")
    else:
        start_date = parse_date(args.start_date, option_name="--start-date")
        end_date = parse_date(args.end_date, option_name="--end-date")

    if start_date and end_date and start_date > end_date:
        parser.error("--start-date must be <= --end-date")

    if not args.db.exists():
        parser.error(f"SQLite file not found: {args.db}")

    rows = load_rows(args.db)
    runs = group_runs(rows)

    matching: list[tuple[str, list[dict[str, Any]]]] = []
    for run_timestamp, run_rows in sorted(runs.items()):
        run_date = run_date_from_timestamp(run_timestamp)
        if start_date and run_date < start_date:
            continue
        if end_date and run_date > end_date:
            continue
        if args.chip_name and not any(row.get("chip_name") == args.chip_name for row in run_rows):
            continue
        matching.append((run_timestamp, run_rows))

    if not matching:
        print("No matching IV measurements found.")
        return 1

    if args.list_runs:
        for run_timestamp, run_rows in matching:
            chip_name, _ = run_label(run_rows)
            run_date = run_date_from_timestamp(run_timestamp).isoformat()
            valid_points = len([row for row in run_rows if row.get("before_current_A") is not None])
            print(f"{run_timestamp}  date={run_date}  chip={chip_name}  points={valid_points}/{len(run_rows)}")
        return 0

    made = 0
    for run_timestamp, run_rows in matching:
        chip_name, _ = run_label(run_rows)
        filename = f"{safe_filename_part(chip_name)}_{run_timestamp}_IV_curve_{args.sweep}.png"
        plot_path = args.output_dir / filename
        if plot_iv_curve(
            run_rows,
            plot_path,
            chip_name=chip_name,
            save_notes=title_note_for_run(run_rows),
            sweep=args.sweep,
        ):
            made += 1
            print(plot_path)
        else:
            print(f"Skipped {run_timestamp}: no valid IV points")

    print(f"Created {made} IV plot(s) in {args.output_dir}")
    return 0 if made else 1


if __name__ == "__main__":
    raise SystemExit(main())
