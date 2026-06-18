#!/usr/bin/env python3
"""Keithley 2470 + ETROC calibration loop with JSON experiment config.

The script can run in two modes:
  - SMU mode: Keithley is connected, so voltage is applied and current is recorded.
  - No-SMU mode: Keithley is missing/disabled, so ETROC calibration still runs,
    but no IV/current CSV, SQLite, or plot is written.

For each experiment step:
  1. Set Keithley bias voltage and current compliance.
  2. Enable output.
  3. Record current before calibration.
  4. Run i2c_test_with_rpi.py and wait until it finishes.
     That script saves baseline/noise-width SQLite results and plots.
  5. Record current after calibration.
  6. Save IV CSV + IV SQLite summary and per-step calibration log.
  7. Draw an IV curve plot after the voltage scan is done.

Recommended JSON config format:
  {
    "chip_name": "test",
    "save_notes": "bias_scan",
    "current_limit": 1e-4,
    "settle": 1.0,
    "cycle_delay": 0.0,
    "check_etroc": true,
    "etroc_i2c_bus": 1,
    "etroc_i2c_address": "0x60",
    "voltages": [-2, -3, -4, -5]
  }

For different current limit per voltage, use explicit steps:
  {
    "chip_name": "test",
    "save_notes": "bias_scan",
    "steps": [
      {"voltage": -2, "current_limit": 1e-4},
      {"voltage": -3, "current_limit": 1e-4},
      {"voltage": -4, "current_limit": 2e-4}
    ]
  }

Run with config:
  source /home/ellie/i2c_etroc/bin/activate
  python /home/ellie/ETL/i2c_gui/helpers/smu_etroc_calibration_loop.py \
    --config /home/ellie/ETL/i2c_gui/helpers/experiment_bias_scan.json

Run without config:
  python smu_etroc_calibration_loop.py --voltages -2 -3 -4 -5 --current-limit 100e-6

Safety:
  - SMU output is turned OFF when changing voltage.
  - SMU output is turned OFF at the end, including on Ctrl-C or failures.
  - Current compliance defaults to 100 uA if not specified.
  - If the SMU is not connected, the script automatically falls back to no-SMU
    mode unless --require-smu (or JSON require_smu=true) is set.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


HELPERS_DIR = Path(__file__).resolve().parent
DEFAULT_DEVICE = "/dev/usbtmc0"
DEFAULT_OUTPUT_DIR = HELPERS_DIR / "output"
DEFAULT_I2C_SCRIPT = HELPERS_DIR / "i2c_test_with_rpi.py"
DEFAULT_CURRENT_LIMIT = 100e-6
DEFAULT_SETTLE = 1.0
DEFAULT_CYCLE_DELAY = 0.0
DEFAULT_CHIP_NAME = "test"
DEFAULT_ETROC_I2C_BUS = 1
DEFAULT_ETROC_I2C_ADDRESS = 0x60


class KeithleyUSBTMC:
    """Small line-oriented USBTMC wrapper for SCPI commands."""

    def __init__(self, device: str = DEFAULT_DEVICE):
        self.device = device
        self._fh = None

    def __enter__(self) -> "KeithleyUSBTMC":
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def open(self) -> None:
        if not os.path.exists(self.device):
            raise FileNotFoundError(
                f"{self.device} not found. Is the Keithley connected and powered on?"
            )
        self._fh = open(self.device, "r+b", buffering=0)

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def write(self, cmd: str) -> None:
        if self._fh is None:
            raise RuntimeError("Keithley device is not open")
        self._fh.write((cmd.rstrip() + "\n").encode("ascii"))

    def read(self, max_bytes: int = 4096) -> str:
        if self._fh is None:
            raise RuntimeError("Keithley device is not open")
        return self._fh.read(max_bytes).decode(errors="replace").strip()

    def query(self, cmd: str, delay_s: float = 0.05) -> str:
        self.write(cmd)
        time.sleep(delay_s)
        return self.read()


def parse_first_float(response: str) -> float | None:
    for token in response.replace(";", ",").split(","):
        token = token.strip()
        if not token:
            continue
        try:
            return float(token)
        except ValueError:
            continue
    return None


def drain_error_queue(smu: KeithleyUSBTMC, max_reads: int = 10) -> list[str]:
    errors: list[str] = []
    for _ in range(max_reads):
        err = smu.query(":SYST:ERR?", delay_s=0.05)
        if err.startswith("0"):
            break
        errors.append(err)
    return errors


def configure_voltage_source(smu: KeithleyUSBTMC, voltage: float, current_limit: float) -> None:
    # Conservative Keithley 2470 SCPI set known to work on firmware 1.7.7b.
    smu.write(":OUTP OFF")
    smu.write(":SOUR:FUNC VOLT")
    smu.write(":SOUR:VOLT:RANG:AUTO ON")
    smu.write(f":SOUR:VOLT {voltage}")
    smu.write(f":SOUR:VOLT:ILIM {current_limit}")
    smu.write(":SENS:CURR:RANG:AUTO ON")


def read_current(smu: KeithleyUSBTMC) -> tuple[float | None, str]:
    raw = smu.query(":MEAS:CURR?", delay_s=0.15)
    return parse_first_float(raw), raw


def connect_keithley(device: str) -> tuple[KeithleyUSBTMC, str]:
    """Open the Keithley and query *IDN? so connection problems are caught early."""
    smu = KeithleyUSBTMC(device)
    try:
        smu.open()
        idn = smu.query("*IDN?")
        if not idn:
            raise RuntimeError("Keithley opened but did not respond to *IDN?")
        return smu, idn
    except Exception:
        smu.close()
        raise


def voltage_tag(voltage: float) -> str:
    return f"{voltage:g}V".replace("-", "minus").replace("+", "plus").replace(".", "p")


def timestamp_for_file() -> str:
    return dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def timestamp_iso() -> str:
    return dt.datetime.now().isoformat(sep=" ", timespec="milliseconds")



def parse_i2c_address(value: Any) -> int:
    if isinstance(value, int):
        address = value
    elif isinstance(value, str):
        address = int(value, 0)  # accepts "0x60" or "96"
    else:
        raise ValueError(f"Invalid I2C address value: {value!r}")
    if not (0 <= address <= 0x7F):
        raise ValueError(f"I2C address must be a 7-bit address, got {address!r}")
    return address


def check_etroc2_connected(i2c_bus: int, i2c_address: int) -> tuple[bool, str]:
    """Return whether ETROC2 ACKs on the selected Raspberry Pi I2C bus."""
    try:
        from smbus2 import SMBus
    except Exception as exc:  # noqa: BLE001
        return False, f"Could not import smbus2: {exc}"

    try:
        with SMBus(i2c_bus) as bus:
            bus.write_quick(i2c_address)
        return True, f"ETROC2 ACK at bus={i2c_bus}, address=0x{i2c_address:02x}"
    except Exception as exc:  # noqa: BLE001
        return False, f"No ETROC2 ACK at bus={i2c_bus}, address=0x{i2c_address:02x}: {exc}"


def load_json_config(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    with path.open() as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("JSON config root must be an object/dictionary")
    return data


def cfg_value(config: dict[str, Any], key: str, cli_value: Any, default: Any) -> Any:
    """CLI value wins over config; config wins over default."""
    if cli_value is not None:
        return cli_value
    return config.get(key, default)


def build_steps(config: dict[str, Any], cli_voltage: float | None, cli_voltages: list[float] | None, cli_current_limit: float | None) -> list[dict[str, float]]:
    default_current_limit = float(config.get("current_limit", DEFAULT_CURRENT_LIMIT))
    if cli_current_limit is not None:
        default_current_limit = float(cli_current_limit)

    if "steps" in config and (cli_voltage is None and cli_voltages is None):
        raw_steps = config["steps"]
        if not isinstance(raw_steps, list) or not raw_steps:
            raise ValueError("config 'steps' must be a non-empty list")
        steps: list[dict[str, float]] = []
        for idx, raw_step in enumerate(raw_steps, start=1):
            if not isinstance(raw_step, dict):
                raise ValueError(f"steps[{idx}] must be an object/dictionary")
            if "voltage" not in raw_step:
                raise ValueError(f"steps[{idx}] is missing required field 'voltage'")
            voltage = float(raw_step["voltage"])
            current_limit = float(raw_step.get("current_limit", default_current_limit))
            if current_limit <= 0:
                raise ValueError(f"steps[{idx}] current_limit must be positive")
            steps.append({"voltage": voltage, "current_limit": current_limit})
        return steps

    if cli_voltage is not None and cli_voltages is not None:
        raise ValueError("Use either --voltage or --voltages, not both")
    if cli_voltages is not None:
        voltages = cli_voltages
    elif cli_voltage is not None:
        voltages = [cli_voltage]
    elif "voltages" in config:
        voltages = config["voltages"]
    elif "voltage" in config:
        voltages = [config["voltage"]]
    else:
        raise ValueError("Provide voltages using JSON 'voltages', JSON 'steps', --voltage, or --voltages")

    if not isinstance(voltages, list) or not voltages:
        raise ValueError("voltages must be a non-empty list")

    current_limits = config.get("current_limits")
    if current_limits is not None:
        if not isinstance(current_limits, list) or not current_limits:
            raise ValueError("current_limits must be a non-empty list")
        if len(current_limits) != len(voltages):
            raise ValueError("current_limits length must match voltages length. For one shared limit, use current_limit.")
        steps = [
            {"voltage": float(voltage), "current_limit": float(current_limit)}
            for voltage, current_limit in zip(voltages, current_limits, strict=True)
        ]
    else:
        steps = [
            {"voltage": float(voltage), "current_limit": default_current_limit}
            for voltage in voltages
        ]

    for idx, step in enumerate(steps, start=1):
        if step["current_limit"] <= 0:
            raise ValueError(f"step {idx} current_limit must be positive")
    return steps


def run_calibration(
    *,
    python_exe: str,
    i2c_script: Path,
    chip_name: str,
    save_notes: str,
    log_path: Path,
    timeout_s: float | None,
) -> subprocess.CompletedProcess[str]:
    cmd = [
        python_exe,
        str(i2c_script),
        "--chip_name",
        chip_name,
        "--save_notes",
        save_notes,
    ]

    start_line = f"$ {' '.join(cmd)}\n\n"
    log_path.write_text(start_line)

    proc = subprocess.run(
        cmd,
        cwd=str(i2c_script.parent),
        text=True,
        capture_output=True,
        timeout=timeout_s,
    )

    with log_path.open("a") as f:
        f.write("--- STDOUT ---\n")
        f.write(proc.stdout)
        f.write("\n--- STDERR ---\n")
        f.write(proc.stderr)
        f.write(f"\n--- RETURN CODE: {proc.returncode} ---\n")

    return proc


def save_iv_rows_sqlite(sqlite_path: Path, rows: list[dict[str, object]]) -> None:
    """Write IV rows to a dedicated SQLite file for this voltage scan."""
    import sqlite3

    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(sqlite_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS iv_measurements (
                step INTEGER PRIMARY KEY,
                step_start TEXT,
                before_time TEXT,
                after_time TEXT,
                applied_voltage_V REAL,
                current_limit_A REAL,
                smu_idn TEXT,
                before_current_A REAL,
                after_current_A REAL,
                before_raw TEXT,
                after_raw TEXT,
                calibration_status TEXT,
                calibration_returncode TEXT,
                calibration_log TEXT,
                save_notes TEXT,
                smu_errors TEXT
            )
            """
        )
        conn.execute("DELETE FROM iv_measurements")
        conn.executemany(
            """
            INSERT INTO iv_measurements (
                step, step_start, before_time, after_time,
                applied_voltage_V, current_limit_A, smu_idn,
                before_current_A, after_current_A, before_raw, after_raw,
                calibration_status, calibration_returncode, calibration_log,
                save_notes, smu_errors
            ) VALUES (
                :step, :step_start, :before_time, :after_time,
                :applied_voltage_V, :current_limit_A, :smu_idn,
                :before_current_A, :after_current_A, :before_raw, :after_raw,
                :calibration_status, :calibration_returncode, :calibration_log,
                :save_notes, :smu_errors
            )
            """,
            rows,
        )


def plot_iv_curve(rows: list[dict[str, object]], plot_path: Path) -> None:
    """Draw before/after-calibration IV curves from collected SMU rows."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    valid_rows = [
        row for row in rows
        if row.get("before_current_A") is not None or row.get("after_current_A") is not None
    ]
    if not valid_rows:
        return

    voltage = [float(row["applied_voltage_V"]) for row in valid_rows]
    before = [row.get("before_current_A") for row in valid_rows]
    after = [row.get("after_current_A") for row in valid_rows]

    fig, ax = plt.subplots(figsize=(8, 6))
    ax.plot(voltage, before, "o-", label="Before calibration")
    ax.plot(voltage, after, "s-", label="After calibration")
    ax.set_xlabel("Applied voltage [V]")
    ax.set_ylabel("Measured current [A]")
    ax.set_title("IV curve")
    ax.grid(True, alpha=0.3)
    ax.legend()
    fig.tight_layout()
    plot_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(plot_path, dpi=150)
    plt.close(fig)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Loop Keithley bias/current recording around ETROC auto-calibration"
    )
    parser.add_argument("--config", type=Path, default=None, help="JSON experiment config file")
    parser.add_argument("--device", default=None, help="Keithley USBTMC device path")
    parser.add_argument("--voltage", type=float, default=None, help="Single bias voltage [V], e.g. -5")
    parser.add_argument("--voltages", type=float, nargs="+", default=None, help="Voltage scan list [V], e.g. --voltages -2 -3 -4 -5")
    parser.add_argument("--current-limit", type=float, default=None, help="Shared current compliance/current limit [A], e.g. 100e-6")
    parser.add_argument("--cycle-delay", type=float, default=None, help="Seconds to wait between voltage steps")
    parser.add_argument("--settle", type=float, default=None, help="Seconds to wait after enabling/applying bias before reading current")
    parser.add_argument("--chip-name", default=None, help="chip_name argument passed to i2c_test_with_rpi.py")
    parser.add_argument("--save-notes", default=None, help="Base save_notes string. Voltage/timestamp are appended automatically.")
    parser.add_argument("--i2c-script", type=Path, default=None, help="Path to i2c_test_with_rpi.py")
    parser.add_argument("--output-dir", type=Path, default=None, help="Directory for CSV summary and per-step logs")
    parser.add_argument("--calibration-timeout", type=float, default=None, help="Optional timeout for each calibration run [s]")
    parser.add_argument("--etroc-i2c-bus", type=int, default=None, help="Raspberry Pi I2C bus for ETROC2 connection check. Default/config: 1")
    parser.add_argument("--etroc-i2c-address", default=None, help="ETROC2 7-bit I2C address for connection check, e.g. 0x60")
    parser.add_argument("--skip-etroc-check", action="store_true", help="Disable ETROC2 I2C connection check before each voltage step")
    parser.add_argument("--continue-on-error", action="store_true", help="Continue scan even if calibration script fails")
    parser.add_argument("--leave-output-on", action="store_true", help="Do not turn SMU output off at the end. Not recommended.")
    parser.add_argument("--no-smu", action="store_true", help="Force no-SMU mode: run calibration without connecting to/applying Keithley bias")
    parser.add_argument("--require-smu", action="store_true", help="Fail if the Keithley is not connected instead of falling back to no-SMU mode")
    parser.add_argument("--dry-config", action="store_true", help="Print resolved experiment steps and exit without touching the SMU")
    return parser


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    config = load_json_config(args.config)

    device = cfg_value(config, "device", args.device, DEFAULT_DEVICE)
    settle = float(cfg_value(config, "settle", args.settle, DEFAULT_SETTLE))
    cycle_delay = float(cfg_value(config, "cycle_delay", args.cycle_delay, DEFAULT_CYCLE_DELAY))
    chip_name = str(cfg_value(config, "chip_name", args.chip_name, DEFAULT_CHIP_NAME))
    save_notes = str(cfg_value(config, "save_notes", args.save_notes, ""))
    i2c_script = Path(cfg_value(config, "i2c_script", args.i2c_script, DEFAULT_I2C_SCRIPT))
    output_dir = Path(cfg_value(config, "output_dir", args.output_dir, DEFAULT_OUTPUT_DIR))
    calibration_timeout = cfg_value(config, "calibration_timeout", args.calibration_timeout, None)
    if calibration_timeout is not None:
        calibration_timeout = float(calibration_timeout)
    etroc_i2c_bus = int(cfg_value(config, "etroc_i2c_bus", args.etroc_i2c_bus, DEFAULT_ETROC_I2C_BUS))
    etroc_address_value = cfg_value(config, "etroc_i2c_address", args.etroc_i2c_address, DEFAULT_ETROC_I2C_ADDRESS)
    etroc_i2c_address = parse_i2c_address(etroc_address_value)
    check_etroc = bool(config.get("check_etroc", True)) and not args.skip_etroc_check

    steps = build_steps(config, args.voltage, args.voltages, args.current_limit)

    if settle < 0:
        raise ValueError("settle must be >= 0")
    if cycle_delay < 0:
        raise ValueError("cycle_delay must be >= 0")
    if not i2c_script.exists():
        raise FileNotFoundError(f"I2C calibration script not found: {i2c_script}")

    print("Resolved experiment steps:")
    for idx, step in enumerate(steps, start=1):
        print(f"  {idx}: voltage={step['voltage']:g} V, current_limit={step['current_limit']:g} A")
    print(f"chip_name={chip_name}")
    print(f"save_notes={save_notes}")
    print(f"settle={settle:g} s, cycle_delay={cycle_delay:g} s")
    print(f"ETROC2 check={check_etroc}, bus={etroc_i2c_bus}, address=0x{etroc_i2c_address:02x}")

    if args.dry_config:
        print("Dry config check only; SMU/calibration not started.")
        return 0

    output_dir.mkdir(parents=True, exist_ok=True)
    iv_stamp = timestamp_for_file()
    csv_path: Path | None = None
    iv_sqlite_path: Path | None = None
    iv_plot_path: Path | None = None
    rows: list[dict[str, object]] = []
    exit_code = 0
    stop_scan = False
    total_steps = len(steps)

    no_smu_requested = bool(config.get("no_smu", False)) or args.no_smu
    require_smu = bool(config.get("require_smu", False)) or args.require_smu
    smu: KeithleyUSBTMC | None = None
    smu_idn = ""
    smu_note = ""

    if no_smu_requested:
        smu_note = "No-SMU mode requested; Keithley connection skipped."
        print(smu_note)
    else:
        print(f"Checking Keithley connection at {device}")
        try:
            smu, smu_idn = connect_keithley(device)
            print(f"SMU connected. IDN: {smu_idn}")
        except Exception as exc:  # noqa: BLE001
            smu_note = f"SMU not available ({exc}); continuing in no-SMU mode."
            if require_smu:
                print(f"SMU connection failed and SMU is required: {exc}", file=sys.stderr)
                return 2
            print(f"WARNING: {smu_note}", file=sys.stderr)

    try:
        if smu is not None:
            old_errors = drain_error_queue(smu)
            if old_errors:
                print("Cleared old Keithley error queue entries:")
                for err in old_errors:
                    print(f"  {err}")

        for step_index, step_config in enumerate(steps, start=1):
            if stop_scan:
                break

            voltage = step_config["voltage"]
            current_limit = step_config["current_limit"]
            vtag = voltage_tag(voltage)

            print(f"\n=== Step {step_index}/{total_steps}: {voltage:g} V ===")
            if check_etroc:
                print(f"Checking ETROC2 connection on I2C bus {etroc_i2c_bus}, address 0x{etroc_i2c_address:02x}")
                connected, message = check_etroc2_connected(etroc_i2c_bus, etroc_i2c_address)
                print(message)
                if not connected:
                    print("ETROC2 is not connected/responding. Quitting.", file=sys.stderr)
                    if smu is not None:
                        smu.write(":OUTP OFF")
                    return 5

            if smu is not None:
                print(
                    f"Configuring voltage source: voltage={voltage:g} V, "
                    f"current_limit={current_limit:g} A"
                )
                configure_voltage_source(smu, voltage, current_limit)
                setup_errors = drain_error_queue(smu)
                if setup_errors:
                    print("Keithley setup errors:", file=sys.stderr)
                    for err in setup_errors:
                        print(f"  {err}", file=sys.stderr)
                    return 3

                print("Enabling SMU output")
                smu.write(":OUTP ON")
                time.sleep(settle)
            else:
                print("No SMU connected: skipping voltage setup, output enable, and current readings")
                setup_errors = []

            step_start = timestamp_iso()
            step_stamp = timestamp_for_file()
            notes_parts = [p for p in [save_notes, f"V_{vtag}", step_stamp] if p]
            step_notes = "_".join(notes_parts)
            log_path = output_dir / f"calibration_V_{vtag}_{step_stamp}.log"

            if smu is not None:
                print(f"Reading current before calibration at Vset={voltage:g} V")
                before_current, before_raw = read_current(smu)
                before_time = timestamp_iso()
                print(f"Before: I={before_current} A raw={before_raw}")
            else:
                before_current = None
                before_raw = "NO_SMU"
                before_time = timestamp_iso()

            print("Starting ETROC auto-calibration")
            try:
                proc = run_calibration(
                    python_exe=sys.executable,
                    i2c_script=i2c_script,
                    chip_name=chip_name,
                    save_notes=step_notes,
                    log_path=log_path,
                    timeout_s=calibration_timeout,
                )
                calibration_returncode: int | str = proc.returncode
                calibration_status = "ok" if proc.returncode == 0 else "failed"
            except subprocess.TimeoutExpired as exc:
                calibration_returncode = "timeout"
                calibration_status = "timeout"
                log_path.write_text(
                    f"Calibration timed out after {calibration_timeout} s\n"
                    f"Command: {exc.cmd}\n"
                    f"stdout:\n{exc.stdout or ''}\n"
                    f"stderr:\n{exc.stderr or ''}\n"
                )

            print(f"Calibration finished: {calibration_status}; log={log_path}")

            if smu is not None:
                print("Reading current after calibration")
                after_current, after_raw = read_current(smu)
                after_time = timestamp_iso()
                print(f"After: I={after_current} A raw={after_raw}")

                smu_errors = drain_error_queue(smu)
                if smu_errors:
                    print("Keithley errors after step:", file=sys.stderr)
                    for err in smu_errors:
                        print(f"  {err}", file=sys.stderr)
            else:
                after_current = None
                after_raw = "NO_SMU"
                after_time = timestamp_iso()
                smu_errors = []

            if smu is not None:
                if csv_path is None:
                    csv_path = output_dir / f"smu_etroc_calibration_loop_{iv_stamp}.csv"
                    iv_sqlite_path = output_dir / f"smu_iv_measurements_{iv_stamp}.sqlite"
                    iv_plot_path = output_dir / f"smu_iv_curve_{iv_stamp}.png"

                row = {
                    "step": step_index,
                    "step_start": step_start,
                    "before_time": before_time,
                    "after_time": after_time,
                    "applied_voltage_V": voltage,
                    "current_limit_A": current_limit,
                    "smu_idn": smu_idn,
                    "before_current_A": before_current,
                    "after_current_A": after_current,
                    "before_raw": before_raw,
                    "after_raw": after_raw,
                    "calibration_status": calibration_status,
                    "calibration_returncode": calibration_returncode,
                    "calibration_log": str(log_path),
                    "save_notes": step_notes,
                    "smu_errors": " | ".join(smu_errors),
                }
                rows.append(row)

                # Write CSV every step so partial IV results survive failures/Ctrl-C.
                with csv_path.open("w", newline="") as f:
                    writer = csv.DictWriter(f, fieldnames=list(row.keys()))
                    writer.writeheader()
                    writer.writerows(rows)
                print(f"Updated IV CSV: {csv_path}")

                if iv_sqlite_path is not None:
                    save_iv_rows_sqlite(iv_sqlite_path, rows)
                    print(f"Updated IV SQLite: {iv_sqlite_path}")

            if calibration_status != "ok":
                exit_code = 4
                if not args.continue_on_error:
                    print("Stopping because calibration failed. Use --continue-on-error to keep going.")
                    stop_scan = True

            if smu is not None:
                print("Turning output OFF before next step")
                smu.write(":OUTP OFF")

            if not stop_scan and step_index != total_steps and cycle_delay > 0:
                print(f"Waiting {cycle_delay:g} s before next step")
                time.sleep(cycle_delay)

    except KeyboardInterrupt:
        print("\nInterrupted by user", file=sys.stderr)
        exit_code = 130
    finally:
        if smu is not None:
            if args.leave_output_on:
                print("Leaving SMU output ON because --leave-output-on was set")
            else:
                print("Turning SMU output OFF")
                try:
                    smu.write(":OUTP OFF")
                except Exception as exc:  # noqa: BLE001
                    print(f"Warning: failed to turn output off: {exc}", file=sys.stderr)
            smu.close()

    if csv_path is not None:
        if iv_plot_path is not None:
            try:
                plot_iv_curve(rows, iv_plot_path)
                print(f"Final IV plot: {iv_plot_path}")
            except Exception as exc:  # noqa: BLE001
                print(f"Warning: failed to draw IV plot: {exc}", file=sys.stderr)
        print(f"Final IV CSV: {csv_path}")
        if iv_sqlite_path is not None:
            print(f"Final IV SQLite: {iv_sqlite_path}")
    else:
        print("No SMU was used; no IV CSV, SQLite, or plot written.")
    print("Done")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
