#!/usr/bin/env python3
"""Extract one complete ETROC BL or NW scan from SQLite into raw JSON."""

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import sqlite3
import sys


def local_timestamp(value: str) -> datetime:
    try:
        timestamp = datetime.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"invalid ISO date/time: {value}") from error
    if timestamp.tzinfo is not None:
        raise argparse.ArgumentTypeError("use a local date/time without an offset; set --timezone separately")
    return timestamp


def database_path(override: Path | None) -> Path:
    if override is not None:
        return override.expanduser().resolve()
    output_dir = os.environ.get("ETROC_SQLITE_OUTPUT_DIR")
    if not output_dir:
        raise ValueError("set ETROC_SQLITE_OUTPUT_DIR or pass --database")
    return (Path(output_dir).expanduser() / "BaselineHistory.sqlite").resolve()


def selected_scan(database: Path, chip_name: str, start: datetime, end: datetime) -> list[tuple]:
    if not database.is_file():
        raise ValueError(f"SQLite database not found: {database}")
    with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as connection:
        rows = connection.execute(
            "SELECT rowid, row, col, baseline, noise_width, timestamp, save_notes "
            "FROM baselines WHERE chip_name = ? AND timestamp >= ? AND timestamp < ? "
            "ORDER BY rowid",
            (chip_name, start.isoformat(sep=" "), end.isoformat(sep=" ")),
        ).fetchall()

    if len(rows) != 256:
        raise ValueError(f"selected {len(rows)} rows; choose a time range containing exactly one 256-pixel scan")
    coordinates = [(row[1], row[2]) for row in rows]
    expected = [(pixel_row, pixel_col) for pixel_row in range(16) for pixel_col in range(16)]
    if coordinates != expected:
        raise ValueError("selected rows are not one complete row-major 16x16 scan")
    if len({row[6] for row in rows}) != 1:
        raise ValueError("selected rows have different save notes; they may span scans")
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, help="default: $ETROC_SQLITE_OUTPUT_DIR/BaselineHistory.sqlite")
    parser.add_argument("--chip-name", required=True, help="exact chip_name stored in SQLite")
    parser.add_argument("--start", required=True, type=local_timestamp, help="inclusive local scan time")
    parser.add_argument("--end", required=True, type=local_timestamp, help="exclusive local scan time")
    parser.add_argument("--kind", required=True, choices=("nw", "bl"), help="noise width or baseline")
    parser.add_argument("--output", type=Path, help="new raw JSON file (default: CHIP_NAME_KIND.json)")
    args = parser.parse_args()

    if args.start >= args.end:
        parser.error("--start must precede --end")
    if not args.chip_name.strip():
        parser.error("--chip-name cannot be empty")
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", args.chip_name).strip("._")
    if not safe_name:
        parser.error("--chip-name cannot form a filename; pass --output")
    output_path = args.output or Path(f"{safe_name}_{args.kind}.json")
    try:
        rows = selected_scan(database_path(args.database), args.chip_name, args.start, args.end)
        value_index = 4 if args.kind == "nw" else 3
        values = [row[value_index] for row in rows]
        if any(type(value) is not int for value in values):
            raise ValueError("selected BL/NW values must all be integers")
        payload = {
            "format": "etl-bl-nw-scan-v1",
            "chip_name": args.chip_name,
            "kind": args.kind,
            "measurement_timestamp": rows[0][5],
            "first_rowid": rows[0][0],
            "last_rowid": rows[-1][0],
            "values": [values[index:index + 16] for index in range(0, 256, 16)],
        }
        with output_path.expanduser().open("x", encoding="utf-8") as output:
            json.dump(payload, output, indent=2)
            output.write("\n")
    except (OSError, sqlite3.Error, ValueError, KeyError) as error:
        print(f"Export failed: {error}", file=sys.stderr)
        return 1

    print(f"Exported {args.kind} for {args.chip_name} from SQLite rows {rows[0][0]}–{rows[-1][0]} to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
