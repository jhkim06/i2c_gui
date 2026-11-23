#!/usr/bin/env python3
"""Guide ETROC contact, calibration, and IV measurements from one terminal.

Source setup_i2c_measurement_env.sh before running. This program owns the
low-voltage E36312A outputs while it is running; the IV subprocess owns the
Keithley bias output and performs its existing shutdown sequence.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from keysight_e36312a_interactive import DEFAULT_PRESETS, USBTMCInstrument, discover_device


HELPERS_DIR = Path(__file__).resolve().parent
DEFAULT_IV_CONFIG = HELPERS_DIR / "iv_configs" / "experiment_smu_voltage_scan_keithley_2400.json"


@dataclass(frozen=True)
class SessionMetadata:
    operator: str
    sample_type: str
    sample_name: str
    probe_trial: int
    bv_trial: int = 0

    def notes(self, *, bv_contacted: bool, iv_scan: bool = False) -> str:
        if iv_scan:
            stage = "BV tip contacted; IV bias sweep"
        else:
            stage = "BV tip contacted; BV=0 V" if bv_contacted else "Probe only; BV tip not contacted"
        return (f"Operator: {self.operator} | {self.sample_type} | "
                f"Probe trial: {self.probe_trial} | BV trial: {self.bv_trial} | {stage}")


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

    def __init__(self, psu_device: str, iv_config: Path, i2c_bus: int = 1, i2c_address: int = 0x60) -> None:
        self.psu_device = psu_device
        self.iv_config = iv_config
        self.i2c_bus = i2c_bus
        self.i2c_address = i2c_address

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
            "--save_notes", metadata.notes(bv_contacted=bv_contacted),
            #"--measure-1v2", "--psu-device", self.psu_device,
            "--stop-after-10-scan-failures", 
            #"--no-show-plots",
        ]
        print("Starting ETROC auto-calibration")
        return subprocess.run(command, cwd=HELPERS_DIR, check=False).returncode == 0

    def iv_scan(self, metadata: SessionMetadata) -> bool:
        command = [
            sys.executable, str(HELPERS_DIR / "smu_etroc_calibration_loop.py"),
            "--config", str(self.iv_config),
            "--chip-name", metadata.sample_name,
            "--save-notes", metadata.notes(bv_contacted=True, iv_scan=True),
        ]
        print(f"Starting IV scan using {self.iv_config}")
        return subprocess.run(command, cwd=HELPERS_DIR, check=False).returncode == 0


class MeasurementWorkflow:
    def __init__(self, psu: PowerSupplyController, etroc: EtrocController,
                 ask: Callable[[str], str] = input) -> None:
        self.psu = psu
        self.etroc = etroc
        self.ask = ask

    def choice(self, prompt: str, allowed: tuple[str, ...]) -> str:
        while True:
            answer = self.ask(prompt).strip().lower()
            if answer in allowed:
                return answer
            print(f"Choose one of: {', '.join(allowed)}")

    def required(self, prompt: str) -> str:
        while True:
            value = self.ask(prompt).strip()
            if value:
                return value
            print("A value is required.")

    def contact(self, label: str) -> bool:
        self.psu.off()
        print(f"{label}: adjust contact with the PSU OFF.")
        return self.choice("Enter d when contact is done, or q to go back: ", ("d", "q")) == "d"

    def energize_and_check(self) -> bool:
        self.psu.on()
        if self.etroc.detected():
            return True
        self.psu.off()
        print("ETROC not detected; outputs are OFF. Adjust contact and retry.")
        return False

    def uncontact(self, label: str) -> None:
        self.psu.off()
        self.choice(f"Remove {label}, then enter d to confirm: ", ("d",))

    def sample(self, operator: str, sample_type: str, sample_name: str) -> None:
        probe_trial = 0
        bv_trial = 0
        stage = "probe_contact"
        while True:
            if stage == "probe_contact":
                probe_trial += 1
                bv_trial = 0
                if not self.contact(f"Probe-card contact trial {probe_trial} for {sample_name}"):
                    return  # Quit current sample; ask for a new name.
                if self.energize_and_check():
                    stage = "probe_menu"
            elif stage == "probe_menu":
                print("Probe menu: [a] Auto-calibration  [b] Contact BV tip  [u] Un-contact probe")
                action = self.choice("> ", ("a", "b", "u"))
                metadata = SessionMetadata(operator, sample_type, sample_name, probe_trial)
                if action == "a":
                    if not self.etroc.calibrate(metadata, bv_contacted=False):
                        print("Auto-calibration failed; review output before trying again.")
                elif action == "b":
                    stage = "bv_contact"
                else:
                    self.uncontact("the probe card")
                    stage = "probe_contact"
            elif stage == "bv_contact":
                bv_trial += 1
                if not self.contact(f"BV-tip contact trial {bv_trial}"):
                    if self.energize_and_check():
                        stage = "probe_menu"
                    else:
                        stage = "probe_contact"
                elif self.energize_and_check():
                    stage = "bv_menu"
            elif stage == "bv_menu":
                print("BV menu: [a] Auto-calibration  [i] IV scan  "
                      "[b] Un-contact BV tip  [u] Un-contact BV and probe")
                action = self.choice("> ", ("a", "i", "b", "u"))
                metadata = SessionMetadata(operator, sample_type, sample_name, probe_trial, bv_trial)
                if action == "a":
                    if self.choice("Confirm Keithley bias output is OFF and BV is 0 V [y/n]: ",
                                   ("y", "n")) != "y":
                        print("Calibration skipped; BV=0 V was not confirmed.")
                        continue
                    if not self.etroc.calibrate(metadata, bv_contacted=True):
                        print("Auto-calibration failed; review output before trying again.")
                elif action == "i":
                    if not self.etroc.iv_scan(metadata):
                        self.psu.off()
                        raise RuntimeError("IV scan failed; verify the Keithley output is OFF before continuing")
                elif action == "b":
                    self.uncontact("the BV tip")
                    if self.energize_and_check():
                        stage = "probe_menu"
                    else:
                        stage = "probe_contact"
                else:
                    self.uncontact("the BV tip and probe card")
                    stage = "probe_contact"

    def run(self) -> None:
        operator = self.required("Operator name: ")
        sample_type = "Bare ETROC" if self.choice("[b] Bare ETROC or [h] Hybrid? ", ("b", "h")) == "b" else "Hybrid"
        print("Enter a new sample name for each measurement, or q to exit.")
        while True:
            sample_name = self.required(f"New {sample_type} name (q to exit): ")
            if sample_name.lower() == "q":
                return
            self.sample(operator, sample_type, sample_name)


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
    args = parser.parse_args()
    if not args.iv_config.is_file():
        parser.error(f"IV config not found: {args.iv_config}")
    if not 0x08 <= args.i2c_address <= 0x77:
        parser.error("I²C address must be a normal 7-bit address (0x08–0x77)")
    for name in ("ETROC_LOCAL_FIGURE_ROOT", "ETROC_SQLITE_OUTPUT_DIR", "ETROC_FIGURE_ROOT"):
        if not os.environ.get(name):
            parser.error("Source helpers/setup_i2c_measurement_env.sh first")

    psu = None
    exit_code = 0
    try:
        psu = PowerSupplyController(args.psu_device, presets={
            1: (args.ch1_voltage, args.ch1_current_limit),
            2: (args.ch2_voltage, args.ch2_current_limit),
        })
        etroc = EtrocController(psu.device, args.iv_config, args.i2c_bus, args.i2c_address)
        MeasurementWorkflow(psu, etroc).run()
    except (KeyboardInterrupt, EOFError):
        print("\nInterrupted; switching controlled PSU outputs OFF.")
        exit_code = 130
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        exit_code = 1
    finally:
        if psu is not None:
            try:
                psu.off()
            except Exception as exc:
                print(f"ERROR: could not confirm PSU shutdown: {exc}", file=sys.stderr)
                exit_code = 2
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
