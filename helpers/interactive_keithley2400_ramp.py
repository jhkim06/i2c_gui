#!/usr/bin/env python3
"""Interactive Keithley 2400 voltage ramp controller.

Safe-ish interactive helper based on the Keithley 2400 handling used by
smu_etroc_calibration_loop.py, but without ETROC calibration.

Features:
  - Connect to Keithley 2400 over the existing smu_keithley_2400.py driver.
  - Ramp gradually to target voltage using configurable step size and delay.
  - Read and print measured voltage/current at every ramp step.
  - Interactive commands for ramping, holding, status, zero, output off, quit.
  - After normal target ramps, hold indefinitely and print voltage/current until Ctrl-C.
  - On Ctrl-C/error/quit, ramp back to 0 V and turn output off by default.

Example:
  /home/ellie/i2c_etroc/bin/python interactive_keithley2400_ramp.py

Commands inside interactive mode:
  set -180              ramp to -180 V, then hold indefinitely until Ctrl-C
  set -180 5 1          ramp to -180 V, 5 V steps, 1 s delay, then hold indefinitely
  ramp -100             alias for set
  wait 60               hold for 60 s, printing status periodically
  wait forever          hold indefinitely, printing status periodically until Ctrl-C
  status                read and print measured voltage/current once
  params                show current defaults
  compliance 100e-6     change current compliance for future voltage changes
  step 5                change default ramp step size in volts
  delay 1               change default per-step delay in seconds
  zero                  ramp to 0 V
  off                   ramp to 0 V, then output OFF
  quit                  ramp to 0 V, output OFF, close
  help                  show commands
"""

from __future__ import annotations

import argparse
import math
import sys
import time
from pathlib import Path
from typing import Sequence

# Keep imports local to this helpers directory, matching the calibration loop.
HELPERS_DIR = Path(__file__).resolve().parent
if str(HELPERS_DIR) not in sys.path:
    sys.path.insert(0, str(HELPERS_DIR))

from smu_keithley_2400 import DEFAULT_DEVICE, Keithley2400IO, drain_error_queue  # noqa: E402


DEFAULT_CURRENT_LIMIT = 100e-6
DEFAULT_STEP_V = 5.0
DEFAULT_DELAY_S = 1.0
DEFAULT_HOLD_STATUS_PERIOD_S = 5.0
DEFAULT_MAX_ABS_VOLTAGE = 250.0


def timestamp() -> str:
    import datetime as dt

    return dt.datetime.now().isoformat(sep=" ", timespec="milliseconds")


def parse_floats(response: str) -> list[float]:
    values: list[float] = []
    for token in response.replace(";", ",").split(","):
        token = token.strip()
        if not token:
            continue
        try:
            values.append(float(token))
        except ValueError:
            continue
    return values


class Keithley2400RampController:
    def __init__(
        self,
        *,
        device: str,
        baudrate: int,
        timeout: float,
        current_limit: float,
        step_v: float,
        delay_s: float,
        max_abs_voltage: float,
        assume_start_voltage: float,
    ):
        self.device = device
        self.baudrate = baudrate
        self.timeout = timeout
        self.current_limit = float(current_limit)
        self.step_v = abs(float(step_v))
        self.delay_s = float(delay_s)
        self.max_abs_voltage = abs(float(max_abs_voltage))
        self.set_voltage = float(assume_start_voltage)
        self.output_enabled = False
        self.opened = False
        self.smu = Keithley2400IO(device, baudrate=baudrate, timeout=timeout)
        self.idn = ""

    def open(self) -> None:
        self.smu.open()
        self.opened = True
        self.idn = self.smu.query("*IDN?", delay_s=0.1)
        if not self.idn:
            raise RuntimeError("Keithley 2400 opened but did not respond to *IDN?")
        print(f"Connected: {self.idn}")
        self.configure_source(self.set_voltage)
        errors = drain_error_queue(self.smu)
        if errors:
            print("Cleared old Keithley error queue entries:")
            for err in errors:
                print(f"  {err}")

    def close(self) -> None:
        self.smu.close()
        self.opened = False
        self.output_enabled = False

    def configure_source(self, voltage: float) -> None:
        """Configure source/measure mode. Does not turn output on."""
        self._check_voltage(voltage)
        if self.current_limit <= 0:
            raise ValueError("current_limit must be positive")
        self.smu.write(":SOUR:FUNC VOLT")
        self.smu.write(":SOUR:VOLT:MODE FIXED")
        self.smu.write(":SOUR:VOLT:RANG:AUTO ON")
        self.smu.write(f":SOUR:VOLT:LEV {voltage}")
        # Match smu_keithley_2400.py: source voltage and measure current.
        # FORM:ELEM below also asks the 2400 to include voltage in READ? output.
        self.smu.write(":SENS:FUNC \"CURR\"")
        self.smu.write(":SENS:CURR:RANG:AUTO ON")
        self.smu.write(f":SENS:CURR:PROT {self.current_limit}")
        self.smu.write(":SENS:CURR:NPLC 0.1")
        # Ask READ? to return measured voltage and current. A Keithley 2400 may
        # append resistance/time/status too; read_status() parses the first two.
        self.smu.write(":FORM:ELEM VOLT,CURR")
        self.set_voltage = float(voltage)

    def output_on(self) -> None:
        self.smu.write(":OUTP ON")
        self.output_enabled = True

    def output_off(self) -> None:
        self.smu.write(":OUTP OFF")
        self.output_enabled = False

    def set_source_voltage(self, voltage: float) -> None:
        self._check_voltage(voltage)
        self.smu.write(f":SOUR:VOLT:LEV {voltage}")
        self.set_voltage = float(voltage)

    def read_status(self) -> tuple[float | None, float | None, str]:
        """Return measured voltage [V], measured current [A], raw response."""
        # Keep FORM explicit in case another command changed it.
        self.smu.write(":FORM:ELEM VOLT,CURR")
        raw = self.smu.query(":READ?", delay_s=0.05)
        values = parse_floats(raw)
        measured_v = values[0] if len(values) >= 1 else None
        measured_i = values[1] if len(values) >= 2 else None
        return measured_v, measured_i, raw

    def print_status(self, *, prefix: str = "STATUS") -> None:
        measured_v, measured_i, raw = self.read_status()
        mv = "nan" if measured_v is None else f"{measured_v:.6g}"
        mi = "nan" if measured_i is None else f"{measured_i:.6e}"
        print(
            f"[{timestamp()}] {prefix}: "
            f"set={self.set_voltage:.6g} V, measured={mv} V, current={mi} A, "
            f"output={'ON' if self.output_enabled else 'OFF'}, raw={raw!r}",
            flush=True,
        )

    def ramp_to(self, target_voltage: float, *, step_v: float | None = None, delay_s: float | None = None) -> None:
        self._check_voltage(target_voltage)
        step = abs(self.step_v if step_v is None else float(step_v))
        delay = self.delay_s if delay_s is None else float(delay_s)
        if step <= 0:
            raise ValueError("step_v must be positive")
        if delay < 0:
            raise ValueError("delay_s must be >= 0")

        target = float(target_voltage)
        start = float(self.set_voltage)
        if math.isclose(start, target, abs_tol=1e-12):
            if not self.output_enabled:
                self.output_on()
            self.print_status(prefix="TARGET")
            return

        direction = 1.0 if target > start else -1.0
        values: list[float] = []
        v = start
        while True:
            next_v = v + direction * step
            if (direction > 0 and next_v >= target) or (direction < 0 and next_v <= target):
                next_v = target
            values.append(next_v)
            if math.isclose(next_v, target, abs_tol=1e-12):
                break
            v = next_v

        print(
            f"Ramping from {start:g} V to {target:g} V "
            f"in {len(values)} step(s), step={step:g} V, delay={delay:g} s, "
            f"compliance={self.current_limit:g} A"
        )
        if not self.output_enabled:
            self.output_on()

        for idx, voltage in enumerate(values, start=1):
            self.set_source_voltage(voltage)
            if delay > 0:
                time.sleep(delay)
            self.print_status(prefix=f"RAMP {idx}/{len(values)}")

    def hold(self, seconds: float, *, period_s: float = DEFAULT_HOLD_STATUS_PERIOD_S) -> None:
        seconds = float(seconds)
        period_s = max(float(period_s), 0.1)
        if seconds < 0:
            raise ValueError("hold time must be >= 0")
        print(f"Holding for {seconds:g} s; status period={period_s:g} s")
        end = time.monotonic() + seconds
        while True:
            self.print_status(prefix="HOLD")
            remaining = end - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(period_s, remaining))

    def hold_forever(self, *, period_s: float = DEFAULT_HOLD_STATUS_PERIOD_S) -> None:
        period_s = max(float(period_s), 0.1)
        print(
            f"Holding indefinitely at {self.set_voltage:g} V; "
            f"status period={period_s:g} s. Press Ctrl-C to return to the prompt."
        )
        while True:
            self.print_status(prefix="HOLD")
            time.sleep(period_s)

    def safe_shutdown(self, *, ramp_to_zero: bool = True) -> None:
        try:
            if self.opened:
                if ramp_to_zero and not math.isclose(self.set_voltage, 0.0, abs_tol=1e-12):
                    print("Safe shutdown: ramping to 0 V")
                    self.ramp_to(0.0)
                print("Safe shutdown: output OFF")
                self.output_off()
        finally:
            self.close()

    def _check_voltage(self, voltage: float) -> None:
        if abs(float(voltage)) > self.max_abs_voltage:
            raise ValueError(
                f"Requested voltage {voltage:g} V exceeds max_abs_voltage={self.max_abs_voltage:g} V. "
                "Restart with --max-abs-voltage if this is intentional."
            )


def print_help() -> None:
    print(__doc__.split("Commands inside interactive mode:", 1)[1].strip())


def parse_command(line: str) -> tuple[str, list[str]]:
    parts = line.strip().split()
    if not parts:
        return "", []
    return parts[0].lower(), parts[1:]


def interactive_loop(
    controller: Keithley2400RampController,
    *,
    no_ramp_on_quit: bool = False,
    hold_period_s: float = DEFAULT_HOLD_STATUS_PERIOD_S,
) -> None:
    print("Type 'help' for commands. Recommended exit: quit")
    while True:
        try:
            line = input("keithley2400> ")
        except EOFError:
            print()
            line = "quit"

        cmd, args = parse_command(line)
        if not cmd:
            continue

        try:
            if cmd in {"help", "?"}:
                print_help()
            elif cmd in {"set", "ramp"}:
                if not args:
                    print("Usage: set TARGET_V [STEP_V] [DELAY_S]")
                    continue
                target = float(args[0])
                step = float(args[1]) if len(args) >= 2 else None
                delay = float(args[2]) if len(args) >= 3 else None
                controller.ramp_to(target, step_v=step, delay_s=delay)
                controller.hold_forever(period_s=hold_period_s)
            elif cmd == "wait":
                if not args:
                    print("Usage: wait SECONDS|forever [STATUS_PERIOD_S]")
                    continue
                period = float(args[1]) if len(args) >= 2 else hold_period_s
                if args[0].lower() in {"forever", "inf", "infinite", "permanent"}:
                    controller.hold_forever(period_s=period)
                else:
                    seconds = float(args[0])
                    controller.hold(seconds, period_s=period)
            elif cmd == "status":
                controller.print_status()
            elif cmd == "params":
                print(
                    f"device={controller.device}, set_voltage={controller.set_voltage:g} V, "
                    f"current_limit={controller.current_limit:g} A, step={controller.step_v:g} V, "
                    f"delay={controller.delay_s:g} s, hold_period={hold_period_s:g} s, "
                    f"max_abs_voltage={controller.max_abs_voltage:g} V, "
                    f"output={'ON' if controller.output_enabled else 'OFF'}"
                )
            elif cmd == "compliance":
                if len(args) != 1:
                    print("Usage: compliance CURRENT_LIMIT_A")
                    continue
                value = float(args[0])
                if value <= 0:
                    print("Current compliance must be positive")
                    continue
                controller.current_limit = value
                controller.smu.write(f":SENS:CURR:PROT {controller.current_limit}")
                print(f"Current compliance set to {controller.current_limit:g} A")
            elif cmd == "step":
                if len(args) != 1:
                    print("Usage: step STEP_V")
                    continue
                value = abs(float(args[0]))
                if value <= 0:
                    print("Step must be positive")
                    continue
                controller.step_v = value
                print(f"Default step set to {controller.step_v:g} V")
            elif cmd == "delay":
                if len(args) != 1:
                    print("Usage: delay DELAY_S")
                    continue
                value = float(args[0])
                if value < 0:
                    print("Delay must be >= 0")
                    continue
                controller.delay_s = value
                print(f"Default delay set to {controller.delay_s:g} s")
            elif cmd in {"hold-period", "period"}:
                if len(args) != 1:
                    print("Usage: hold-period SECONDS")
                    continue
                value = float(args[0])
                if value <= 0:
                    print("Hold status period must be positive")
                    continue
                hold_period_s = value
                print(f"Hold status period set to {hold_period_s:g} s")
            elif cmd == "zero":
                controller.ramp_to(0.0)
            elif cmd == "off":
                controller.ramp_to(0.0)
                controller.output_off()
                print("Output OFF")
            elif cmd in {"quit", "exit", "q"}:
                controller.safe_shutdown(ramp_to_zero=not no_ramp_on_quit)
                return
            else:
                print(f"Unknown command: {cmd!r}. Type 'help'.")
        except KeyboardInterrupt:
            print("\nHold/command interrupted. Output remains ON at current set voltage. Type 'quit' to safely ramp down and exit.")
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR: {exc}", file=sys.stderr)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Interactive Keithley 2400 voltage ramp controller")
    parser.add_argument("--device", default=DEFAULT_DEVICE, help=f"Keithley device path. Default: {DEFAULT_DEVICE}")
    parser.add_argument("--baudrate", type=int, default=9600, help="Serial baudrate. Default: 9600")
    parser.add_argument("--timeout", type=float, default=1.0, help="I/O timeout in seconds. Default: 1")
    parser.add_argument("--current-limit", type=float, default=DEFAULT_CURRENT_LIMIT, help="Current compliance [A]. Default: 100e-6")
    parser.add_argument("--step", type=float, default=DEFAULT_STEP_V, help="Default ramp step size [V]. Default: 5")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY_S, help="Default delay per ramp step [s]. Default: 1")
    parser.add_argument("--max-abs-voltage", type=float, default=DEFAULT_MAX_ABS_VOLTAGE, help="Refuse commands above this absolute voltage [V]. Default: 250")
    parser.add_argument("--assume-start-voltage", type=float, default=0.0, help="Initial internal set-voltage assumption [V]. Default: 0")
    parser.add_argument("--target", type=float, default=None, help="Optional initial target voltage to ramp to before interactive mode, then hold indefinitely")
    parser.add_argument("--hold-period", type=float, default=DEFAULT_HOLD_STATUS_PERIOD_S, help="Status print period while holding at target [s]. Default: 5")
    parser.add_argument("--no-ramp-on-quit", action="store_true", help="Do not ramp to 0 V on quit/Ctrl-C; only turn output OFF. Not recommended.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    controller = Keithley2400RampController(
        device=args.device,
        baudrate=args.baudrate,
        timeout=args.timeout,
        current_limit=args.current_limit,
        step_v=args.step,
        delay_s=args.delay,
        max_abs_voltage=args.max_abs_voltage,
        assume_start_voltage=args.assume_start_voltage,
    )

    try:
        controller.open()
        if args.target is not None:
            try:
                controller.ramp_to(args.target)
                controller.hold_forever(period_s=args.hold_period)
            except KeyboardInterrupt:
                print("\nInitial target hold interrupted. Entering interactive prompt.")
        interactive_loop(
            controller,
            no_ramp_on_quit=args.no_ramp_on_quit,
            hold_period_s=args.hold_period,
        )
        return 0
    except KeyboardInterrupt:
        print("\nCtrl-C received.")
        try:
            controller.safe_shutdown(ramp_to_zero=not args.no_ramp_on_quit)
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR during shutdown: {exc}", file=sys.stderr)
            return 2
        return 130
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        try:
            controller.safe_shutdown(ramp_to_zero=not args.no_ramp_on_quit)
        except Exception as shutdown_exc:  # noqa: BLE001
            print(f"ERROR during shutdown: {shutdown_exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
