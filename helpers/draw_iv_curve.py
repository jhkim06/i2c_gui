#!/usr/bin/env python3
"""Draw IV curves from helpers/output/IVHistory.sqlite.

Examples:
  # Re-draw every stored IV measurement into ETROC-figures/IV
  python helpers/draw_iv_curve.py --all

  # Re-draw one run
  python helpers/draw_iv_curve.py --datetime 20260625_212514 --hybrid W12_21+87694_9
  python helpers/draw_iv_curve.py --datetime "2026-06-25 21:25" --hybrid W12_21+87694_9

  # Re-draw selected chip/date pairs
  python helpers/draw_iv_curve.py --list W03_100:20260811_221126 W03_72:20260811_221122

  # Draw selected chip/date pairs together in one plot
  python helpers/draw_iv_curve.py --list W03_100:20260811_221126 W03_72:20260811_221122 --one-plot

  # Draw grouped selections from a text file with columns: CHIP DATETIME LABEL
  # This writes one combined plot per label, e.g. FBK_IV_curves.png and HPK_IV_curves.png
  python helpers/draw_iv_curve.py --group-file iv_groups.txt

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

REPO_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DB = Path(__file__).resolve().parent / "output" / "IVHistory.sqlite"
DEFAULT_OUTPUT_DIR = REPO_DIR / "ETROC-figures" / "IV"


def scale_y_for_legend(ax: Any, *, max_iterations: int = 12) -> None:
    """Increase the y-axis upper limit if the legend overlaps plotted data.

    Prefer mplhep's yscale_legend utility when mplhep is available.  Keep a
    small matplotlib-only fallback so IV plotting still works on DAQ machines
    where mplhep is not installed.
    """
    legend = ax.get_legend()
    if legend is None:
        return

    try:
        import mplhep as hep  # type: ignore[import-not-found]

        hep.yscale_legend(ax=ax, soft_fail=True, N=max_iterations)
        return
    except Exception:
        pass

    fig = ax.figure

    def legend_overlaps_plotted_data() -> bool:
        fig.canvas.draw()
        renderer = fig.canvas.get_renderer()
        current_legend_bbox = legend.get_window_extent(renderer=renderer).expanded(1.03, 1.08)
        for line in ax.lines:
            if not line.get_visible() or len(line.get_xdata(orig=False)) == 0:
                continue
            path = line.get_path().transformed(line.get_transform())
            if path.get_extents().overlaps(current_legend_bbox):
                return True
        return False

    if not legend_overlaps_plotted_data():
        return

    for _ in range(max_iterations):
        ymin, ymax = ax.get_ylim()
        if ax.get_yscale() == "log" and ymin > 0 and ymax > 0:
            ax.set_ylim(ymin, ymax * 1.35)
        else:
            ax.set_ylim(ymin, ymin + (ymax - ymin) * 1.35)
        if not legend_overlaps_plotted_data():
            break


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


def parse_chip_datetime_selection(value: str) -> tuple[str, str]:
    """Parse CHIP:DATETIME selection strings used by --list."""
    if ":" not in value:
        raise argparse.ArgumentTypeError(
            f"selection {value!r} must use CHIP:DATETIME, e.g. W03_100:20260811_221126"
        )
    chip, datetime_value = value.split(":", 1)
    chip = chip.strip()
    datetime_value = datetime_value.strip()
    if not chip or not datetime_value:
        raise argparse.ArgumentTypeError(
            f"selection {value!r} must use non-empty CHIP:DATETIME"
        )
    try:
        normalize_datetime_prefix(datetime_value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"selection {value!r} has invalid datetime: {exc}") from exc
    return chip, datetime_value


def parse_group_file(path: Path) -> dict[str, list[tuple[str, str]]]:
    """Parse a whitespace-delimited selection file with lines: CHIP DATETIME LABEL.

    Blank lines and lines starting with # are ignored. Inline comments are also
    allowed after the three required columns.
    """
    groups: dict[str, list[tuple[str, str]]] = {}
    with path.open(encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.split("#", 1)[0].strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) < 3:
                raise ValueError(
                    f"{path}:{line_number}: expected at least 3 columns: CHIP DATETIME LABEL"
                )
            chip, datetime_value, label = parts[:3]
            try:
                normalize_datetime_prefix(datetime_value)
            except ValueError as exc:
                raise ValueError(f"{path}:{line_number}: invalid datetime {datetime_value!r}: {exc}") from exc
            if not chip or not label:
                raise ValueError(f"{path}:{line_number}: CHIP and LABEL must be non-empty")
            groups.setdefault(label, []).append((chip, datetime_value))

    if not groups:
        raise ValueError(f"No grouped IV selections found in {path}")
    return groups


def fetch_all_run_keys(db_path: Path) -> list[tuple[str, str]]:
    """Return every distinct IV run key as (run_timestamp, chip_name)."""
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        """
        SELECT run_timestamp, COALESCE(chip_name, 'unknown') AS chip_name
        FROM iv_measurements
        GROUP BY run_timestamp, chip_name
        ORDER BY run_timestamp, chip_name
        """
    ).fetchall()
    conn.close()
    return [(str(run_timestamp), str(chip_name)) for run_timestamp, chip_name in rows]


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


def select_iv_sweep_rows(rows: list[sqlite3.Row], current_col: str, sweep: str) -> list[sqlite3.Row]:
    """Return rows for the requested IV sweep direction.

    "up" is the initial increasing-HV-magnitude part of the scan. "down" starts
    at the high-voltage turning point and follows the decreasing-HV-magnitude
    part. "both" keeps every valid row, preserving the old plotting behavior.
    """
    valid_rows = [
        row for row in rows
        if row["applied_voltage_V"] is not None and row[current_col] is not None
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
        # Monotonic scans have no separate down-sweep, so keep all rows.
        return valid_rows
    if sweep == "up":
        return valid_rows[:turn_index]
    if sweep == "down":
        return valid_rows[turn_index - 1:]
    raise ValueError(f"Unsupported IV sweep selection: {sweep!r}")


def iv_points_for_plot(rows: list[sqlite3.Row], current_col: str, sweep: str) -> list[tuple[float, float, int, int, sqlite3.Row]]:
    """Return sorted (|HV|, |I| in µA, step, id, row) points for plotting."""
    selected = select_iv_sweep_rows(rows, current_col, sweep)
    return sorted(
        (
            abs(float(row["applied_voltage_V"])),
            abs(float(row[current_col])) * 1e6,
            int(row["step"]),
            int(row["id"]),
            row,
        )
        for row in selected
    )


def make_plot(rows: list[sqlite3.Row], kept: list[sqlite3.Row], output: Path, current_col: str, *, title_note: str, sweep: str) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    selected = select_iv_sweep_rows(kept, current_col, sweep)
    points = iv_points_for_plot(kept, current_col, sweep)
    if not points:
        raise RuntimeError("No valid IV points left after filtering/sweep selection")

    hv = [p[0] for p in points]
    current_uA = [p[1] for p in points]
    chip_name = selected[0]["chip_name"] or "unknown hybrid"
    run_timestamp = selected[0]["run_timestamp"]

    fig, ax = plt.subplots(figsize=(8, 6))
    ax.plot(hv, current_uA, "o-", color="#4285F4", linewidth=2.0, markersize=4)
    ax.set_xlabel("HV magnitude (V)")
    ax.set_ylabel(f"{chip_name} current |I| (µA)")
    title = f"IV curve: {chip_name}\nrun {run_timestamp}\nIV sweep: {sweep} ({len(selected)}/{len(kept)} kept points)"
    if title_note:
        title += f" — {title_note}"
    ax.set_title(title)
    ax.grid(True, color="#d9d9d9", linewidth=1.0)
    ax.set_axisbelow(True)
    fig.tight_layout()
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, dpi=150)
    plt.close(fig)


def make_combined_plot(curves: list[dict[str, Any]], output: Path, current_col: str, *, sweep: str, title_label: str | None = None) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(9, 6.5))
    for curve in curves:
        points = iv_points_for_plot(curve["kept_rows"], current_col, sweep)
        if not points:
            raise RuntimeError(f"No valid IV points left for {curve['hybrid']}:{curve['datetime']}")
        hv = [p[0] for p in points]
        current_uA = [p[1] for p in points]
        label = f"{curve['hybrid']} ({curve['run_timestamp']})"
        ax.plot(hv, current_uA, "o-", linewidth=2.0, markersize=4, label=label)

    ax.set_xlabel("Bias Voltage (V)")
    ax.set_ylabel("Current (µA)")
    title_prefix = f"{title_label} IV curves" if title_label else "IV curves"
    ax.set_title(f"{title_prefix} — {len(curves)} runs\nIV sweep: {sweep}")
    ax.grid(True, color="#d9d9d9", linewidth=1.0)
    ax.set_axisbelow(True)
    ax.legend(fontsize="small")
    scale_y_for_legend(ax)
    fig.tight_layout()
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, dpi=150)
    plt.close(fig)


def safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.+-]+", "_", text).strip("_")


def default_output_path(output_dir: Path, run_timestamp: str, hybrid: str) -> Path:
    """Build the standard IV-figure filename under ETROC-figures/IV."""
    return output_dir / f"{safe_name(hybrid)}_IV_curve_{safe_name(run_timestamp)}.png"


def filter_rows(rows: list[sqlite3.Row], args: argparse.Namespace) -> tuple[list[sqlite3.Row], list[tuple[sqlite3.Row, str]]]:
    kept: list[sqlite3.Row] = []
    dropped: list[tuple[sqlite3.Row, str]] = []
    for row in rows:
        bad, reason = is_bad_current(row, args.current_column, args)
        if bad:
            dropped.append((row, reason))
        else:
            kept.append(row)
    return kept, dropped


def draw_one_run(rows: list[sqlite3.Row], args: argparse.Namespace, output: Path) -> tuple[int, int, int]:
    """Filter and draw one IV run. Returns (kept, selected, dropped)."""
    kept, dropped = filter_rows(rows, args)
    selected = select_iv_sweep_rows(kept, args.current_column, args.iv_plot_sweep)
    note = f"{len(kept)}/{len(rows)} points kept before sweep selection"
    make_plot(rows, kept, output, args.current_column, title_note=note, sweep=args.iv_plot_sweep)
    return len(kept), len(selected), len(dropped)


def draw_selection(datetime_value: str, hybrid: str, args: argparse.Namespace, *, output: Path | None = None) -> tuple[Path, int, int, int, int]:
    """Draw one selected run. Returns (output, matched, kept, selected, dropped)."""
    rows = fetch_rows(args.db, datetime_value, hybrid, ignore_case=not args.case_sensitive)
    if not rows:
        raise RuntimeError(f"No rows found for datetime={datetime_value!r}, hybrid={hybrid!r} in {args.db}")

    if output is None:
        prefix = normalize_datetime_prefix(datetime_value).rstrip("_")
        output = default_output_path(args.output_dir, prefix, hybrid)

    kept, selected, dropped = draw_one_run(rows, args, output)
    return output, len(rows), kept, selected, dropped


def collect_selection_for_combined_plot(datetime_value: str, hybrid: str, args: argparse.Namespace) -> dict[str, Any]:
    """Fetch/filter one selected run for a combined plot."""
    rows = fetch_rows(args.db, datetime_value, hybrid, ignore_case=not args.case_sensitive)
    if not rows:
        raise RuntimeError(f"No rows found for datetime={datetime_value!r}, hybrid={hybrid!r} in {args.db}")

    kept, dropped = filter_rows(rows, args)
    selected = select_iv_sweep_rows(kept, args.current_column, args.iv_plot_sweep)
    if not selected:
        raise RuntimeError(f"No valid IV points left after filtering/sweep selection for {hybrid}:{datetime_value}")

    return {
        "datetime": datetime_value,
        "hybrid": hybrid,
        "run_timestamp": selected[0]["run_timestamp"],
        "matched": len(rows),
        "kept": len(kept),
        "selected": len(selected),
        "dropped": len(dropped),
        "kept_rows": kept,
    }


def draw_combined_selection(selections: list[tuple[str, str]], args: argparse.Namespace, output: Path, *, title_label: str | None = None) -> tuple[Path, list[dict[str, Any]], list[tuple[str, str, str]]]:
    """Draw selected CHIP:DATETIME pairs together in one plot."""
    curves: list[dict[str, Any]] = []
    skipped: list[tuple[str, str, str]] = []
    for hybrid, datetime_value in selections:
        try:
            curves.append(collect_selection_for_combined_plot(datetime_value, hybrid, args))
        except Exception as exc:
            skipped.append((datetime_value, hybrid, str(exc)))

    if not curves:
        raise RuntimeError("No selected IV curves could be drawn")

    make_combined_plot(curves, output, args.current_column, sweep=args.iv_plot_sweep, title_label=title_label)
    return output, curves, skipped


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Plot IV curves from IVHistory.sqlite.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help=f"SQLite DB path (default: {DEFAULT_DB})")
    parser.add_argument("--all", action="store_true", help=f"Re-draw every IV run into --output-dir (default: {DEFAULT_OUTPUT_DIR})")
    parser.add_argument("--list", nargs="+", type=parse_chip_datetime_selection, metavar="CHIP:DATETIME", help="Re-draw selected chip/date pairs, e.g. --list W03_100:20260811_221126 W03_72:20260811_221122")
    parser.add_argument("--group-file", type=Path, help="Text file with whitespace-separated columns: CHIP DATETIME LABEL. Writes one combined IV plot per LABEL, e.g. FBK and HPK.")
    parser.add_argument("--one-plot", "--combined", dest="one_plot", action="store_true", help="With --list, draw all selected IV curves together in one output plot")
    parser.add_argument("--datetime", help="Run datetime/prefix, e.g. 20260625_212514 or '2026-06-25 21:25'")
    parser.add_argument("--hybrid", "--chip", dest="hybrid", help="Hybrid/chip name stored in chip_name")
    parser.add_argument("--current-column", default="before_current_A", choices=["before_current_A", "after_current_A"], help="Current column to plot")
    parser.add_argument("--iv-plot-sweep", "--iv-sweep", dest="iv_plot_sweep", default="up", choices=["up", "down", "both"], help="Which sweep to draw in the IV plot: up, down, or both. Default: up. --iv-sweep is kept as a deprecated alias.")
    parser.add_argument("--output", type=Path, default=None, help="Output PNG path for a single run. Default: ETROC-figures/IV/<hybrid>_IV_curve_<datetime>.png")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help=f"Directory for --all outputs (default: {DEFAULT_OUTPUT_DIR})")
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

    modes = sum(bool(value) for value in (args.all, args.list, args.group_file, args.datetime or args.hybrid))
    if modes != 1:
        raise SystemExit("Use exactly one mode: --all, --list CHIP:DATETIME [...], --group-file FILE, or both --datetime and --hybrid/--chip")

    if args.one_plot and not args.list:
        raise SystemExit("--one-plot/--combined can only be used with --list")

    if args.output is not None and (args.all or args.group_file or args.list and len(args.list) > 1 and not args.one_plot):
        raise SystemExit("--output can only be used when drawing one run, or with --list --one-plot")

    if args.all:
        run_keys = fetch_all_run_keys(args.db)
        if not run_keys:
            raise SystemExit(f"No IV runs found in {args.db}")

        args.output_dir.mkdir(parents=True, exist_ok=True)
        written = 0
        skipped: list[tuple[str, str, str]] = []
        for run_timestamp, hybrid in run_keys:
            rows = fetch_rows(args.db, run_timestamp, hybrid, ignore_case=False)
            output = default_output_path(args.output_dir, run_timestamp, hybrid)
            try:
                kept, selected, dropped = draw_one_run(rows, args, output)
            except Exception as exc:  # keep batch re-draw going if one bad run exists
                skipped.append((run_timestamp, hybrid, str(exc)))
                print(f"SKIP {run_timestamp} {hybrid}: {exc}")
                continue
            written += 1
            print(
                f"Wrote {output} "
                f"(rows={len(rows)}, kept={kept}, selected_{args.iv_plot_sweep}={selected}, dropped={dropped})"
            )

        print(f"\nDone. Wrote {written}/{len(run_keys)} IV plots into {args.output_dir}")
        if skipped:
            print("Skipped runs:")
            for run_timestamp, hybrid, reason in skipped:
                print(f"  {run_timestamp} {hybrid}: {reason}")
        return 0 if not skipped else 1

    if args.group_file:
        if not args.group_file.exists():
            raise FileNotFoundError(args.group_file)

        args.output_dir.mkdir(parents=True, exist_ok=True)
        try:
            groups = parse_group_file(args.group_file)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc

        written = 0
        all_skipped: list[tuple[str, str, str, str]] = []
        for label, selections in groups.items():
            output = args.output_dir / f"{safe_name(label)}_IV_curves.png"
            try:
                output, curves, skipped = draw_combined_selection(selections, args, output, title_label=label)
            except Exception as exc:
                print(f"SKIP group {label}: {exc}")
                all_skipped.append((label, "*", "*", str(exc)))
                continue

            written += 1
            print(f"Wrote {output}")
            for curve in curves:
                print(
                    f"  [{label}] {curve['hybrid']}:{curve['datetime']} "
                    f"(run={curve['run_timestamp']}, rows={curve['matched']}, kept={curve['kept']}, "
                    f"selected_{args.iv_plot_sweep}={curve['selected']}, dropped={curve['dropped']})"
                )
            if skipped:
                print(f"Skipped selections for group {label}:")
                for datetime_value, hybrid, reason in skipped:
                    print(f"  [{label}] {hybrid}:{datetime_value}: {reason}")
                    all_skipped.append((label, datetime_value, hybrid, reason))

        print(f"\nDone. Wrote {written}/{len(groups)} grouped IV plots into {args.output_dir}")
        if all_skipped:
            print("Skipped grouped selections:")
            for label, datetime_value, hybrid, reason in all_skipped:
                print(f"  [{label}] {hybrid}:{datetime_value}: {reason}")
        return 0 if not all_skipped else 1

    if args.list:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        if args.one_plot:
            output = args.output or (args.output_dir / "selected_IV_curves.png")
            try:
                output, curves, skipped = draw_combined_selection(args.list, args, output)
            except Exception as exc:
                raise SystemExit(str(exc)) from exc
            print(f"Wrote {output}")
            for curve in curves:
                print(
                    f"  {curve['hybrid']}:{curve['datetime']} "
                    f"(run={curve['run_timestamp']}, rows={curve['matched']}, kept={curve['kept']}, "
                    f"selected_{args.iv_plot_sweep}={curve['selected']}, dropped={curve['dropped']})"
                )
            if skipped:
                print("Skipped selections:")
                for datetime_value, hybrid, reason in skipped:
                    print(f"  {hybrid}:{datetime_value}: {reason}")
            return 0 if not skipped else 1

        written = 0
        skipped: list[tuple[str, str, str]] = []
        for hybrid, datetime_value in args.list:
            try:
                output, matched, kept, selected, dropped = draw_selection(
                    datetime_value,
                    hybrid,
                    args,
                    output=args.output if len(args.list) == 1 else None,
                )
            except Exception as exc:
                skipped.append((datetime_value, hybrid, str(exc)))
                print(f"SKIP {datetime_value} {hybrid}: {exc}")
                continue
            written += 1
            print(
                f"Wrote {output} "
                f"(rows={matched}, kept={kept}, selected_{args.iv_plot_sweep}={selected}, dropped={dropped})"
            )

        print(f"\nDone. Wrote {written}/{len(args.list)} selected IV plots into {args.output_dir}")
        if skipped:
            print("Skipped selections:")
            for datetime_value, hybrid, reason in skipped:
                print(f"  {hybrid}:{datetime_value}: {reason}")
        return 0 if not skipped else 1

    if not args.datetime or not args.hybrid:
        raise SystemExit("Either use --all, --list CHIP:DATETIME [...], or provide both --datetime and --hybrid/--chip")

    output, matched, kept, selected, dropped = draw_selection(args.datetime, args.hybrid, args, output=args.output)

    print(f"Wrote {output}")
    print(f"Matched rows: {matched}, kept: {kept}, selected for {args.iv_plot_sweep} plot sweep: {selected}, dropped: {dropped}")

    if args.print_points:
        rows = fetch_rows(args.db, args.datetime, args.hybrid, ignore_case=not args.case_sensitive)
        kept_rows, dropped_rows = filter_rows(rows, args)
        print("\nKept points:")
        for row in kept_rows:
            print(f"  step={row['step']:>3} V={row['applied_voltage_V']:>8g} I={row[args.current_column]:.6g} A raw={row['before_raw']}")
        if dropped_rows:
            print("\nDropped rows:")
            for row, reason in dropped_rows:
                print(f"  step={row['step']:>3} V={row['applied_voltage_V']:>8g} I={row[args.current_column]} A reason={reason} raw={row['before_raw']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
