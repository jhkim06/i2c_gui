#!/usr/bin/env python3
"""Guide ETROC contact, calibration, and IV measurements from one terminal.

Source setup_i2c_measurement_env.sh before running. This program owns the
low-voltage E36312A outputs while it is running; the IV subprocess owns the
Keithley bias output and performs its existing shutdown sequence.
"""

from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from keysight_e36312a_interactive import DEFAULT_PRESETS, USBTMCInstrument, discover_device
from gelpak_map import GelPakMap, fetch_gelpak
from sample_naming import canonical_sample_name


HELPERS_DIR = Path(__file__).resolve().parent
DEFAULT_IV_CONFIG = HELPERS_DIR / "iv_configs" / "experiment_smu_voltage_scan_keithley_2400.json"


@dataclass(frozen=True)
class SessionMetadata:
    operator: str
    sample_type: str
    sample_name: str
    probe_trial: int
    bv_trial: int = 0

    def plot_footer(self, *, bv_contacted: bool) -> str:
        stage = "BV tip contacted; BV=0 V" if bv_contacted else "Probe only; BV tip not contacted"
        return (f"Operator: {self.operator} | {self.sample_type} | "
                f"Probe trial: {self.probe_trial} | BV trial: {self.bv_trial} | {stage}")

    def iv_plot_footer(self) -> str:
        return (f"Operator: {self.operator} | {self.sample_type} | "
                f"Probe trial: {self.probe_trial} | BV trial: {self.bv_trial} | "
                "BV tip contacted; bias sweep")


class PowerSupplyController:
    """Control only the configured low-voltage E36312A channels."""

    def __init__(self, device: str | None = None, channels: tuple[int, ...] = (1, 2),
                 presets: dict[int, tuple[float, float]] | None = None) -> None:
        self.device = device or discover_device()
        if self.device is None:
            raise RuntimeError("No E36312A USBTMC device found")
        self.channels = channels
        self.presets = dict(DEFAULT_PRESETS if presets is None else presets)
        self.instrument = USBTMCInstrument(self.device)
        identity = self.instrument.query("*IDN?")
        if "E36312A" not in identity.upper():
            raise RuntimeError(f"Expected E36312A at {self.device}, got {identity!r}")
        for channel in channels:
            if self.presets.get(channel) is None:
                raise ValueError(f"No configured preset for PSU channel {channel}")
            voltage, limit = self.presets[channel]
            if voltage <= 0 or limit <= 0:
                raise ValueError(f"PSU CH{channel} voltage and current limit must be positive")
        print(f"Power supply: {identity.strip()} ({self.device})")
        for channel in channels:
            voltage, limit = self.presets[channel]
            print(f"  CH{channel}: {voltage:g} V, {limit:g} A current limit")

    def off(self) -> None:
        self.instrument.output_channels([1, 2, 3], False)
        for channel in (1, 2, 3):
            status = self.instrument.get_channel_status(channel)
            if status.output_on is not False:
                raise RuntimeError(f"PSU CH{channel} output could not be confirmed OFF")
        print("PSU CH1–CH3 outputs confirmed OFF")

    def on(self) -> None:
        for channel in self.channels:
            voltage, limit = self.presets[channel]
            self.instrument.configure_channel(channel, voltage, limit)
        self.instrument.output_channels(list(self.channels), True)
        time.sleep(0.5)
        for channel in self.channels:
            status = self.instrument.get_channel_status(channel)
            if status.output_on is not True:
                self.off()
                raise RuntimeError(f"PSU CH{channel} output could not be confirmed ON")
            print(f"CH{channel} ON: measured {status.meas_voltage} V, {status.meas_current} A")


class EtrocController:
    """Run the existing ETROC and IV scripts; do not duplicate their scans."""

    def __init__(self, psu_device: str, iv_config: Path, i2c_bus: int = 1, i2c_address: int = 0x60,
                 script_runner: Callable[[list[str], Path], int] | None = None) -> None:
        self.psu_device = psu_device
        self.iv_config = iv_config
        self.i2c_bus = i2c_bus
        self.i2c_address = i2c_address
        self.script_runner = script_runner

    def run_script(self, command: list[str]) -> bool:
        if self.script_runner is not None:
            return self.script_runner(command, HELPERS_DIR) == 0
        return subprocess.run(command, cwd=HELPERS_DIR, check=False).returncode == 0

    def detected(self) -> bool:
        result = subprocess.run(
            ["i2cdetect", "-y", str(self.i2c_bus)],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"i2cdetect failed: {result.stderr.strip() or result.stdout.strip()}")
        row_label = f"{self.i2c_address & 0xF0:02x}:"
        offset = self.i2c_address & 0x0F
        for line in result.stdout.splitlines():
            columns = line.split()
            if columns and columns[0].lower() == row_label:
                cell = columns[offset + 1].lower() if len(columns) > offset + 1 else "--"
                found = cell == f"{self.i2c_address:02x}"
                print(f"I²C bus {self.i2c_bus}, address 0x{self.i2c_address:02x}: "
                      f"{'detected' if found else f'not detected (i2cdetect: {cell})'}")
                return found
        raise RuntimeError(f"Could not parse i2cdetect output for address 0x{self.i2c_address:02x}")

    def calibrate(self, metadata: SessionMetadata, *, bv_contacted: bool) -> bool:
        command = [
            sys.executable, str(HELPERS_DIR / "i2c_test_with_rpi.py"),
            "--chip_name", metadata.sample_name,
            "--plot-footer-metadata", metadata.plot_footer(bv_contacted=bv_contacted),
            #"--measure-1v2", "--psu-device", self.psu_device,
            "--stop-after-10-scan-failures", 
            #"--no-show-plots",
        ]
        print("Starting ETROC auto-calibration")
        return self.run_script(command)

    def iv_scan(self, metadata: SessionMetadata) -> bool:
        command = [
            sys.executable, str(HELPERS_DIR / "smu_etroc_calibration_loop.py"),
            "--config", str(self.iv_config),
            "--chip-name", metadata.sample_name,
            "--plot-footer-metadata", metadata.iv_plot_footer(),
        ]
        print(f"Starting IV scan using {self.iv_config}")
        return self.run_script(command)


class MeasurementWorkflow:
    def __init__(self, psu: PowerSupplyController, etroc: EtrocController,
                 ask: Callable[[str], str] = input, ui=None,
                 gelpak_loader: Callable[[str], GelPakMap] = fetch_gelpak) -> None:
        self.psu = psu
        self.etroc = etroc
        self.ask = ask
        self.ui = ui
        self.gelpak_loader = gelpak_loader

    def status(self, **values) -> None:
        if self.ui is not None:
            self.ui.update(**values)

    def menu(self, text: str) -> None:
        if self.ui is not None:
            self.ui.set_menu(text)
        else:
            print(text)

    def choice(self, prompt: str, allowed: tuple[str, ...]) -> str:
        while True:
            answer = self.ask(prompt).strip().lower()
            if answer in allowed:
                return answer
            print(f"Choose one of: {', '.join(allowed)}")

    def required(self, prompt: str, *, history_key: str | None = None) -> str:
        while True:
            if history_key is not None and self.ui is not None and hasattr(self.ui, "ask_with_history"):
                value = self.ui.ask_with_history(prompt, history_key).strip()
            else:
                value = self.ask(prompt).strip()
            if value:
                return value
            print("A value is required.")

    def contact(self, label: str, *, bv_tip: bool = False) -> bool:
        self.psu.off()
        self.status(psu="OFF", i2c="NOT CHECKED", **({"bv": False} if bv_tip else {"probe": False, "bv": False}))
        print(f"{label}: adjust contact with the PSU OFF.")
        self.menu("[d] Contact done   [q] Return")
        return self.choice("Enter d when contact is done, or q to go back: ", ("d", "q")) == "d"

    def energize_and_check(self) -> bool:
        self.psu.on()
        self.status(psu="ON", i2c="CHECKING")
        if self.etroc.detected():
            self.status(i2c="DETECTED")
            return True
        self.psu.off()
        self.status(psu="OFF", i2c="NOT DETECTED")
        print("ETROC not detected; outputs are OFF. Adjust contact and retry.")
        return False

    def uncontact(self, label: str, *, probe: bool = False) -> None:
        self.psu.off()
        self.status(psu="OFF", i2c="NOT CHECKED")
        self.menu("[d] Confirm contact removed")
        self.choice(f"Remove {label}, then enter d to confirm: ", ("d",))
        self.status(**({"probe": False, "bv": False} if probe else {"bv": False}))

    def sample(self, operator: str, sample_type: str, sample_name: str) -> None:
        probe_trial = 0
        bv_trial = 0
        stage = "probe_contact"
        self.status(sample_name=sample_name, probe=False, bv=False, probe_trial=0,
                    bv_trial=0, i2c="NOT CHECKED")
        while True:
            if stage == "probe_contact":
                probe_trial += 1
                bv_trial = 0
                self.status(phase="Probe-card contact", probe_trial=probe_trial, bv_trial=0)
                if not self.contact(f"Probe-card contact trial {probe_trial} for {sample_name}"):
                    return  # Quit current sample; ask for a new name.
                self.status(probe=True)
                if self.energize_and_check():
                    stage = "probe_menu"
            elif stage == "probe_menu":
                self.status(phase="Probe menu")
                self.menu("Probe: [a] Auto-calibration  [b] Contact BV tip  [u] Un-contact probe")
                action = self.choice("> ", ("a", "b", "u"))
                metadata = SessionMetadata(operator, sample_type, sample_name, probe_trial)
                if action == "a":
                    self.status(phase="Auto-calibration running")
                    success = self.etroc.calibrate(metadata, bv_contacted=False)
                    self.status(phase="Probe menu")
                    if not success:
                        print("Auto-calibration failed; review output before trying again.")
                elif action == "b":
                    stage = "bv_contact"
                else:
                    self.uncontact("the probe card", probe=True)
                    stage = "probe_contact"
            elif stage == "bv_contact":
                bv_trial += 1
                self.status(phase="BV-tip contact", bv_trial=bv_trial)
                if not self.contact(f"BV-tip contact trial {bv_trial}", bv_tip=True):
                    if self.energize_and_check():
                        stage = "probe_menu"
                    else:
                        stage = "probe_contact"
                else:
                    self.status(bv=True)
                    if self.energize_and_check():
                        stage = "bv_menu"
            elif stage == "bv_menu":
                self.status(phase="BV menu")
                self.menu("BV: [a] Auto-calibration  [i] IV scan  "
                          "[b] Un-contact BV tip  [u] Un-contact BV and probe")
                action = self.choice("> ", ("a", "i", "b", "u"))
                metadata = SessionMetadata(operator, sample_type, sample_name, probe_trial, bv_trial)
                if action == "a":
                    self.status(phase="Auto-calibration running")
                    success = self.etroc.calibrate(metadata, bv_contacted=True)
                    self.status(phase="BV menu")
                    if not success:
                        print("Auto-calibration failed; review output before trying again.")
                elif action == "i":
                    self.status(phase="IV scan running")
                    success = self.etroc.iv_scan(metadata)
                    self.status(phase="BV menu")
                    if not success:
                        self.psu.off()
                        self.status(psu="OFF")
                        raise RuntimeError("IV scan failed; verify the Keithley output is OFF before continuing")
                elif action == "b":
                    self.uncontact("the BV tip")
                    if self.energize_and_check():
                        stage = "probe_menu"
                    else:
                        stage = "probe_contact"
                else:
                    self.uncontact("the BV tip and probe card", probe=True)
                    stage = "probe_contact"

    def run(self) -> None:
        """Navigate selection prompts with q; measurement contact menus stay separate."""
        stage = "operator"
        operator = ""
        sample_type = ""
        gelpak_mode = False
        gelpak = None
        while True:
            if stage == "operator":
                if self.ui is not None and hasattr(self.ui, "set_gelpak"):
                    self.ui.set_gelpak(None)
                self.status(operator="—", sample_type="—", sample_name="—", phase="Select operator")
                operator = self.required("Operator name (q to exit): ")
                if operator.lower() == "q":
                    return
                stage = "sample_type"
            elif stage == "sample_type":
                self.status(operator=operator, sample_type="—", phase="Select sample type")
                self.menu("[b] Bare ETROC   [h] Hybrid   [q] Operator name")
                answer = self.choice("[b] Bare ETROC, [h] Hybrid, or [q] back? ",
                                     ("b", "h", "q"))
                if answer == "q":
                    stage = "operator"
                    continue
                sample_type = "Bare ETROC" if answer == "b" else "Hybrid"
                stage = "gelpak_choice"
            elif stage == "gelpak_choice":
                self.status(sample_type=sample_type, sample_name="—", phase="Select Gel-Pak mode")
                self.menu("Measure samples from a Gel-Pak? [y] Yes  [n] No  [q] Back")
                answer = self.choice("Samples in a Gel-Pak? [y/n/q]: ", ("y", "n", "q"))
                if answer == "q":
                    stage = "sample_type"
                    continue
                gelpak_mode = answer == "y"
                gelpak = None
                if not gelpak_mode:
                    if self.ui is not None and hasattr(self.ui, "set_gelpak"):
                        self.ui.set_gelpak(None)
                    if self.ui is not None and hasattr(self.ui, "set_history"):
                        self.ui.set_history(sample_type, [])
                stage = "gelpak_id" if gelpak_mode else "sample_name"
            elif stage == "gelpak_id":
                if self.ui is not None and hasattr(self.ui, "set_gelpak"):
                    self.ui.set_gelpak(None)
                self.status(sample_name="—", phase="Select Gel-Pak")
                self.menu("Enter a Gel-Pak ID, [n] no map, [q] Gel-Pak yes/no")
                identifier = self.required("Gel-Pak ID (n: no map, q: back): ")
                if identifier.lower() == "q":
                    stage = "gelpak_choice"
                    continue
                if identifier.lower() == "n":
                    gelpak = None
                else:
                    try:
                        gelpak = self.gelpak_loader(identifier)
                    except (ValueError, RuntimeError) as exc:
                        print(f"Could not load Gel-Pak map: {exc}")
                        continue
                    if self.ui is not None and hasattr(self.ui, "set_gelpak"):
                        self.ui.set_gelpak(gelpak)
                    else:
                        print(f"Gel-Pak {gelpak.name or gelpak.identifier}:")
                        for line, _ in gelpak.slot_lines():
                            print(f"  {line}")
                if self.ui is not None and hasattr(self.ui, "set_history"):
                    names = []
                    for slot in (gelpak.slots if gelpak is not None else ()):
                        if slot:
                            try:
                                names.append(canonical_sample_name(slot))
                            except ValueError:
                                print(f"Skipping invalid Gel-Pak sample name: {slot}")
                    self.ui.set_history(sample_type, names)
                stage = "sample_name"
            else:
                self.status(operator=operator, sample_type=sample_type, sample_name="—",
                            probe=False, bv=False, i2c="NOT CHECKED", phase="Select sample")
                back_label = "Gel-Pak ID" if gelpak_mode else "Gel-Pak yes/no"
                history_hint = " (↑/↓ Gel-Pak names and history)" if gelpak and self.ui is not None else ""
                self.menu(f"Enter a new sample name{history_hint}, or q for {back_label}")
                entered_name = self.required(f"New {sample_type} name (q: back): ",
                                             history_key=sample_type)
                if entered_name.lower() == "q":
                    self.psu.off()
                    self.status(psu="OFF", sample_name="—", probe=False, bv=False,
                                i2c="NOT CHECKED")
                    stage = "gelpak_id" if gelpak_mode else "gelpak_choice"
                    continue
                try:
                    sample_name = canonical_sample_name(entered_name)
                except ValueError as exc:
                    print(f"Invalid sample name: {exc}")
                    continue
                if entered_name != sample_name:
                    print(f"Using sample name {sample_name} for plots, directories, and SQLite")
                if self.ui is not None and hasattr(self.ui, "remember_history"):
                    self.ui.remember_history(sample_type, sample_name)
                self.sample(operator, sample_type, sample_name)
                self.status(sample_name="—", probe=False, bv=False, i2c="NOT CHECKED",
                            phase="Select sample")


def run_measurement(args, ui=None) -> int:
    """Run the existing hardware workflow with an optional terminal display."""
    psu = None
    exit_code = 0
    try:
        psu = PowerSupplyController(args.psu_device, presets={
            1: (args.ch1_voltage, args.ch1_current_limit),
            2: (args.ch2_voltage, args.ch2_current_limit),
        })
        etroc = EtrocController(psu.device, args.iv_config, args.i2c_bus, args.i2c_address,
                                script_runner=ui.run_script if ui is not None else None)
        MeasurementWorkflow(psu, etroc, ask=ui.ask if ui is not None else input, ui=ui).run()
    except (KeyboardInterrupt, EOFError):
        message = "Interrupted or terminal input unavailable; switching controlled PSU outputs OFF."
        print(f"\n{message}")
        if ui is not None:
            ui.error_summary = message
        exit_code = 130
    except Exception as exc:
        message = f"ERROR: {exc}"
        print(message, file=sys.stderr)
        if ui is not None:
            ui.error_summary = message
        exit_code = 1
    finally:
        if psu is not None:
            try:
                psu.off()
                if ui is not None:
                    ui.update(psu="OFF", i2c="NOT CHECKED", phase="Finished")
            except Exception as exc:
                message = f"ERROR: could not confirm PSU shutdown: {exc}"
                print(message, file=sys.stderr)
                if ui is not None:
                    ui.update(psu="UNKNOWN", phase="Shutdown warning")
                    ui.error_summary = message
                exit_code = 2
    return exit_code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--psu-device", help="E36312A USBTMC path (auto-detected by default)")
    parser.add_argument("--ch1-voltage", type=float, default=DEFAULT_PRESETS[1][0])
    parser.add_argument("--ch1-current-limit", type=float, default=DEFAULT_PRESETS[1][1])
    parser.add_argument("--ch2-voltage", type=float, default=DEFAULT_PRESETS[2][0])
    parser.add_argument("--ch2-current-limit", type=float, default=DEFAULT_PRESETS[2][1])
    parser.add_argument("--iv-config", type=Path, default=DEFAULT_IV_CONFIG)
    parser.add_argument("--i2c-bus", type=int, default=1)
    parser.add_argument("--i2c-address", type=lambda value: int(value, 0), default=0x60)
    parser.add_argument("--plain", action="store_true", help="Use the original scrolling text prompts instead of the terminal UI")
    args = parser.parse_args()
    if not args.iv_config.is_file():
        parser.error(f"IV config not found: {args.iv_config}")
    if not 0x08 <= args.i2c_address <= 0x77:
        parser.error("I²C address must be a normal 7-bit address (0x08–0x77)")
    for name in ("ETROC_LOCAL_FIGURE_ROOT", "ETROC_SQLITE_OUTPUT_DIR", "ETROC_FIGURE_ROOT"):
        if not os.environ.get(name):
            parser.error("Source helpers/setup_i2c_measurement_env.sh first")

    if args.plain or not (sys.stdin.isatty() and sys.stdout.isatty()):
        return run_measurement(args)

    import curses
    from measurement_tui import MeasurementTUI

    failure_summary = ""

    def run_tui(screen) -> int:
        nonlocal failure_summary
        ui = MeasurementTUI(screen)
        with redirect_stdout(ui), redirect_stderr(ui):
            exit_code = run_measurement(args, ui)
            if exit_code:
                failure_summary = getattr(ui, "error_summary", "Measurement stopped with an error")
                ui.set_menu("Measurement stopped; review the messages above")
                try:
                    ui.ask("Press Enter to return to the shell: ")
                except (EOFError, KeyboardInterrupt):
                    pass
            return exit_code

    exit_code = curses.wrapper(run_tui)
    if exit_code:
        print(failure_summary, file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
