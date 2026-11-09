#!/usr/bin/env python3
"""Plot pooled or overlaid 1D noise-width histograms for a hybrid batch."""

import argparse
from contextlib import closing
from datetime import datetime
import os
from pathlib import Path
import re
import sqlite3
import sys

from export_bl_nw_json import database_path


def expand_hybrids(spec: str) -> list[str]:
    if "~" not in spec:
        return [spec]
    first, _, last = spec.partition("~")
    endpoints = [re.fullmatch(r"(.*_)([0-9]+)", name.strip()) for name in (first, last)]
    if any(match is None for match in endpoints):
        raise ValueError(f"invalid hybrid range {spec!r}")
    (prefix, start), (end_prefix, end) = [match.groups() for match in endpoints]
    if prefix != end_prefix or len(start) != len(end) or int(start) > int(end):
        raise ValueError(f"invalid hybrid range {spec!r}")
    return [f"{prefix}{number:0{len(start)}d}" for number in range(int(start), int(end) + 1)]


def batch_members(path: Path, batch: str) -> list[str]:
    """Read a two-column hybrid/batch mapping."""
    members = {}
    hybrid_first = True
    first_record = True
    for line_number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        content = line.partition("#")[0].strip()
        if not content:
            continue
        fields = re.split(r"\s*[,;]\s*", content, maxsplit=1)
        if len(fields) == 1:
            fields = content.split(maxsplit=1)
        if len(fields) == 1 and "~" in content:
            continue  # An unlabeled range does not assign a batch.
        if len(fields) != 2 or not all(fields):
            raise ValueError(f"{path}:{line_number}: expected a hybrid and batch label")
        if first_record:
            first_record = False
            header = [field.casefold() for field in fields]
            hybrid_headers = {"hybrid", "hybrid_name", "chip_name"}
            if header[0] in hybrid_headers and header[1] == "batch":
                continue
            if header[0] == "batch" and header[1] in hybrid_headers:
                hybrid_first = False
                continue
        hybrid_spec, category = fields if hybrid_first else fields[::-1]
        try:
            hybrids = expand_hybrids(hybrid_spec)
        except ValueError as error:
            raise ValueError(f"{path}:{line_number}: {error}") from error
        for hybrid in hybrids:
            if hybrid in members and members[hybrid] != category:
                raise ValueError(f"{path}:{line_number}: conflicting batch assignments for {hybrid}")
            members[hybrid] = category
    return [hybrid for hybrid, category in members.items() if category == batch]


def batch_rows(database: Path, hybrids: list[str], *, by_scan: bool = False):
    columns = "chip_name, row, col, timestamp, noise_width" if by_scan else "chip_name, noise_width"
    extra_filter = "ORDER BY chip_name, rowid" if by_scan else "AND noise_width IS NOT NULL"
    with closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)) as connection:
        for start in range(0, len(hybrids), 900):
            names = hybrids[start:start + 900]
            placeholders = ",".join("?" for _ in names)
            yield from connection.execute(
                f"SELECT {columns} FROM baselines WHERE chip_name IN ({placeholders}) {extra_filter}",
                names,
            )


def valid_nw(name: str, value: int) -> int:
    if type(value) is not int or not 0 <= value < 16:
        raise ValueError(f"{name}: invalid noise_width {value!r}, expected an integer from 0 to 15")
    return value


def batch_noise_widths(database: Path, hybrids: list[str]) -> tuple[list[int], set[str]]:
    values = []
    found = set()
    for name, value in batch_rows(database, hybrids):
        values.append(valid_nw(name, value))
        found.add(name)
    return values, found


def batch_scans(database: Path, hybrids: list[str]):
    """Recover complete scans from row-major pixel coordinates in insertion order."""
    scans = []
    skipped = 0
    name, timestamp = None, None
    pixels = []
    expected = [(row, col) for row in range(16) for col in range(16)]
    for chip, row, col, time, value in batch_rows(database, hybrids, by_scan=True):
        if chip != name or (row, col) == (0, 0):
            if pixels:
                skipped += 1
            name, timestamp, pixels = chip, time, []
        pixels.append(((row, col), value))
        if len(pixels) == len(expected):
            if [coord for coord, _ in pixels] == expected and all(v is not None for _, v in pixels):
                scans.append((name, timestamp, [valid_nw(name, v) for _, v in pixels]))
            else:
                skipped += 1
            pixels = []
    return scans, skipped + bool(pixels)


def main() -> int:
    pi_repo = Path.home() / "ETL" / "i2c_gui"
    figure_root = Path(os.environ.get(
        "ETROC_FIGURE_ROOT", Path.home() / "ETL" / "cernbox-www-plots" / "ETROC-figures"
    )).expanduser()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", required=True, help="exact batch label in the mapping")
    parser.add_argument(
        "--batch-file", type=Path, default=figure_root / "by_hybrid_name" / "batch.txt",
        help="hybrid or hybrid range, followed by its batch label (default: %(default)s)",
    )
    parser.add_argument(
        "--database", type=Path,
        help="default: $ETROC_SQLITE_OUTPUT_DIR/BaselineHistory.sqlite, or helpers/output on the Pi",
    )
    parser.add_argument("--output", type=Path, help="output PNG (default: timestamped PNG in the current directory)")
    parser.add_argument(
        "--per-measurement", action="store_true",
        help="overlay one colored histogram per complete 16×16 scan",
    )
    parser.add_argument(
        "--show-legend", action="store_true",
        help="label each histogram in the per-measurement overlay",
    )
    args = parser.parse_args()

    db_override = args.database
    if db_override is None and not os.environ.get("ETROC_SQLITE_OUTPUT_DIR"):
        db_override = pi_repo / "helpers" / "output" / "BaselineHistory.sqlite"

    try:
        hybrids = batch_members(args.batch_file.expanduser(), args.batch)
        if not hybrids:
            raise ValueError(f"no hybrids assigned to batch {args.batch!r}")
        database = database_path(db_override)
        if args.per_measurement:
            scans, skipped = batch_scans(database, hybrids)
            found = {name for name, _, _ in scans}
            entries = sum(len(values) for _, _, values in scans)
            if skipped:
                print(f"Skipped {skipped} incomplete or malformed scan(s)", file=sys.stderr)
        else:
            values, found = batch_noise_widths(database, hybrids)
            entries = len(values)
        missing = sorted(set(hybrids) - found)
        if missing:
            print(f"No NW measurements for: {', '.join(missing)}", file=sys.stderr)
        if not entries:
            raise ValueError(f"no NW measurements found for batch {args.batch!r}")

        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.colors import to_hex
        import mplhep as hep
        import numpy as np
        from i2c_gui2_helpers import draw_nw_1d, format_nw_axis, plot_nw_histogram

        label = re.sub(r"[^A-Za-z0-9_-]+", "_", args.batch).strip("_") or "selection"
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        suffix = "_per_measurement" if args.per_measurement else ""
        output = (args.output or Path(f"batch_{label}_NW_1D_hist{suffix}_{timestamp}.png")).expanduser()
        output.parent.mkdir(parents=True, exist_ok=True)

        hep.style.use("CMS")
        height = max(10, len(scans) * 0.22 + 2) if args.per_measurement and args.show_legend else 10
        fig, ax = plt.subplots(figsize=(14 if args.per_measurement else 10, height))
        try:
            title = f"Batch {args.batch}"
            note = f"{len(found)} hybrids, {entries:,} pixel entries"
            if args.per_measurement:
                colors = (plt.get_cmap("tab20").colors if len(scans) <= 20
                          else plt.get_cmap("hsv")(np.arange(len(scans)) / len(scans)))
                for index, (name, time, scan_values) in enumerate(scans, 1):
                    plot_nw_histogram(
                        ax, np.asarray(scan_values), label=f"{index}. {name}  {time}",
                        color=to_hex(colors[index - 1]), histtype="step",
                    )
                format_nw_axis(ax, title, f"{len(scans)} scans, {note}")
                if args.show_legend:
                    ax.legend(loc="upper left", bbox_to_anchor=(1.01, 1), fontsize=10)
            else:
                draw_nw_1d(ax, np.asarray(values), title, note)
            fig.tight_layout()
            fig.savefig(output, format="png")
        finally:
            plt.close(fig)
    except (OSError, sqlite3.Error, ValueError, ImportError) as error:
        print(f"Batch NW plot failed: {error}", file=sys.stderr)
        return 1

    print(f"Saved {output} ({len(found)} hybrids, {entries:,} pixel entries)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
