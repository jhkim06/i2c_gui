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
from urllib.parse import urlencode

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


def layout_measurement_legend(fig, ax) -> None:
    """Keep the single-column plot dimensions and widen the legend area as needed."""
    handles, labels = ax.get_legend_handles_labels()
    labels_per_column = 30
    columns = max(1, (len(labels) + labels_per_column - 1) // labels_per_column)
    legend_options = dict(loc="upper left", bbox_to_anchor=(1.01, 1), fontsize=10)
    legend = ax.legend(handles[:labels_per_column], labels[:labels_per_column], **legend_options)
    fig.tight_layout()
    if columns == 1:
        return

    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    single_column_right = legend.get_window_extent(renderer).x1
    position = ax.get_position().frozen()
    width = fig.get_figwidth()
    legend = ax.legend(handles, labels, ncol=columns, **legend_options)
    extra_width = max(0, legend.get_window_extent(renderer).x1 - single_column_right) / fig.dpi

    fig.set_figwidth(width + extra_width)
    scale = width / fig.get_figwidth()
    ax.set_position([position.x0 * scale, position.y0, position.width * scale, position.height])


def place_scatter_labels(fig, ax, labels, points) -> int:
    """Find nearby label positions in display coordinates after figure layout."""
    from matplotlib.transforms import Bbox

    if not labels:
        return 0
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    pixels_per_point = renderer.points_to_pixels(1)
    bounds = ax.bbox.padded(-4 * pixels_per_point)
    point_padding = 5 * pixels_per_point
    occupied = [
        Bbox.from_extents(x - point_padding, y - point_padding,
                          x + point_padding, y + point_padding)
        for x, y in ax.transData.transform(points)
    ]
    # Search closest offsets first, keeping the usual position for isolated points.
    offsets = sorted(
        ((dx, dy) for dx in range(-240, 241, 12) for dy in range(-240, 241, 12)),
        key=lambda offset: offset[0] ** 2 + offset[1] ** 2,
    )
    unresolved = 0
    for label in labels:
        original = label.get_window_extent(renderer).padded(2 * pixels_per_point)
        for dx, dy in offsets:
            candidate = original.translated(dx * pixels_per_point, dy * pixels_per_point)
            if not (bounds.contains(candidate.x0, candidate.y0)
                    and bounds.contains(candidate.x1, candidate.y1)):
                continue
            if any(candidate.overlaps(other) for other in occupied):
                continue
            offset = (5 + dx, dy)
            label.set_position(offset)
            occupied.append(candidate)
            if dx or dy:
                ax.annotate(
                    "", xy=label.xy, xytext=offset, textcoords="offset points",
                    arrowprops={"arrowstyle": "-", "color": "0.45", "lw": 0.6,
                                "shrinkA": 2, "shrinkB": 4},
                    zorder=2,
                )
            break
        else:
            occupied.append(original)
            unresolved += 1
    return unresolved


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
    parser.add_argument(
        "--nw-threshold", type=float, metavar="X",
        help="print the start time of each complete scan with any NW value greater than X",
    )
    parser.add_argument(
        "--highlight-above-threshold", action="store_true",
        help="overlay scans, coloring and labeling only those above --nw-threshold",
    )
    parser.add_argument(
        "--mean-std", action="store_true",
        help="save a mean vs. standard deviation scatter plot alongside the per-measurement histogram",
    )
    parser.add_argument(
        "--skip-all-zero", action="store_true",
        help="exclude complete scans with zero NW in every pixel (pool only complete scans)",
    )
    parser.add_argument(
        "--std-label-threshold", type=float, metavar="X",
        help="label scatter points with their ETROC name when NW standard deviation exceeds X",
    )
    parser.add_argument(
        "--clickable-labels", action="store_true",
        help="also save a PDF with ETROC labels linked to the CERN plot gallery",
    )
    args = parser.parse_args()
    if args.clickable_labels and args.std_label_threshold is None:
        parser.error("--clickable-labels requires --std-label-threshold")
    if args.std_label_threshold is not None:
        args.mean_std = True
    if args.mean_std:
        args.per_measurement = True
    if args.highlight_above_threshold:
        if args.nw_threshold is None:
            parser.error("--highlight-above-threshold requires --nw-threshold")
        args.per_measurement = True
        args.show_legend = True

    db_override = args.database
    if db_override is None and not os.environ.get("ETROC_SQLITE_OUTPUT_DIR"):
        db_override = pi_repo / "helpers" / "output" / "BaselineHistory.sqlite"

    try:
        hybrids = batch_members(args.batch_file.expanduser(), args.batch)
        if not hybrids:
            raise ValueError(f"no hybrids assigned to batch {args.batch!r}")
        database = database_path(db_override)
        if args.per_measurement or args.skip_all_zero or args.nw_threshold is not None:
            scans, skipped = batch_scans(database, hybrids)
            if skipped:
                print(f"Skipped {skipped} incomplete or malformed scan(s) in scan analysis", file=sys.stderr)
        if args.skip_all_zero:
            scan_count = len(scans)
            scans = [scan for scan in scans if any(scan[2])]
            zero_count = scan_count - len(scans)
            if zero_count:
                print(f"Skipped {zero_count} all-zero NW scan(s)", file=sys.stderr)
            if not scans:
                raise ValueError(f"no complete scans with nonzero NW found for batch {args.batch!r}")
        above_threshold = set()
        if args.nw_threshold is not None:
            for index, (name, start_time, scan_values) in enumerate(scans, 1):
                peak = max(scan_values)
                if peak > args.nw_threshold:
                    print(f"{name}: measurement start {start_time} (max NW {peak})")
                    above_threshold.add(index)
            if not above_threshold:
                print(f"No complete scans with NW greater than {args.nw_threshold:g}")
        if args.per_measurement or args.skip_all_zero:
            found = {name for name, _, _ in scans}
            entries = sum(len(scan_values) for _, _, scan_values in scans)
            if not args.per_measurement:
                values = [nw for _, _, scan_values in scans for nw in scan_values]
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

        stats_output = output.with_name(f"{output.stem}_mean_std.png")
        hep.style.use("CMS")
        fig, ax = plt.subplots(figsize=(14 if args.per_measurement else 10, 10))
        stats_fig = None
        try:
            if args.mean_std:
                stats_fig, stats_ax = plt.subplots(figsize=(14, 10))
                stats_labels, stats_points = [], []
            title = f"Batch {args.batch}"
            #note = f"{len(found)} hybrids, {entries:,} pixel entries"
            note = f"{len(found)} bare ETROCs"
            if args.per_measurement:
                colored_count = len(above_threshold) if args.highlight_above_threshold else len(scans)
                colors = (plt.get_cmap("tab20").colors if colored_count <= 20
                          else plt.get_cmap("hsv")(np.arange(colored_count) / colored_count))
                color_index = 0
                for index, (name, time, scan_values) in enumerate(scans, 1):
                    pixel_values = np.asarray(scan_values)
                    gray = args.highlight_above_threshold and index not in above_threshold
                    if gray:
                        scan_label, color, zorder = "_nolegend_", "0.65", 0.5
                    else:
                        scan_label = f"{index}. {name}  {time}"
                        color, zorder = to_hex(colors[color_index]), 1
                        color_index += 1
                    plot_nw_histogram(
                        ax, pixel_values, label=scan_label, color=color,
                        zorder=zorder, histtype="step",
                    )
                    if stats_fig is not None:
                        mean_nw = pixel_values.mean()
                        std_nw = pixel_values.std()
                        stats_points.append((mean_nw, std_nw))
                        stats_ax.scatter(mean_nw, std_nw, label=scan_label, color=color, zorder=zorder)
                        if args.std_label_threshold is not None and std_nw > args.std_label_threshold:
                            annotation = stats_ax.annotate(
                                name, (mean_nw, std_nw), xytext=(5, 0),
                                textcoords="offset points", ha="left", va="center", fontsize=10,
                                rotation=45, rotation_mode="anchor",
                            )
                            if args.clickable_labels:
                                gallery = (
                                    "https://jhkim.web.cern.ch/MTD/Hybrid%20sensor%20probe%20system/"
                                    "Plots/Hybrid_QA_QC/ETROC-figures/index.php"
                                )
                                query = urlencode({"path": f"by_hybrid_name/{name}"})
                                annotation.set_url(f"{gallery}?{query}")
                            annotation.set_in_layout(False)
                            stats_labels.append(annotation)
                #format_nw_axis(ax, title, f"{len(scans)} scans, {note}")
                format_nw_axis(ax, title, f"{note}")
            else:
                draw_nw_1d(ax, np.asarray(values), title, note)
            show_legend = args.per_measurement and args.show_legend and (
                not args.highlight_above_threshold or bool(above_threshold)
            )
            if show_legend:
                layout_measurement_legend(fig, ax)
            else:
                fig.tight_layout()
            fig.savefig(output, format="png")
            if stats_fig is not None:
                hep.cms.text(loc=0, ax=stats_ax, fontsize=17, text="ETL ETROC")
                stats_ax.set_title(f"{title}: NW mean vs. standard deviation", loc="right", fontsize=17)
                stats_ax.set_xlabel("Mean NW [DAC LSB]")
                stats_ax.set_ylabel("Standard deviation of NW [DAC LSB]")
                if show_legend:
                    layout_measurement_legend(stats_fig, stats_ax)
                else:
                    stats_fig.tight_layout()
                unresolved = place_scatter_labels(stats_fig, stats_ax, stats_labels, stats_points)
                if unresolved:
                    print(f"Could not find clear space for {unresolved} scatter label(s)", file=sys.stderr)
                stats_fig.savefig(stats_output, format="png")
                print(f"Saved {stats_output} ({len(scans)} scans)")
                if args.clickable_labels:
                    pdf_output = stats_output.with_suffix(".pdf")
                    stats_fig.savefig(pdf_output, format="pdf")
                    print(f"Saved {pdf_output} (clickable ETROC labels)")
        finally:
            plt.close(fig)
            if stats_fig is not None:
                plt.close(stats_fig)
    except (OSError, sqlite3.Error, ValueError, ImportError) as error:
        print(f"Batch NW plot failed: {error}", file=sys.stderr)
        return 1

    print(f"Saved {output} ({len(found)} hybrids, {entries:,} pixel entries)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


