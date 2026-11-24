"""Fixed-status terminal display for the interactive ETROC workflow."""

from __future__ import annotations

import curses
import os
import selectors
import signal
import subprocess
import textwrap
from collections import deque
from pathlib import Path


class MeasurementTUI:
    def __init__(self, screen) -> None:
        self.screen = screen
        self.screen.keypad(True)
        self.lines: deque[str] = deque(maxlen=1000)
        self.pending = ""
        self.menu = ""
        self.prompt = ""
        self.answer = ""
        self.status = {
            "operator": "—", "sample_type": "—", "sample_name": "—",
            "probe": False, "bv": False, "probe_trial": 0, "bv_trial": 0,
            "psu": "UNKNOWN", "i2c": "NOT CHECKED", "phase": "Starting",
        }
        self.red = curses.A_BOLD
        self.green = curses.A_NORMAL
        if curses.has_colors():
            curses.start_color()
            try:
                curses.use_default_colors()
                background = -1
            except curses.error:
                background = curses.COLOR_BLACK
            curses.init_pair(1, curses.COLOR_RED, background)
            curses.init_pair(2, curses.COLOR_GREEN, background)
            self.red |= curses.color_pair(1)
            self.green = curses.A_BOLD | curses.color_pair(2)
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        self.draw()

    def update(self, **values) -> None:
        self.status.update(values)
        self.draw()

    def set_menu(self, menu: str) -> None:
        self.menu = menu
        self.draw()

    def write(self, text: str) -> int:
        for char in text:
            if char == "\r":
                self.pending = ""
            elif char == "\n":
                if self.pending:
                    self.lines.append(self.pending)
                self.pending = ""
            elif char.isprintable() or char == "\t":
                self.pending += char
        self.draw()
        return len(text)

    def flush(self) -> None:
        pass

    def _text(self, row: int, col: int, value: str, attr: int = 0,
              end_col: int | None = None) -> int:
        height, width = self.screen.getmaxyx()
        limit = min(width - 1, end_col if end_col is not None else width - 1)
        if row >= height or col >= limit:
            return col
        value = str(value).expandtabs(4)
        length = min(len(value), limit - col)
        try:
            self.screen.addnstr(row, col, value, length, attr)
        except curses.error:
            pass
        return col + length

    def _contact(self, row: int, col: int, label: str, contacted: bool,
                 trial: int, end_col: int | None = None) -> None:
        col = self._text(row, col, label, end_col=end_col)
        col = self._text(row, col, "CONTACTED" if contacted else "NOT CONTACTED",
                         self.red if contacted else self.green, end_col=end_col)
        self._text(row, col, f" (trial {trial})", end_col=end_col)

    def _psu(self, row: int, col: int, value: str, end_col: int | None = None) -> None:
        col = self._text(row, col, "PSU CH1/CH2: ", end_col=end_col)
        attr = self.red if value == "ON" else self.green if value == "OFF" else 0
        self._text(row, col, value, attr, end_col=end_col)

    def _i2c(self, row: int, col: int, value: str) -> None:
        col = self._text(row, col, "ETROC I²C 0x60: ")
        attr = (self.red if value == "DETECTED" else
                self.green if value in ("NOT CHECKED", "NOT DETECTED") else 0)
        self._text(row, col, value, attr)

    def draw(self) -> None:
        height, width = self.screen.getmaxyx()
        self.screen.erase()
        if height < 12 or width < 45:
            self._text(0, 0, "Resize terminal to at least 45x12")
            self.screen.refresh()
            return

        state = self.status
        if width >= 70:
            right_col = width // 2
            left_end = right_col - 2
            self._text(0, 0, f"Sample: {state['sample_name']} ({state['sample_type']})", end_col=left_end)
            self._text(0, right_col, f"Operator: {state['operator']}")
            self._contact(1, 0, "Probe: ", state['probe'], state['probe_trial'], left_end)
            self._contact(1, right_col, "BV tip: ", state['bv'], state['bv_trial'])
            self._psu(2, 0, state['psu'], left_end)
            self._i2c(2, right_col, state['i2c'])
            self._text(3, 0, f"Stage: {state['phase']}")
            separator_row = 4
        else:
            # Stack the same fields when two readable columns cannot fit.
            self._text(0, 0, f"Sample: {state['sample_name']} ({state['sample_type']})")
            self._text(1, 0, f"Operator: {state['operator']}")
            self._contact(2, 0, "Probe: ", state['probe'], state['probe_trial'])
            self._contact(3, 0, "BV tip: ", state['bv'], state['bv_trial'])
            self._psu(4, 0, state['psu'])
            self._i2c(5, 0, state['i2c'])
            self._text(6, 0, f"Stage: {state['phase']}")
            separator_row = 7
        self._text(separator_row, 0, "─" * (width - 1))

        menu_lines = textwrap.wrap(self.menu, width=width - 1, break_long_words=False) or [""]
        menu_lines = menu_lines[:3]
        menu_top = height - 1 - len(menu_lines)
        log_start = separator_row + 1
        log_height = max(0, menu_top - log_start - 1)
        recent = list(self.lines)
        if self.pending:
            recent.append(self.pending)
        wrapped: list[str] = []
        for line in reversed(recent):
            parts = textwrap.wrap(line, width=width - 1, replace_whitespace=False) or [""]
            wrapped.extend(reversed(parts))
            if len(wrapped) >= log_height:
                break
        for row, line in enumerate(reversed(wrapped[:log_height]), start=log_start):
            self._text(row, 0, line)

        self._text(menu_top - 1, 0, "─" * (width - 1))
        for row, line in enumerate(menu_lines, start=menu_top):
            self._text(row, 0, line)
        self._text(height - 1, 0, self.prompt + self.answer)
        self.screen.refresh()

    def ask(self, prompt: str) -> str:
        self.prompt = prompt
        self.answer = ""
        self.draw()
        # Menus should wait for a real keypress even if input mode changed.
        self.screen.timeout(-1)
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        while True:
            try:
                key = self.screen.get_wch()
            except curses.error as exc:
                # EOF/disconnected terminal can still fail even in blocking
                # mode. Let the workflow perform its normal PSU shutdown.
                raise EOFError("Terminal input unavailable") from exc
            if key == curses.KEY_RESIZE:
                self.draw()
            elif key in ("\n", "\r", curses.KEY_ENTER):
                result = self.answer
                self.prompt = ""
                self.answer = ""
                self.draw()
                try:
                    curses.curs_set(0)
                except curses.error:
                    pass
                return result
            elif key in ("\b", "\x7f", curses.KEY_BACKSPACE):
                self.answer = self.answer[:-1]
                self.draw()
            elif isinstance(key, str) and key.isprintable():
                self.answer += key
                self.draw()

    def run_script(self, command: list[str], cwd: Path) -> int:
        """Stream child output into the log; Ctrl-C reaches its own process group."""
        self.set_menu("Measurement running — wait for completion (Ctrl-C to interrupt)")
        process = subprocess.Popen(
            command, cwd=cwd, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
            start_new_session=True, bufsize=0,
        )
        assert process.stdout is not None
        interrupted = False
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        os.set_blocking(process.stdout.fileno(), False)
        try:
            while selector.get_map():
                try:
                    events = selector.select(timeout=0.2)
                except KeyboardInterrupt:
                    if not interrupted:
                        interrupted = True
                        self.write("\nInterrupting measurement; waiting for child shutdown...\n")
                        if process.poll() is None:
                            try:
                                os.killpg(process.pid, signal.SIGINT)
                            except ProcessLookupError:
                                pass
                    continue
                for key, _ in events:
                    try:
                        chunk = os.read(key.fileobj.fileno(), 4096)
                    except BlockingIOError:
                        continue
                    if chunk:
                        self.write(chunk.decode(errors="replace"))
                    else:
                        selector.unregister(key.fileobj)
                if process.poll() is not None:
                    # A detached X-window viewer can inherit this pipe. Drain
                    # bytes already available, but do not wait for the viewer
                    # to close before restoring the workflow menu.
                    while selector.get_map():
                        try:
                            chunk = os.read(process.stdout.fileno(), 4096)
                        except BlockingIOError:
                            break
                        if not chunk:
                            selector.unregister(process.stdout)
                            break
                        self.write(chunk.decode(errors="replace"))
                    break
            returncode = process.wait()
        finally:
            selector.close()
            if process.poll() is None:
                # Do not return control to the PSU workflow while a scan child
                # may still be using its instruments.
                try:
                    os.killpg(process.pid, signal.SIGINT)
                except ProcessLookupError:
                    pass
                while process.poll() is None:
                    try:
                        process.communicate()
                    except KeyboardInterrupt:
                        continue
            process.stdout.close()
        if interrupted:
            raise KeyboardInterrupt
        return returncode
