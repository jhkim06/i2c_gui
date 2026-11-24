#!/usr/bin/env python3
"""Render a saved BL/NW scan after its bias output has been switched off."""

from __future__ import annotations

import argparse
import fcntl
import json
import sqlite3
from pathlib import Path


def render_job(job_path: Path) -> list[Path]:
    import pandas as pd
    from i2c_gui2_helpers import i2c_connection, log_unsynced_plot

    job = json.loads(job_path.read_text(encoding="utf-8"))
    sqlite_path = Path(job["sqlite_path"])
    with sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True) as connection:
        scan = pd.read_sql_query(
            "SELECT * FROM baselines WHERE rowid BETWEEN ? AND ? ORDER BY rowid",
            connection, params=(job["rowid_start"], job["rowid_end"]),
        )
    if len(scan) != job["row_count"] or len(scan) != 256 or scan["chip_name"].nunique() != 1 or scan["chip_name"].iloc[0] != job["chip_name"]:
        raise ValueError(f"Saved BL/NW rows do not match deferred job: {job_path}")
    if set(zip(scan["row"], scan["col"])) != {(row, col) for row in range(16) for col in range(16)}:
        raise ValueError(f"Incomplete pixel coordinates in deferred job: {job_path}")
    if not scan["save_notes"].eq(job["save_notes"]).all():
        raise ValueError(f"Saved notes do not match deferred job: {job_path}")

    pivot = scan.pivot(index="row", columns="col", values=["baseline", "noise_width"])
    paths: list[Path] = []
    for figure_dir_name in job["figure_dirs"]:
        figure_dir = Path(figure_dir_name)
        figure_dir.mkdir(parents=True, exist_ok=True)
        paths.append(i2c_connection.make_BL_NW_2D_maps(
            pivot, job["chip_name"], job["save_notes"], figure_dir,
            job["timestamp"], plot_footer_metadata=job["plot_footer_metadata"],
        ))
        paths.append(i2c_connection.make_BL_NW_1D_hists(
            scan, job["chip_name"], job["save_notes"], figure_dir,
            job["timestamp"], plot_footer_metadata=job["plot_footer_metadata"],
        ))
        if job["rail_readback"]:
            paths.append(i2c_connection.make_rail_readback_1D_plot(
                scan, job["chip_name"], job["save_notes"], figure_dir, job["timestamp"],
            ))
    for path in paths:
        log_unsynced_plot(path, Path(job["output_dir"]))
    job_path.unlink()
    return paths


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job", type=Path, help="Deferred BL/NW job JSON")
    args = parser.parse_args()
    # Only one renderer loads Matplotlib at a time on a memory-limited Pi.
    with (args.job.parent / ".render.lock").open("a") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        if not args.job.exists():
            print(f"Deferred BL/NW job already completed: {args.job}")
            return 0
        for path in render_job(args.job):
            print(f"Saved deferred BL/NW plot: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
