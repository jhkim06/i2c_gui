#!/usr/bin/env python3
"""Redraw saved BL/NW and IV plots without accessing measurement hardware.

Use --all for both histories. The original single-scan --chip-name/--start/
--end interface remains available.
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
from pathlib import Path

PIXELS = {(row, col) for row in range(16) for col in range(16)}


def connect_read_only(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(path)
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def baseline_scans(path: Path, chip_name: str | None = None):
    """Group by the next (0,0) row, never by chip name or notes alone."""
    with connect_read_only(path) as connection:
        query = "SELECT rowid, * FROM baselines"
        params = ()
        if chip_name:
            query += " WHERE chip_name = ?"
            params = (chip_name,)
        scan = []
        for row in connection.execute(query + " ORDER BY rowid", params):
            if (row["row"], row["col"]) == (0, 0):
                if scan:
                    yield scan
                scan = [row]
            elif scan:
                scan.append(row)
        if scan:
            yield scan


def valid_scan(rows) -> bool:
    return (len(rows) == 256
            and len({row["chip_name"] for row in rows}) == 1
            and {(row["row"], row["col"]) for row in rows} == PIXELS
            and all(row["baseline"] is not None and row["noise_width"] is not None
                    for row in rows))


def iv_runs(path: Path, chip_name: str | None = None):
    with connect_read_only(path) as connection:
        query = "SELECT * FROM iv_measurements"
        params = ()
        if chip_name:
            query += " WHERE chip_name = ?"
            params = (chip_name,)
        key = None
        run = []
        for row in connection.execute(query + " ORDER BY run_timestamp, chip_name, step", params):
            new_key = (row["run_timestamp"], row["chip_name"])
            if key is not None and new_key != key:
                yield run
                run = []
            key = new_key
            run.append(dict(row))
        if run:
            yield run


def figure_dir(root: Path, chip_name: str) -> Path:
    from smu_etroc_calibration_loop import safe_filename_part

    return root / "by_hybrid_name" / safe_filename_part(chip_name)


def baseline_timestamp(rows) -> str:
    # Original figure-save time is not in SQLite. Last pixel time is closest.
    return str(rows[-1]["timestamp"]).split(".", 1)[0].replace(" ", "_").replace(":", "-")


def draw_baseline(rows, directory: Path, *, dry_run: bool, overwrite: bool,
                  duplicate_timestamp: bool = False) -> int:
    from i2c_gui2_helpers import i2c_connection

    chip = str(rows[0]["chip_name"])
    stamp = baseline_timestamp(rows)
    if duplicate_timestamp:
        stamp += f"_rowid{rows[0]['rowid']}"
    paths = [directory / f"{chip}_BL_NW_{kind}_{stamp}.png"
             for kind in ("2D_map", "1D_hist")]
    if any(path.exists() for path in paths) and not overwrite:
        print(f"Skip existing BL/NW scan: {chip}, rowid {rows[0]['rowid']}")
        return 0
    if dry_run:
        for path in paths:
            print(f"Would draw: {path}")
        return 2
    import pandas as pd

    directory.mkdir(parents=True, exist_ok=True)
    data = pd.DataFrame([dict(row) for row in rows])
    pivot = data.pivot(index="row", columns="col", values=["baseline", "noise_width"])
    notes = data["save_notes"].dropna().unique()
    note = str(notes[0]) if len(notes) else ""
    import matplotlib.pyplot as plt

    try:
        i2c_connection.make_BL_NW_2D_maps(pivot, chip, note, directory, stamp)
        i2c_connection.make_BL_NW_1D_hists(data, chip, note, directory, stamp)
    finally:
        # The acquisition helpers keep figures open for interactive use. A
        # historical batch must release them after each 256-pixel scan.
        plt.close("all")
    return 2


def run_note(rows: list[dict]) -> str:
    # New per-step notes end in BV=<voltage>V. Legacy voltage-specific notes
    # cannot reconstruct the original run-level note, so omit them.
    notes = [str(row.get("save_notes") or "") for row in rows]
    bases = [re.sub(r"(?:\s+)?BV=-?\d+(?:\.\d+)?V$", "", note).strip()
             for note in notes]
    return bases[0] if bases and all(base == bases[0] for base in bases) else ""


def draw_iv(rows: list[dict], directory: Path, *, sweep: str,
            dry_run: bool, overwrite: bool) -> int:
    from smu_etroc_calibration_loop import (
        plot_iv_curve, safe_filename_part, select_iv_sweep_rows,
    )

    chip = str(rows[0]["chip_name"] or "unknown")
    note = run_note(rows)
    label = safe_filename_part(f"{chip}_{note}" if note else chip)
    path = directory / f"{label}_IV_curve_{rows[0]['run_timestamp']}.png"
    if not select_iv_sweep_rows(rows, sweep):
        print(f"Skip IV run without plottable current: {chip}, {rows[0]['run_timestamp']}")
        return 0
    if path.exists() and not overwrite:
        print(f"Skip existing IV plot: {path}")
        return 0
    if dry_run:
        print(f"Would draw: {path}")
        return 1
    return int(plot_iv_curve(rows, path, chip_name=chip, save_notes=note,
                             sweep=sweep) is not None)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all", action="store_true",
                        help="Redraw all complete BL/NW scans and IV runs")
    parser.add_argument("--output-dir", type=Path,
                        default=Path(os.environ.get("ETROC_SQLITE_OUTPUT_DIR",
                                                     Path(__file__).parent / "output")))
    parser.add_argument("--figure-root", type=Path,
                        default=Path(os.environ["ETROC_LOCAL_FIGURE_ROOT"])
                        if os.environ.get("ETROC_LOCAL_FIGURE_ROOT") else None)
    parser.add_argument("--chip-name", help="Optional exact chip_name filter with --all")
    parser.add_argument("--start", help="Single-scan start timestamp, inclusive")
    parser.add_argument("--end", help="Single-scan end timestamp, inclusive")
    parser.add_argument("--db", type=Path, help="Single-scan SQLite override")
    parser.add_argument("--outdir", type=Path, help="Single-scan plot directory")
    parser.add_argument("--iv-sweep", choices=("up", "down", "both"), default="up")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.all:
        if args.start or args.end or args.db or args.outdir:
            parser.error("--start, --end, --db and --outdir are single-scan options")
        if args.figure_root is None or not args.figure_root.is_absolute():
            parser.error("set ETROC_LOCAL_FIGURE_ROOT or pass an absolute --figure-root")
    elif not (args.chip_name and args.start and args.end):
        parser.error("single-scan mode requires --chip-name, --start and --end")

    baseline_path = args.db or args.output_dir / "BaselineHistory.sqlite"
    iv_path = args.output_dir / "IVHistory.sqlite"
    count = skipped = 0
    if args.all:
        if not baseline_path.is_file() or not iv_path.is_file():
            parser.error("both SQLite histories must exist in --output-dir")
        seen_stamps: set[tuple[str, str]] = set()
        for rows in baseline_scans(baseline_path, args.chip_name):
            if not valid_scan(rows):
                skipped += 1
                continue
            stamp_key = (str(rows[0]["chip_name"]), baseline_timestamp(rows))
            count += draw_baseline(rows, figure_dir(args.figure_root, str(rows[0]["chip_name"])),
                                   dry_run=args.dry_run, overwrite=args.overwrite,
                                   duplicate_timestamp=stamp_key in seen_stamps)
            seen_stamps.add(stamp_key)
        for rows in iv_runs(iv_path, args.chip_name):
            count += draw_iv(rows, figure_dir(args.figure_root, str(rows[0]["chip_name"] or "unknown")),
                             sweep=args.iv_sweep, dry_run=args.dry_run, overwrite=args.overwrite)
    else:
        with connect_read_only(baseline_path) as connection:
            rows = connection.execute(
                "SELECT rowid, * FROM baselines WHERE chip_name = ? AND timestamp >= ? "
                "AND timestamp <= ? ORDER BY rowid",
                (args.chip_name, args.start, args.end),
            ).fetchall()
        if not valid_scan(rows):
            parser.error("time window must select exactly one complete 256-pixel scan")
        directory = args.outdir or (figure_dir(args.figure_root, args.chip_name)
                                    if args.figure_root else Path.cwd())
        count = draw_baseline(rows, directory, dry_run=args.dry_run, overwrite=args.overwrite)
    print(f"{'Planned' if args.dry_run else 'Saved'} {count} plot(s); "
          f"skipped {skipped} incomplete BL/NW scan(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
