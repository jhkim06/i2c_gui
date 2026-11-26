# Rebuild saved ETROC plots

With the measurement virtual environment active:

```bash
source ~/ETL/i2c_gui/helpers/setup_i2c_measurement_env.sh
python redraw_BL_NW_2D_from_sqlite.py --all --dry-run
python redraw_BL_NW_2D_from_sqlite.py --all
python redraw_BL_NW_2D_from_sqlite.py --all --only iv
python redraw_BL_NW_2D_from_sqlite.py --all --only bl-nw
```

`--all` reads `BaselineHistory.sqlite` and `IVHistory.sqlite` from
`ETROC_SQLITE_OUTPUT_DIR` and writes BL/NW 2D maps, BL/NW 1D histograms, and
IV curves under `ETROC_LOCAL_FIGURE_ROOT/by_hybrid_name/<chip_name>/`.
`--only iv` or `--only bl-nw` limits the batch to one plot type and only
requires that type's SQLite history. Use either option together with `--all`.
It uses the acquisition scripts' plotting functions and filename patterns.
Use `--chip-name NAME` to limit the batch, `--iv-sweep up|down|both` to choose
the IV direction, and `--overwrite` to replace existing PNGs. By default,
existing files are skipped. The database is opened read-only; no SMU or ETROC
is contacted.

Only complete 256-pixel BL/NW scans are drawn. If two scans finish in the same
second, the later filename gets a `_rowid<first-rowid>` suffix to avoid a
collision. The original plot-save timestamp is not stored in SQLite, so the
redraw uses the final pixel timestamp instead. Similarly, plot-footer metadata
is not stored in these histories and cannot be recovered by this command.
For older IV rows with per-step notes that do not share a recoverable base note,
the redraw omits the original run-level note rather than guessing it.

The original single-scan form is still supported:

```bash
python redraw_BL_NW_2D_from_sqlite.py --chip-name W15_019 \
  --start '2026-11-25 12:00:00' --end '2026-11-25 12:05:00' \
  --db output/BaselineHistory.sqlite --outdir /tmp/redraw-preview
```
