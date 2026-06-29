#!/usr/bin/env python3
"""ETROC auto-calibration loop with optional SMU bias control.

The script can run in two modes:
  - SMU mode: a supported SMU driver is loaded, so voltage is applied and current is recorded.
  - No-SMU mode: Keithley is missing/disabled, so ETROC calibration still runs,
    but no IV/current SQLite or plot is written.

For each experiment step:
  1. Set SMU bias voltage and current compliance.
  2. Enable output.
  3. Record current before calibration.
  4. Run i2c_test_with_rpi.py and wait until it finishes.
     That script saves baseline/noise-width SQLite results and plots.
  5. Append pre-calibration IV measurements to cumulative IVHistory.sqlite,
     and optionally save per-step calibration logs when requested.
  6. Draw an IV curve plot from the pre-calibration currents after the voltage scan is done.

Recommended JSON config format:
  {
    "chip_name": "test",
    "save_notes": "bias_scan",
    "current_limit": 1e-4,
    "settle": 1.0,
    "cycle_delay": 0.0,
    "check_etroc": true,
    "smu_driver": "keithley",
    "smu_channel": 3,
    "etroc_i2c_bus": 1,
    "etroc_i2c_address": "0x60",
    "voltages": [-2, -3, -4, -5]
  }

Use "smu_driver": "caen" and "smu_channel": 3 for CAEN NDT1470 CH3.

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

Run with CAEN NDT1470 CH3:
  python smu_etroc_calibration_loop.py --smu-driver caen --channel 3 \
    --voltages -50 -100 -150 --current-limit 100e-6

Safety:
  - SMU output is turned OFF when changing voltage.
  - SMU output is turned OFF at the end, including on Ctrl-C or failures.
  - Current compliance defaults to 100 uA if not specified.
  - SMU-specific code lives in separate driver modules, currently smu_keithley.py.
  - If the SMU is not connected, the script automatically falls back to no-SMU
    mode unless --require-smu (or JSON require_smu=true) is set.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from smu_caen import DEFAULT_BOARD as CAEN_DEFAULT_BOARD
from smu_caen import DEFAULT_BAUDRATE as CAEN_DEFAULT_BAUDRATE
from smu_caen import DEFAULT_CHANNEL as CAEN_DEFAULT_CHANNEL
from smu_caen import DEFAULT_DEVICE as CAEN_DEFAULT_DEVICE
from smu_caen import connect_caen
from smu_keithley import DEFAULT_DEVICE as KEITHLEY_DEFAULT_DEVICE
from smu_keithley import connect_keithley


HELPERS_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = HELPERS_DIR / "output"
DEFAULT_FIGURE_ROOT = HELPERS_DIR.parent / "ETROC-figures"
DEFAULT_I2C_SCRIPT = HELPERS_DIR / "i2c_test_with_rpi.py"
DEFAULT_CURRENT_LIMIT = 100e-6
DEFAULT_SETTLE = 1.0
DEFAULT_CYCLE_DELAY = 0.0
DEFAULT_CHIP_NAME = "test"
DEFAULT_ETROC_I2C_BUS = 1
DEFAULT_ETROC_I2C_ADDRESS = 0x60
DEFAULT_ETROC_RECHECK_ATTEMPTS = 3
DEFAULT_ETROC_RECHECK_DELAY = 5.0
DEFAULT_CURRENT_SAMPLES = 1
DEFAULT_CURRENT_SAMPLE_DELAY = 0.1
DEFAULT_CURRENT_STAT = "median"


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


def wait_and_recheck_etroc2(
    i2c_bus: int,
    i2c_address: int,
    *,
    attempts: int = DEFAULT_ETROC_RECHECK_ATTEMPTS,
    delay_s: float = DEFAULT_ETROC_RECHECK_DELAY,
) -> tuple[bool, str]:
    """Wait briefly and re-check ETROC2 before declaring the I2C link lost."""
    attempts = max(int(attempts), 1)
    delay_s = max(float(delay_s), 0.0)
    last_message = ""
    for attempt in range(1, attempts + 1):
        if delay_s > 0:
            print(f"Waiting {delay_s:g} s before ETROC2 I2C re-check {attempt}/{attempts}")
            time.sleep(delay_s)
        connected, last_message = check_etroc2_connected(i2c_bus, i2c_address)
        print(last_message)
        if connected:
            return True, last_message
    return False, last_message


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


def default_device_for_driver(driver: str) -> str:
    if driver == "caen":
        return CAEN_DEFAULT_DEVICE
    if driver == "keithley":
        return KEITHLEY_DEFAULT_DEVICE
    raise ValueError(f"Unsupported SMU driver: {driver}")


def connect_smu_driver(
    driver: str,
    device: str,
    *,
    channel: int | None = None,
    board: int = CAEN_DEFAULT_BOARD,
    baudrate: int = CAEN_DEFAULT_BAUDRATE,
    voltage_magnitude: bool = True,
):
    """Connect a supported SMU driver.

    Keep the dispatch small so adding CAEN later only needs a new module and one
    branch here, while no-SMU calibration remains independent of any hardware.
    """
    if driver == "keithley":
        return connect_keithley(device)
    if driver == "caen":
        return connect_caen(
            device,
            channel=CAEN_DEFAULT_CHANNEL if channel is None else channel,
            board=board,
            baudrate=baudrate,
            voltage_magnitude=voltage_magnitude,
        )
    raise ValueError(f"Unsupported SMU driver: {driver}")


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
    log_path: Path | None,
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
    log_fh = None
    if log_path is not None:
        log_path.write_text(start_line)
        log_fh = log_path.open("ab")
        log_fh.write(b"--- OUTPUT ---\n")
    else:
        print(start_line, end="")

    sys.stdout.flush()
    sys.stderr.flush()

    output_chunks: list[bytes] = []
    proc = subprocess.Popen(
        cmd,
        cwd=str(i2c_script.parent),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
    )

    assert proc.stdout is not None
    try:
        import selectors

        selector = selectors.DefaultSelector()
        selector.register(proc.stdout, selectors.EVENT_READ)
        start_time = time.monotonic()
        while True:
            if timeout_s is not None and (time.monotonic() - start_time) > timeout_s:
                proc.kill()
                proc.wait()
                output = b"".join(output_chunks).decode(errors="replace")
                if log_fh is not None:
                    log_fh.write(f"\n--- TIMEOUT AFTER {timeout_s} s ---\n".encode())
                raise subprocess.TimeoutExpired(cmd, timeout_s, output=output)

            events = selector.select(timeout=0.1)
            for key, _ in events:
                chunk = os.read(key.fileobj.fileno(), 4096)
                if chunk:
                    sys.stdout.buffer.write(chunk)
                    sys.stdout.buffer.flush()
                    if log_fh is not None:
                        log_fh.write(chunk)
                        log_fh.flush()
                    output_chunks.append(chunk)

            if proc.poll() is not None:
                # Drain any remaining buffered output after process exit.
                while True:
                    chunk = proc.stdout.read(4096)
                    if not chunk:
                        break
                    sys.stdout.buffer.write(chunk)
                    sys.stdout.buffer.flush()
                    if log_fh is not None:
                        log_fh.write(chunk)
                    output_chunks.append(chunk)
                break
    finally:
        proc.stdout.close()
        if log_fh is not None:
            log_fh.write(f"\n--- RETURN CODE: {proc.returncode} ---\n".encode())
            log_fh.close()

    output_text = b"".join(output_chunks).decode(errors="replace")
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout=output_text, stderr="")


def safe_return_to_zero(
    smu: Any,
    current_limit: float,
    *,
    voltage_wait: bool,
    voltage_tolerance: float,
    settle: float,
) -> None:
    """Return the bias supply to 0 V without taking any extra measurements."""
    print("Safely returning SMU bias to 0 V without measurement")
    try:
        smu.configure_voltage(0.0, current_limit)
        smu.output_on()
        if voltage_wait and hasattr(smu, "wait_until_voltage"):
            try:
                reached_v, reached_raw = smu.wait_until_voltage(
                    0.0,
                    tolerance=voltage_tolerance,
                )
                print(f"Zero-bias reached: VMON={reached_v} raw={reached_raw}")
            except TimeoutError as exc:
                print(f"WARNING: {exc}; turning output off anyway.", file=sys.stderr)
        elif settle > 0:
            time.sleep(settle)
    finally:
        print("Turning SMU output OFF after zero-bias return")
        smu.output_off()


def summarize_current_samples(samples: list[float], *, statistic: str) -> dict[str, float | int | None]:
    """Summarize repeated current readings and choose one value for the IV curve."""
    if not samples:
        return {
            "selected": None,
            "mean": None,
            "median": None,
            "std": None,
            "min": None,
            "max": None,
            "count": 0,
        }

    stat = statistic.lower()
    mean_value = statistics.fmean(samples)
    median_value = statistics.median(samples)
    if stat == "mean":
        selected = mean_value
    elif stat == "first":
        selected = samples[0]
    elif stat == "last":
        selected = samples[-1]
    else:
        selected = median_value

    return {
        "selected": selected,
        "mean": mean_value,
        "median": median_value,
        "std": statistics.stdev(samples) if len(samples) > 1 else 0.0,
        "min": min(samples),
        "max": max(samples),
        "count": len(samples),
    }


def read_current_summary(
    smu: Any,
    *,
    samples: int,
    sample_delay: float,
    statistic: str,
) -> tuple[dict[str, float | int | None], str]:
    """Read current one or more times and return summary stats plus raw values."""
    samples = max(int(samples), 1)
    sample_delay = max(float(sample_delay), 0.0)
    currents: list[float] = []
    raw_values: list[str] = []

    for sample_idx in range(samples):
        current, raw = smu.read_current()
        raw_values.append(raw)
        if current is not None:
            currents.append(float(current))
        if sample_idx != samples - 1 and sample_delay > 0:
            time.sleep(sample_delay)

    summary = summarize_current_samples(currents, statistic=statistic)
    raw_summary = " | ".join(
        f"{idx}:{raw}" for idx, raw in enumerate(raw_values, start=1)
    )
    return summary, raw_summary


def safe_filename_part(value: str) -> str:
    """Return a compact filesystem-safe label for filenames."""
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in value.strip())
    return "_".join(part for part in safe.split("_") if part) or "scan"


def etroc_figure_dir(chip_name: str | None = None) -> Path:
    """Return the same dated figure directory used by save_baselines()."""
    fig_dir = DEFAULT_FIGURE_ROOT / f"{dt.date.today().isoformat()}_Array_Test_Results"
    if chip_name:
        fig_dir = fig_dir / safe_filename_part(chip_name)
    return fig_dir


def save_iv_rows_sqlite(sqlite_path: Path, rows: list[dict[str, object]]) -> None:
    """Append/update IV rows in a cumulative SQLite file, like BaselineHistory.sqlite."""
    import sqlite3

    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(sqlite_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS iv_measurements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_timestamp TEXT NOT NULL,
                chip_name TEXT,
                save_notes TEXT,
                step INTEGER NOT NULL,
                step_start TEXT,
                before_time TEXT,
                applied_voltage_V REAL,
                current_limit_A REAL,
                smu_idn TEXT,
                before_current_A REAL,
                before_current_mean_A REAL,
                before_current_median_A REAL,
                before_current_std_A REAL,
                before_current_min_A REAL,
                before_current_max_A REAL,
                before_current_samples INTEGER,
                before_raw TEXT,
                calibration_status TEXT,
                calibration_returncode TEXT,
                calibration_log TEXT,
                smu_errors TEXT,
                UNIQUE(run_timestamp, step)
            )
            """
        )
        existing_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(iv_measurements)").fetchall()
        }
        for column_name, column_type in {
            "before_current_mean_A": "REAL",
            "before_current_median_A": "REAL",
            "before_current_std_A": "REAL",
            "before_current_min_A": "REAL",
            "before_current_max_A": "REAL",
            "before_current_samples": "INTEGER",
        }.items():
            if column_name not in existing_columns:
                conn.execute(f"ALTER TABLE iv_measurements ADD COLUMN {column_name} {column_type}")

        conn.executemany(
            """
            INSERT OR REPLACE INTO iv_measurements (
                run_timestamp, chip_name, save_notes, step,
                step_start, before_time,
                applied_voltage_V, current_limit_A, smu_idn,
                before_current_A, before_current_mean_A, before_current_median_A,
                before_current_std_A, before_current_min_A, before_current_max_A,
                before_current_samples, before_raw,
                calibration_status, calibration_returncode, calibration_log,
                smu_errors
            ) VALUES (
                :run_timestamp, :chip_name, :save_notes, :step,
                :step_start, :before_time,
                :applied_voltage_V, :current_limit_A, :smu_idn,
                :before_current_A, :before_current_mean_A, :before_current_median_A,
                :before_current_std_A, :before_current_min_A, :before_current_max_A,
                :before_current_samples, :before_raw,
                :calibration_status, :calibration_returncode, :calibration_log,
                :smu_errors
            )
            """,
            rows,
        )


def load_iv_rows_sqlite(sqlite_path: Path, run_timestamp: str) -> list[dict[str, object]]:
    """Load IV rows for one voltage-scan run from the cumulative SQLite history."""
    import sqlite3

    with sqlite3.connect(sqlite_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT *
            FROM iv_measurements
            WHERE run_timestamp = ?
            ORDER BY step
            """,
            (run_timestamp,),
        ).fetchall()
    return [dict(row) for row in rows]


def plot_iv_curve(
    rows: list[dict[str, object]],
    plot_path: Path,
    *,
    chip_name: str,
    save_notes: str,
) -> None:
    """Draw an IV curve from pre-calibration SMU current readings."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    valid_rows = [
        row for row in rows
        if row.get("before_current_A") is not None
    ]
    if not valid_rows:
        return

    points = sorted(
        (
            abs(float(row["applied_voltage_V"])),
            abs(float(row["before_current_A"])) * 1e6,
        )
        for row in valid_rows
    )
    hv = [point[0] for point in points]
    current_uA = [point[1] for point in points]

    fig, ax = plt.subplots(figsize=(8, 6))
    ax.plot(hv, current_uA, "-", color="#4285F4", linewidth=2.0)
    ax.set_xlabel("HV (V)")
    ax.set_ylabel(f"{chip_name} current (µA)")
    title = f"{chip_name} vs HV (V)"
    if save_notes:
        title += f"\n{save_notes}"
    ax.set_title(title)
    ax.grid(True, color="#d9d9d9", linewidth=1.0)
    ax.set_axisbelow(True)
    fig.tight_layout()
    plot_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(plot_path, dpi=150)
    plt.close(fig)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Loop optional SMU bias/current recording around ETROC auto-calibration"
    )
    parser.add_argument("--config", type=Path, default=None, help="JSON experiment config file")
    parser.add_argument("--device", default=None, help="SMU device path. Driver-specific default when omitted.")
    parser.add_argument("--smu-driver", default=None, choices=["keithley", "caen"], help="SMU driver to use. Default/config: keithley")
    parser.add_argument("--channel", type=int, default=None, help="SMU channel for multi-channel supplies, e.g. CAEN CH3")
    parser.add_argument("--caen-board", type=int, default=None, help="CAEN board address. Default/config: 0")
    parser.add_argument("--caen-baudrate", type=int, default=None, help="CAEN serial baudrate. Default/config: 9600")
    parser.add_argument("--caen-signed-voltage", action="store_true", help="Send signed voltage to CAEN VSET instead of abs(voltage). Default sends magnitude.")
    parser.add_argument("--voltage", type=float, default=None, help="Single bias voltage [V], e.g. -5")
    parser.add_argument("--voltages", type=float, nargs="+", default=None, help="Voltage scan list [V], e.g. --voltages -2 -3 -4 -5")
    parser.add_argument("--current-limit", type=float, default=None, help="Shared current compliance/current limit [A], e.g. 100e-6")
    parser.add_argument("--current-samples", type=int, default=None, help="Number of current readings before calibration. Default/config: 1")
    parser.add_argument("--current-sample-delay", type=float, default=None, help="Delay between repeated current readings [s]. Default/config: 0.1")
    parser.add_argument("--current-stat", default=None, choices=["median", "mean", "first", "last"], help="Statistic used as before_current_A/IV point when multiple readings are taken. Default/config: median")
    parser.add_argument("--cycle-delay", type=float, default=None, help="Seconds to wait between voltage steps")
    parser.add_argument("--settle", type=float, default=None, help="Seconds to wait after enabling/applying bias before reading current")
    parser.add_argument("--skip-voltage-wait", action="store_true", help="Do not wait for drivers with VMON support to reach requested voltage before current read")
    parser.add_argument("--voltage-tolerance", type=float, default=None, help="Voltage tolerance for VMON wait [V]. Default/config: 1.0")
    parser.add_argument("--chip-name", default=None, help="chip_name argument passed to i2c_test_with_rpi.py")
    parser.add_argument("--save-notes", default=None, help="Base save_notes string. Voltage/timestamp are appended automatically.")
    parser.add_argument("--i2c-script", type=Path, default=None, help="Path to i2c_test_with_rpi.py")
    parser.add_argument("--output-dir", type=Path, default=None, help="Directory for SQLite output and optional per-step logs")
    parser.add_argument("--calibration-timeout", type=float, default=None, help="Optional timeout for each calibration run [s]")
    parser.add_argument("--etroc-i2c-bus", type=int, default=None, help="Raspberry Pi I2C bus for ETROC2 connection check. Default/config: 1")
    parser.add_argument("--etroc-i2c-address", default=None, help="ETROC2 7-bit I2C address for connection check, e.g. 0x60")
    parser.add_argument("--etroc-recheck-attempts", type=int, default=None, help="Number of delayed ETROC2 I2C re-checks before safely aborting after a lost connection. Default/config: 3")
    parser.add_argument("--etroc-recheck-delay", type=float, default=None, help="Seconds to wait before each ETROC2 I2C re-check. Default/config: 5")
    parser.add_argument("--skip-etroc-check", action="store_true", help="Disable ETROC2 I2C connection check before each voltage step")
    parser.add_argument("--continue-on-error", action="store_true", help="Continue scan even if calibration script fails")
    parser.add_argument("--save-logs", action="store_true", help="Save per-step calibration stdout logs under output-dir. Default/config: false")
    parser.add_argument("--leave-output-on", action="store_true", help="Do not turn SMU output off at the end. Not recommended.")
    parser.add_argument("--no-smu", action="store_true", help="Force no-SMU mode: run calibration without connecting to/applying Keithley bias")
    parser.add_argument("--require-smu", action="store_true", help="Fail if the selected SMU is not connected instead of falling back to no-SMU mode")
    parser.add_argument("--dry-config", action="store_true", help="Print resolved experiment steps and exit without touching the SMU")
    return parser


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    config = load_json_config(args.config)

    smu_driver = str(cfg_value(config, "smu_driver", args.smu_driver, "keithley")).lower()
    device = cfg_value(config, "device", args.device, default_device_for_driver(smu_driver))
    smu_channel_value = cfg_value(config, "smu_channel", args.channel, config.get("channel"))
    smu_channel = None if smu_channel_value is None else int(smu_channel_value)
    caen_board = int(cfg_value(config, "caen_board", args.caen_board, CAEN_DEFAULT_BOARD))
    caen_baudrate = int(cfg_value(config, "caen_baudrate", args.caen_baudrate, CAEN_DEFAULT_BAUDRATE))
    caen_voltage_magnitude = not bool(config.get("caen_signed_voltage", False) or args.caen_signed_voltage)
    settle = float(cfg_value(config, "settle", args.settle, DEFAULT_SETTLE))
    voltage_wait = bool(config.get("voltage_wait", True)) and not args.skip_voltage_wait
    voltage_tolerance = float(cfg_value(config, "voltage_tolerance", args.voltage_tolerance, 1.0))
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
    etroc_recheck_attempts = int(cfg_value(config, "etroc_recheck_attempts", args.etroc_recheck_attempts, DEFAULT_ETROC_RECHECK_ATTEMPTS))
    etroc_recheck_delay = float(cfg_value(config, "etroc_recheck_delay", args.etroc_recheck_delay, DEFAULT_ETROC_RECHECK_DELAY))
    check_etroc = bool(config.get("check_etroc", True)) and not args.skip_etroc_check
    save_logs = bool(config.get("save_logs", False)) or args.save_logs
    current_samples = int(cfg_value(config, "current_samples", args.current_samples, DEFAULT_CURRENT_SAMPLES))
    current_sample_delay = float(cfg_value(config, "current_sample_delay", args.current_sample_delay, DEFAULT_CURRENT_SAMPLE_DELAY))
    current_stat = str(cfg_value(config, "current_stat", args.current_stat, DEFAULT_CURRENT_STAT)).lower()

    steps = build_steps(config, args.voltage, args.voltages, args.current_limit)

    if settle < 0:
        raise ValueError("settle must be >= 0")
    if cycle_delay < 0:
        raise ValueError("cycle_delay must be >= 0")
    if etroc_recheck_attempts < 1:
        raise ValueError("etroc_recheck_attempts must be >= 1")
    if etroc_recheck_delay < 0:
        raise ValueError("etroc_recheck_delay must be >= 0")
    if current_samples < 1:
        raise ValueError("current_samples must be >= 1")
    if current_sample_delay < 0:
        raise ValueError("current_sample_delay must be >= 0")
    if current_stat not in {"median", "mean", "first", "last"}:
        raise ValueError("current_stat must be one of: median, mean, first, last")
    if not i2c_script.exists():
        raise FileNotFoundError(f"I2C calibration script not found: {i2c_script}")

    print("Resolved experiment steps:")
    for idx, step in enumerate(steps, start=1):
        print(f"  {idx}: voltage={step['voltage']:g} V, current_limit={step['current_limit']:g} A")
    print(f"chip_name={chip_name}")
    print(f"save_notes={save_notes}")
    print(f"settle={settle:g} s, voltage_wait={voltage_wait}, voltage_tolerance={voltage_tolerance:g} V, cycle_delay={cycle_delay:g} s")
    print(f"current_samples={current_samples}, current_sample_delay={current_sample_delay:g} s, current_stat={current_stat}")
    print(
        f"ETROC2 check={check_etroc}, bus={etroc_i2c_bus}, address=0x{etroc_i2c_address:02x}, "
        f"recheck_attempts={etroc_recheck_attempts}, recheck_delay={etroc_recheck_delay:g} s, save_logs={save_logs}"
    )
    print(f"SMU driver={smu_driver}, device={device}")
    if smu_driver == "caen":
        print(
            f"CAEN board={caen_board}, channel={CAEN_DEFAULT_CHANNEL if smu_channel is None else smu_channel}, "
            f"baudrate={caen_baudrate}, voltage_mode={'magnitude' if caen_voltage_magnitude else 'signed'}"
        )

    if args.dry_config:
        print("Dry config check only; SMU/calibration not started.")
        return 0

    output_dir.mkdir(parents=True, exist_ok=True)
    iv_stamp = timestamp_for_file()
    iv_sqlite_path: Path | None = None
    iv_plot_path: Path | None = None
    rows: list[dict[str, object]] = []
    exit_code = 0
    stop_scan = False
    total_steps = len(steps)

    no_smu_requested = bool(config.get("no_smu", False)) or args.no_smu
    require_smu = bool(config.get("require_smu", False)) or args.require_smu
    smu = None
    smu_idn = ""
    smu_note = ""

    if no_smu_requested:
        smu_note = "No-SMU mode requested; Keithley connection skipped."
        print(smu_note)
    else:
        print(f"Checking {smu_driver} SMU connection at {device}")
        try:
            smu = connect_smu_driver(
                smu_driver,
                device,
                channel=smu_channel,
                board=caen_board,
                baudrate=caen_baudrate,
                voltage_magnitude=caen_voltage_magnitude,
            )
            smu_idn = smu.idn
            print(f"SMU connected. IDN: {smu_idn}")
        except Exception as exc:  # noqa: BLE001
            smu_note = f"SMU not available ({exc}); continuing in no-SMU mode."
            if require_smu:
                print(f"SMU connection failed and SMU is required: {exc}", file=sys.stderr)
                return 2
            print(f"WARNING: {smu_note}", file=sys.stderr)

    try:
        if smu is not None:
            old_errors = smu.drain_errors()
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
                    print("ETROC2 is not connected/responding; waiting and re-checking before abort.", file=sys.stderr)
                    connected, _message = wait_and_recheck_etroc2(
                        etroc_i2c_bus,
                        etroc_i2c_address,
                        attempts=etroc_recheck_attempts,
                        delay_s=etroc_recheck_delay,
                    )
                    if not connected:
                        print("ETROC2 is still unavailable. Quitting scan safely.", file=sys.stderr)
                        if smu is not None:
                            smu.output_off()
                        return 5

            if smu is not None:
                print(
                    f"Configuring voltage source: voltage={voltage:g} V, "
                    f"current_limit={current_limit:g} A"
                )
                smu.configure_voltage(voltage, current_limit)
                setup_errors = smu.drain_errors()
                if setup_errors:
                    print("Keithley setup errors:", file=sys.stderr)
                    for err in setup_errors:
                        print(f"  {err}", file=sys.stderr)
                    return 3

                print("Enabling SMU output")
                smu.output_on()
                time.sleep(settle)
                if voltage_wait and hasattr(smu, "wait_until_voltage"):
                    print(f"Waiting for monitored voltage to reach {voltage:g} V")
                    try:
                        reached_v, reached_raw = smu.wait_until_voltage(
                            voltage,
                            tolerance=voltage_tolerance,
                        )
                        print(f"Voltage reached: VMON={reached_v} raw={reached_raw}")
                    except TimeoutError as exc:
                        print(
                            f"WARNING: {exc}; continuing with measured voltage/current before calibration.",
                            file=sys.stderr,
                        )
            else:
                print("No SMU connected: skipping voltage setup, output enable, and current readings")
                setup_errors = []

            step_start = timestamp_iso()
            step_stamp = timestamp_for_file()
            notes_parts = [p for p in [save_notes, f"V_{vtag}", step_stamp] if p]
            step_notes = "_".join(notes_parts)
            log_path = output_dir / f"calibration_V_{vtag}_{step_stamp}.log" if save_logs else None

            before_voltage = None
            before_voltage_raw = ""
            smu_status_raw = ""
            current_summary: dict[str, float | int | None] = {
                "selected": None,
                "mean": None,
                "median": None,
                "std": None,
                "min": None,
                "max": None,
                "count": 0,
            }
            if smu is not None:
                if hasattr(smu, "read_voltage"):
                    before_voltage, before_voltage_raw = smu.read_voltage()
                    print(f"Before voltage: VMON={before_voltage} raw={before_voltage_raw}")
                if hasattr(smu, "read_status"):
                    _status_value, smu_status_raw = smu.read_status()
                    print(f"Before status: raw={smu_status_raw}")
                print(
                    f"Reading current before calibration at Vset={voltage:g} V "
                    f"({current_samples} sample(s), stat={current_stat})"
                )
                current_summary, before_raw = read_current_summary(
                    smu,
                    samples=current_samples,
                    sample_delay=current_sample_delay,
                    statistic=current_stat,
                )
                before_current = current_summary["selected"]
                before_time = timestamp_iso()
                print(
                    f"Before: I={before_current} A, "
                    f"median={current_summary['median']} A, mean={current_summary['mean']} A, "
                    f"std={current_summary['std']} A, n={current_summary['count']} raw={before_raw}"
                )
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
                if log_path is not None:
                    log_path.write_text(
                        f"Calibration timed out after {calibration_timeout} s\n"
                        f"Command: {exc.cmd}\n"
                        f"stdout:\n{exc.stdout or ''}\n"
                        f"stderr:\n{exc.stderr or ''}\n"
                    )

            log_msg = f"; log={log_path}" if log_path is not None else ""
            print(f"Calibration finished: {calibration_status}{log_msg}")

            if smu is not None:
                smu_errors = smu.drain_errors()
                if smu_errors:
                    print("Keithley errors after step:", file=sys.stderr)
                    for err in smu_errors:
                        print(f"  {err}", file=sys.stderr)
            else:
                smu_errors = []

            if smu is not None:
                if iv_sqlite_path is None:
                    iv_sqlite_path = output_dir / "IVHistory.sqlite"
                    plot_label = safe_filename_part(f"{chip_name}_{save_notes}" if save_notes else chip_name)
                    iv_plot_path = etroc_figure_dir(chip_name) / f"{plot_label}_IV_curve_{iv_stamp}.png"

                row = {
                    "run_timestamp": iv_stamp,
                    "chip_name": chip_name,
                    "step": step_index,
                    "step_start": step_start,
                    "before_time": before_time,
                    "applied_voltage_V": voltage,
                    "current_limit_A": current_limit,
                    "smu_idn": smu_idn,
                    "before_voltage_V": before_voltage,
                    "before_voltage_raw": before_voltage_raw,
                    "smu_status_raw": smu_status_raw,
                    "before_current_A": before_current,
                    "before_current_mean_A": current_summary["mean"],
                    "before_current_median_A": current_summary["median"],
                    "before_current_std_A": current_summary["std"],
                    "before_current_min_A": current_summary["min"],
                    "before_current_max_A": current_summary["max"],
                    "before_current_samples": current_summary["count"],
                    "before_raw": before_raw,
                    "calibration_status": calibration_status,
                    "calibration_returncode": calibration_returncode,
                    "calibration_log": str(log_path) if log_path is not None else "",
                    "save_notes": step_notes,
                    "smu_errors": " | ".join(smu_errors),
                }
                rows.append(row)

                if iv_sqlite_path is not None:
                    # Write SQLite every step so partial IV results survive failures/Ctrl-C.
                    save_iv_rows_sqlite(iv_sqlite_path, rows)
                    print(f"Updated IV SQLite: {iv_sqlite_path}")

            if calibration_status != "ok":
                exit_code = 4
                etroc_still_connected = True
                if check_etroc:
                    print(
                        "Calibration failed; checking whether ETROC2 I2C connection was lost "
                        "before deciding how to stop."
                    )
                    etroc_still_connected, _message = wait_and_recheck_etroc2(
                        etroc_i2c_bus,
                        etroc_i2c_address,
                        attempts=etroc_recheck_attempts,
                        delay_s=etroc_recheck_delay,
                    )

                if not etroc_still_connected:
                    print(
                        "ETROC2 I2C connection is still unavailable. "
                        "Safely returning bias to 0 V and quitting scan without further measurements.",
                        file=sys.stderr,
                    )
                    exit_code = 5
                    stop_scan = True
                    if smu is not None:
                        safe_return_to_zero(
                            smu,
                            current_limit,
                            voltage_wait=voltage_wait,
                            voltage_tolerance=voltage_tolerance,
                            settle=settle,
                        )
                elif not args.continue_on_error:
                    print("Stopping because calibration failed. Use --continue-on-error to keep going.")
                    stop_scan = True

            if smu is not None:
                print("Turning output OFF before next step")
                smu.output_off()

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
                    smu.output_off()
                except Exception as exc:  # noqa: BLE001
                    print(f"Warning: failed to turn output off: {exc}", file=sys.stderr)
            smu.close()

    if iv_sqlite_path is not None:
        if iv_plot_path is not None:
            plot_rows = load_iv_rows_sqlite(iv_sqlite_path, iv_stamp)
            plot_iv_curve(plot_rows, iv_plot_path, chip_name=chip_name, save_notes=save_notes)
            print(f"Final IV plot: {iv_plot_path}")
        print(f"Final IV SQLite: {iv_sqlite_path}")
    else:
        print("No SMU was used; no IV SQLite or plot written.")
    print("Done")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
