#!/usr/bin/env python3
"""Draw an IV curve from helpers/output/IVHistory.sqlite.

Example:
  python helpers/draw_iv_curve.py --datetime 20260625_212514 --hybrid W12_21+87694_9
  python helpers/draw_iv_curve.py --datetime "2026-06-25 21:25" --hybrid W12_21+87694_9

The script is intentionally separate from the SMU scan loop so old IV data can be
re-plotted after removing obvious SMU/compliance overflow points.
"""

from __future__ import annotations

import argparse
import math
import re
import sqlite3
from pathlib import Path
from typing import Any

DEFAULT_DB = Path(__file__).resolve().parent / "output" / "IVHistory.sqlite"


def normalize_datetime_prefix(value: str) -> str:
    """Return a prefix matching IVHistory run_timestamp format YYYYMMDD_HHMMSS."""
    text = value.strip()
    if re.fullmatch(r"\d{8}_\d{0,6}", text):
        return text
    digits = re.sub(r"\D", "", text)
    if len(digits) < 8:
        raise ValueError("datetime must include at least YYYY-MM-DD or YYYYMMDD")
    if len(digits) <= 8:
        return digits
    return f"{digits[:8]}_{digits[8:14]}"


def fetch_rows(db_path: Path, datetime_value: str, hybrid: str, *, ignore_case: bool) -> list[sqlite3.Row]:
    prefix = normalize_datetime_prefix(datetime_value)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    chip_expr = "lower(chip_name) = lower(?)" if ignore_case else "chip_name = ?"
    rows = conn.execute(
        f"""
        SELECT *
        FROM iv_measurements
        WHERE run_timestamp LIKE ?
          AND {chip_expr}
        ORDER BY run_timestamp, step, id
        """,
        (prefix + "%", hybrid),
    ).fetchall()
    conn.close()
    return rows


def is_bad_current(row: sqlite3.Row, current_col: str, args: argparse.Namespace) -> tuple[bool, str]:
    value = row[current_col]
    if value is None:
        return True, "missing current"
    try:
        current = float(value)
    except (TypeError, ValueError):
        return True, "non-numeric current"
    if not math.isfinite(current):
        return True, "non-finite current"

    raw_col = current_col.replace("_current_A", "_raw")
    raw = str(row[raw_col] if raw_col in row.keys() else "")
    if re.search(r"over|overflow|ovr|inf|nan", raw, flags=re.IGNORECASE):
        return True, "raw looks like overflow"

    if args.current_min is not None and current < args.current_min:
        return True, f"current < {args.current_min:g} A"
    if args.current_max is not None and current > args.current_max:
        return True, f"current > {args.current_max:g} A"

    limit = row["current_limit_A"]
    if args.drop_compliance and limit is not None:
        try:
            limit = abs(float(limit))
        except (TypeError, ValueError):
            limit = None
        if limit and abs(current) >= args.compliance_fraction * limit:
            return True, f"near current limit ({current:g} A vs {limit:g} A)"

    if args.drop_positive and current > args.positive_threshold:
        return True, f"positive current > {args.positive_threshold:g} A"

    return False, ""


def make_plot(rows: list[sqlite3.Row], kept: list[sqlite3.Row], output: Path, current_col: str, *, title_note: str) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    points = sorted(
        (
            abs(float(row["applied_voltage_V"])),
            abs(float(row[current_col])) * 1e6,
            row,
        )
        for row in kept
        if row["applied_voltage_V"] is not None and row[current_col] is not None
    )
    if not points:
        raise RuntimeError("No valid IV points left after filtering")

    hv = [p[0] for p in points]
    current_uA = [p[1] for p in points]
    chip_name = kept[0]["chip_name"] or "unknown hybrid"
    run_timestamp = kept[0]["run_timestamp"]

    fig, ax = plt.subplots(figsize=(8, 6))
    ax.plot(hv, current_uA, "o-", color="#4285F4", linewidth=2.0, markersize=4)
    ax.set_xlabel("HV magnitude (V)")
    ax.set_ylabel(f"{chip_name} current |I| (µA)")
    title = f"IV curve: {chip_name}\nrun {run_timestamp}"
    if title_note:
        title += f" — {title_note}"
    ax.set_title(title)
    ax.grid(True, color="#d9d9d9", linewidth=1.0)
    ax.set_axisbelow(True)
    fig.tight_layout()
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, dpi=150)
    plt.close(fig)


def safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.+-]+", "_", text).strip("_")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Plot an IV curve from IVHistory.sqlite for a given datetime and hybrid/chip name.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help=f"SQLite DB path (default: {DEFAULT_DB})")
    parser.add_argument("--datetime", required=True, help="Run datetime/prefix, e.g. 20260625_212514 or '2026-06-25 21:25'")
    parser.add_argument("--hybrid", "--chip", dest="hybrid", required=True, help="Hybrid/chip name stored in chip_name")
    parser.add_argument("--current-column", default="before_current_A", choices=["before_current_A", "after_current_A"], help="Current column to plot")
    parser.add_argument("--output", type=Path, default=None, help="Output PNG path. Default: helpers/output/IVcurve_<datetime>_<hybrid>.png")
    parser.add_argument("--case-sensitive", action="store_true", help="Require exact case match for hybrid/chip name")

    parser.add_argument("--keep-compliance", dest="drop_compliance", action="store_false", help="Keep points close to current_limit_A")
    parser.set_defaults(drop_compliance=True)
    parser.add_argument("--compliance-fraction", type=float, default=0.995, help="Drop |I| >= fraction*current_limit_A (default: 0.995)")
    parser.add_argument("--drop-positive", action="store_true", help="Drop positive currents above --positive-threshold. Useful for reverse-biased Keithley overflow artifacts.")
    parser.add_argument("--positive-threshold", type=float, default=0.0, help="Threshold for --drop-positive in A (default: 0)")
    parser.add_argument("--current-min", type=float, default=None, help="Drop currents below this raw value [A]")
    parser.add_argument("--current-max", type=float, default=None, help="Drop currents above this raw value [A]")
    parser.add_argument("--print-points", action="store_true", help="Print kept points and dropped rows")
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    if not args.db.exists():
        raise FileNotFoundError(args.db)

    rows = fetch_rows(args.db, args.datetime, args.hybrid, ignore_case=not args.case_sensitive)
    if not rows:
        raise SystemExit(f"No rows found for datetime={args.datetime!r}, hybrid={args.hybrid!r} in {args.db}")

    kept: list[sqlite3.Row] = []
    dropped: list[tuple[sqlite3.Row, str]] = []
    for row in rows:
        bad, reason = is_bad_current(row, args.current_column, args)
        if bad:
            dropped.append((row, reason))
        else:
            kept.append(row)

    if args.output is None:
        prefix = normalize_datetime_prefix(args.datetime).rstrip("_")
        args.output = args.db.parent / f"IVcurve_{safe_name(prefix)}_{safe_name(args.hybrid)}.png"

    note = f"{len(kept)}/{len(rows)} points kept"
    make_plot(rows, kept, args.output, args.current_column, title_note=note)

    print(f"Wrote {args.output}")
    print(f"Matched rows: {len(rows)}, kept: {len(kept)}, dropped: {len(dropped)}")
    if dropped:
        reasons: dict[str, int] = {}
        for _, reason in dropped:
            reasons[reason] = reasons.get(reason, 0) + 1
        print("Dropped summary:")
        for reason, count in sorted(reasons.items()):
            print(f"  {count:3d}  {reason}")

    if args.print_points:
        print("\nKept points:")
        for row in kept:
            print(f"  step={row['step']:>3} V={row['applied_voltage_V']:>8g} I={row[args.current_column]:.6g} A raw={row['before_raw']}")
        if dropped:
            print("\nDropped rows:")
            for row, reason in dropped:
                print(f"  step={row['step']:>3} V={row['applied_voltage_V']:>8g} I={row[args.current_column]} A reason={reason} raw={row['before_raw']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
