"""Hardware-free checks for the fixed terminal display and child log streaming."""

from __future__ import annotations

import sys
import time
import unittest
import curses
import threading
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from measurement_tui import MeasurementTUI  # noqa: E402


class FakeScreen:
    def __init__(self, width: int = 100) -> None:
        self.width = width
        self.drawn: list[tuple[int, str, int]] = []
        self.positions: list[tuple[int, int, str]] = []
        self.keys = iter(())
        self.input_timeout = None

    def keypad(self, enabled: bool) -> None:
        pass

    def getmaxyx(self) -> tuple[int, int]:
        return 24, self.width

    def erase(self) -> None:
        self.drawn.clear()
        self.positions.clear()

    def addnstr(self, row: int, col: int, text: str, length: int, attr: int) -> None:
        self.drawn.append((row, text[:length], attr))
        self.positions.append((row, col, text[:length]))

    def refresh(self) -> None:
        pass

    def get_wch(self):
        return next(self.keys)

    def timeout(self, delay: int) -> None:
        self.input_timeout = delay


class TUITests(unittest.TestCase):
    def make_ui(self) -> tuple[MeasurementTUI, FakeScreen]:
        screen = FakeScreen()
        with patch("measurement_tui.curses.has_colors", return_value=True), \
             patch("measurement_tui.curses.start_color"), \
             patch("measurement_tui.curses.use_default_colors"), \
             patch("measurement_tui.curses.init_pair"), \
             patch("measurement_tui.curses.color_pair", side_effect=lambda pair: pair * 256), \
             patch("measurement_tui.curses.curs_set"):
            ui = MeasurementTUI(screen)
        return ui, screen

    def test_contact_and_i2c_detected_are_red(self) -> None:
        ui, screen = self.make_ui()
        ui.update(probe=True, bv=True, i2c="DETECTED", psu="ON", sample_name="H1")
        highlighted = [(row, text) for row, text, attr in screen.drawn if attr == ui.red]
        self.assertEqual(highlighted, [(1, "CONTACTED"), (1, "CONTACTED"),
                                       (2, "ON"), (2, "DETECTED")])
        ui.update(bv=False, psu="OFF", i2c="NOT DETECTED")
        highlighted = [(row, text) for row, text, attr in screen.drawn if attr == ui.red]
        self.assertEqual(highlighted, [(1, "CONTACTED")])
        inactive = [(row, text) for row, text, attr in screen.drawn if attr == ui.green]
        self.assertEqual(inactive, [(1, "NOT CONTACTED"), (2, "OFF"), (2, "NOT DETECTED")])

    def test_unconfirmed_psu_and_i2c_are_neutral(self) -> None:
        ui, screen = self.make_ui()
        ui.update(psu="UNKNOWN", i2c="CHECKING")
        self.assertNotIn("UNKNOWN", [text for _, text, attr in screen.drawn
                                      if attr in (ui.green, ui.red)])
        self.assertNotIn("CHECKING", [text for _, text, attr in screen.drawn
                                       if attr in (ui.green, ui.red)])

    def test_status_fields_share_a_right_column(self) -> None:
        ui, screen = self.make_ui()
        ui.update(sample_name="W15_073", sample_type="Hybrid", operator="jhkim",
                  probe=True, bv=True, i2c="DETECTED")
        right_labels = [(row, col) for row, col, text in screen.positions
                        if text.startswith(("Operator:", "BV tip:", "ETROC I²C"))]
        self.assertEqual(right_labels, [(0, 50), (1, 50), (2, 50)])
        self.assertTrue(all(col + len(text) <= 48 for row, col, text in screen.positions
                            if row <= 2 and col < 50))

    def test_narrow_terminal_stacks_status_without_overlap(self) -> None:
        ui, screen = self.make_ui()
        screen.width = 50
        ui.update(probe=True, bv=True, i2c="DETECTED")
        labels = [(row, col) for row, col, text in screen.positions
                  if text.startswith(("Operator:", "BV tip:", "ETROC I²C"))]
        self.assertEqual(labels, [(1, 0), (3, 0), (5, 0)])

    def test_child_output_stays_in_message_pane(self) -> None:
        ui, screen = self.make_ui()
        result = ui.run_script([sys.executable, "-c", "print('calibration message')"], Path.cwd())
        self.assertEqual(result, 0)
        self.assertIn("calibration message", ui.lines)
        self.assertTrue(any(row >= 5 and text == "calibration message" for row, text, _ in screen.drawn))

    def test_python_scan_message_arrives_before_scan_exits(self) -> None:
        ui, _ = self.make_ui()
        first_message = threading.Event()
        original_write = ui.write

        def observe(text: str) -> int:
            result = original_write(text)
            if "early IV message" in text:
                first_message.set()
            return result

        ui.write = observe
        result = []
        child = "import time; print('early IV message'); time.sleep(2)"
        worker = threading.Thread(target=lambda: result.append(
            ui.run_script([sys.executable, "-c", child], Path.cwd())))
        worker.start()
        try:
            self.assertTrue(first_message.wait(1), "IV message was buffered until process exit")
        finally:
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result, [0])

    def test_detached_viewer_does_not_hold_menu_open(self) -> None:
        ui, _ = self.make_ui()
        child = ("import subprocess, sys; "
                 "subprocess.Popen([sys.executable, '-c', "
                 "'import time; time.sleep(2)'], start_new_session=True); "
                 "print('scan done')")
        started = time.monotonic()
        result = ui.run_script([sys.executable, "-c", child], Path.cwd())
        self.assertEqual(result, 0)
        self.assertIn("scan done", ui.lines)
        self.assertLess(time.monotonic() - started, 1.5)

    def test_unicode_name_can_be_entered(self) -> None:
        ui, screen = self.make_ui()
        screen.keys = iter(("김", "A", "\n"))
        with patch("measurement_tui.curses.curs_set"):
            self.assertEqual(ui.ask("Name: "), "김A")
        self.assertEqual(screen.input_timeout, -1)

    def test_sample_history_uses_up_down_and_restores_draft(self) -> None:
        ui, screen = self.make_ui()
        ui.remember_history("Hybrid", "W15_001")
        ui.remember_history("Hybrid", "W15_002")
        ui.remember_history("Bare ETROC", "E1")
        screen.keys = iter((curses.KEY_UP, curses.KEY_UP, curses.KEY_DOWN, "\n"))
        with patch("measurement_tui.curses.curs_set"):
            self.assertEqual(ui.ask_with_history("New Hybrid: ", "Hybrid"), "W15_002")
        screen.keys = iter(("W", "1", "5", curses.KEY_UP, curses.KEY_DOWN, "\n"))
        with patch("measurement_tui.curses.curs_set"):
            self.assertEqual(ui.ask_with_history("New Hybrid: ", "Hybrid"), "W15")
        screen.keys = iter((curses.KEY_UP, "\n"))
        with patch("measurement_tui.curses.curs_set"):
            self.assertEqual(ui.ask_with_history("New Bare ETROC: ", "Bare ETROC"), "E1")

    def test_sample_history_prefix_finds_latest_match(self) -> None:
        ui, screen = self.make_ui()
        for name in ("W15_001", "Other", "W15_002"):
            ui.remember_history("Hybrid", name)
        screen.keys = iter(("w", "1", "5", curses.KEY_UP, "\n"))
        with patch("measurement_tui.curses.curs_set"):
            self.assertEqual(ui.ask_with_history("New Hybrid: ", "Hybrid"), "W15_002")

    def test_missing_terminal_input_is_reported_as_eof(self) -> None:
        ui, screen = self.make_ui()
        screen.get_wch = lambda: (_ for _ in ()).throw(curses.error("no input"))
        with patch("measurement_tui.curses.curs_set"):
            with self.assertRaisesRegex(EOFError, "Terminal input unavailable"):
                ui.ask("Press Enter: ")

    def test_bv_menu_remains_visible_on_narrow_terminal(self) -> None:
        ui, screen = self.make_ui()
        screen.width = 50
        ui.set_menu("BV: [a] Auto-calibration  [i] IV scan  "
                    "[b] Un-contact BV tip  [u] Un-contact BV and probe")
        footer = " ".join(text for row, text, _ in screen.drawn if row >= 20)
        self.assertIn("[a] Auto-calibration", footer)
        self.assertIn("[u] Un-contact BV and probe", footer)


if __name__ == "__main__":
    unittest.main()
