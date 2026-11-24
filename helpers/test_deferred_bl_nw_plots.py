"""Hardware-free checks for deferred BL/NW plot jobs."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from i2c_gui2_helpers import i2c_connection
from render_deferred_bl_nw_plots import render_job
from smu_etroc_calibration_loop import run_calibration


def fake_scan(baseline: int) -> pd.DataFrame:
    return pd.DataFrame([
        {"row": row, "col": col, "baseline": baseline, "noise_width": 5,
         "chip_name": "H1"}
        for row in range(16) for col in range(16)
    ])


class DeferredBLNWTests(unittest.TestCase):
    def test_job_reads_only_its_own_saved_scan(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            env = {"ETROC_LOCAL_FIGURE_ROOT": str(root / "figures"),
                   "ETROC_SQLITE_OUTPUT_DIR": str(root / "output"),
                   "ETROC_FIGURE_ROOT": str(root / "remote")}
            with patch.dict(os.environ, env):
                connection = i2c_connection.__new__(i2c_connection)
                connection.conn = None  # The test never opens an I2C connection.
                connection.chip_addresses = [0x60]
                connection.chip_names = ["H1"]
                jobs = []
                for baseline in (100, 500):
                    connection.BL_df = {0x60: fake_scan(baseline)}
                    captured = io.StringIO()
                    with contextlib.redirect_stdout(captured):
                        connection.save_baselines(hist_dir=root / "output", save_notes="BV=-100V",
                                                  defer_plots=True)
                    jobs.append(Path(captured.getvalue().split("DEFERRED_BL_NW_JOB=")[1].splitlines()[0]))
                self.assertFalse(list((root / "figures").rglob("*.png")))
                self.assertEqual(json.loads(jobs[1].read_text())["row_count"], 256)

                with patch.object(i2c_connection, "make_BL_NW_2D_maps", return_value=root / "map.png") as map_plot, \
                     patch.object(i2c_connection, "make_BL_NW_1D_hists", return_value=root / "hist.png") as hist_plot, \
                     patch("render_deferred_bl_nw_plots.log_unsynced_plot") as sync:
                    render_job(jobs[1])
                self.assertEqual(map_plot.call_args.args[0]["baseline"].to_numpy().min(), 500)
                self.assertEqual(hist_plot.call_args.args[0]["baseline"].min(), 500)
                self.assertEqual(sync.call_count, 2)
                self.assertTrue(jobs[0].exists())
                self.assertFalse(jobs[1].exists())

    def test_smu_child_flag_is_separate_from_no_show_plots(self) -> None:
        for deferred in (False, True):
            with self.subTest(deferred=deferred), \
                 patch("smu_etroc_calibration_loop.subprocess.Popen", side_effect=RuntimeError("stop")) as popen, \
                 contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, "stop"):
                    run_calibration(python_exe="python", i2c_script=Path("/mock/i2c_test_with_rpi.py"),
                                    chip_name="H1", save_notes="BV=-100V", plot_footer_metadata="Operator: mock",
                                    log_path=None, output_dir=Path("/mock"), timeout_s=None,
                                    defer_bl_nw_plots=deferred)
            command = popen.call_args.args[0]
            self.assertIn("--no-show-plots", command)
            self.assertEqual("--defer-bl-nw-plots" in command, deferred)

    def test_existing_save_path_still_renders_without_defer(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            env = {"ETROC_LOCAL_FIGURE_ROOT": str(root / "figures"),
                   "ETROC_SQLITE_OUTPUT_DIR": str(root / "output"),
                   "ETROC_FIGURE_ROOT": str(root / "remote")}
            with patch.dict(os.environ, env), \
                 patch.object(i2c_connection, "make_BL_NW_2D_maps", return_value=root / "map.png") as map_plot, \
                 patch.object(i2c_connection, "make_BL_NW_1D_hists", return_value=root / "hist.png") as hist_plot, \
                 patch("i2c_gui2_helpers.log_unsynced_plot"):
                connection = i2c_connection.__new__(i2c_connection)
                connection.conn = None
                connection.chip_addresses = [0x60]
                connection.chip_names = ["H1"]
                connection.BL_df = {0x60: fake_scan(400)}
                connection.save_baselines(hist_dir=root / "output", save_notes="normal",
                                          preview_plots=False)
            map_plot.assert_called_once()
            hist_plot.assert_called_once()
            self.assertFalse(list((root / "output").rglob("*.json")))

    def test_incomplete_saved_scan_keeps_job_for_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            env = {"ETROC_LOCAL_FIGURE_ROOT": str(root / "figures"),
                   "ETROC_SQLITE_OUTPUT_DIR": str(root / "output"),
                   "ETROC_FIGURE_ROOT": str(root / "remote")}
            with patch.dict(os.environ, env), contextlib.redirect_stdout(io.StringIO()):
                connection = i2c_connection.__new__(i2c_connection)
                connection.conn = None
                connection.chip_addresses = [0x60]
                connection.chip_names = ["H1"]
                connection.BL_df = {0x60: fake_scan(400)}
                connection.save_baselines(hist_dir=root / "output", defer_plots=True)
                job = next((root / "output" / "deferred_bl_nw_plots").glob("*.json"))
                with sqlite3.connect(root / "output" / "BaselineHistory.sqlite") as db:
                    db.execute("DELETE FROM baselines WHERE rowid=1")
                with self.assertRaisesRegex(ValueError, "do not match"):
                    render_job(job)
                self.assertTrue(job.exists())


if __name__ == "__main__":
    unittest.main()
