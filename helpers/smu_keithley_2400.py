#!/usr/bin/env python3
"""Keithley 2400 serial/USBTMC support for ETROC bias scans.

This module keeps Keithley 2400-specific SCPI separate from the existing
``smu_keithley.py`` helper, which is used for the Keithley 2470 setup.  It
exposes the same adapter surface expected by ``smu_etroc_calibration_loop.py``.

Most Keithley 2400 units expose RS-232 through a USB-serial adapter, e.g.
``/dev/ttyUSB0``.  A USBTMC-style path such as ``/dev/usbtmc0`` is also kept
supported for unusual adapter setups.
"""

from __future__ import annotations

import os
import time


DEFAULT_DEVICE = "/dev/ttyUSB0"
DEFAULT_BAUDRATE = 9600
DEFAULT_TIMEOUT = 1.0


class Keithley2400IO:
    """Small line-oriented SCPI wrapper for Keithley 2400 serial or USBTMC I/O."""

    def __init__(
        self,
        device: str = DEFAULT_DEVICE,
        *,
        baudrate: int = DEFAULT_BAUDRATE,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        self.device = device
        self.baudrate = baudrate
        self.timeout = timeout
        self._fh = None
        self._serial = None

    def __enter__(self) -> "Keithley2400IO":
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    @property
    def is_serial(self) -> bool:
        return os.path.basename(self.device).startswith(("ttyUSB", "ttyACM", "ttyS"))

    def open(self) -> None:
        if not os.path.exists(self.device):
            raise FileNotFoundError(
                f"{self.device} not found. Is the Keithley 2400 connected and powered on?"
            )

        if self.is_serial:
            try:
                import serial
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError("pyserial is required for Keithley 2400 RS-232/ttyUSB control") from exc

            self._serial = serial.Serial(
                self.device,
                baudrate=self.baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=self.timeout,
                write_timeout=self.timeout,
                xonxoff=False,
                rtscts=False,
                dsrdtr=False,
            )
            # Clear any stale bytes from a previous session.
            self._serial.reset_input_buffer()
            self._serial.reset_output_buffer()
        else:
            self._fh = open(self.device, "r+b", buffering=0)

    def close(self) -> None:
        if self._serial is not None:
            self._serial.close()
            self._serial = None
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def write(self, cmd: str) -> None:
        if self._serial is not None:
            # Keithley 2400 RS-232 accepts CR/LF termination; CR is the common
            # RS-232 terminator and LF keeps behavior compatible on adapters.
            self._serial.write((cmd.rstrip() + "\r\n").encode("ascii"))
            self._serial.flush()
            return
        if self._fh is not None:
            self._fh.write((cmd.rstrip() + "\n").encode("ascii"))
            return
        raise RuntimeError("Keithley 2400 device is not open")

    def read(self, max_bytes: int = 4096) -> str:
        if self._serial is not None:
            # Keithley 2400 RS-232 commonly terminates replies with CR rather
            # than LF.  Read byte-by-byte so we stop promptly on either one;
            # using read_until(b"\n") can wait for the full timeout on CR-only
            # replies and make every current sample look ~1-2 s slower.
            data = bytearray()
            deadline = time.monotonic() + self.timeout
            while len(data) < max_bytes and time.monotonic() < deadline:
                byte = self._serial.read(1)
                if not byte:
                    if data:
                        break
                    continue
                data.extend(byte)
                if byte in {b"\r", b"\n"}:
                    break
            return bytes(data).decode(errors="replace").strip()
        if self._fh is not None:
            return self._fh.read(max_bytes).decode(errors="replace").strip()
        raise RuntimeError("Keithley 2400 device is not open")

    def query(self, cmd: str, delay_s: float = 0.05) -> str:
        self.write(cmd)
        time.sleep(delay_s)
        return self.read()


# Backward-compatible aliases matching the generic helper naming pattern.
Keithley2400USBTMC = Keithley2400IO
KeithleyUSBTMC = Keithley2400IO


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


def parse_current_from_2400_read(response: str) -> float | None:
    """Parse current from a Keithley 2400 READ?/MEAS? comma response.

    A typical 2400 reading when sourcing voltage returns fields like
    voltage,current,resistance,time,status.  Fall back to the first float for
    one-value responses.
    """
    values: list[float] = []
    for token in response.replace(";", ",").split(","):
        token = token.strip()
        if not token:
            continue
        try:
            values.append(float(token))
        except ValueError:
            continue
    if len(values) >= 2:
        return values[1]
    if values:
        return values[0]
    return None


def drain_error_queue(smu: Keithley2400IO, max_reads: int = 10) -> list[str]:
    errors: list[str] = []
    for _ in range(max_reads):
        err = smu.query(":SYST:ERR?", delay_s=0.1)
        if err.startswith("0") or "No error" in err:
            break
        errors.append(err)
    return errors


def configure_voltage_source(smu: Keithley2400IO, voltage: float, current_limit: float) -> None:
    # Keithley 2400 SourceMeter SCPI: source voltage, measure current, set
    # current compliance/protection with SENS:CURR:PROT.
    smu.write(":OUTP OFF")
    smu.write(":SOUR:FUNC VOLT")
    smu.write(":SOUR:VOLT:MODE FIXED")
    smu.write(":SOUR:VOLT:RANG:AUTO ON")
    smu.write(f":SOUR:VOLT:LEV {voltage}")
    smu.write(":SENS:FUNC \"CURR\"")
    smu.write(":SENS:CURR:RANG:AUTO ON")
    smu.write(f":SENS:CURR:PROT {current_limit}")
    # Keep integration time short for scan speed.  Increase NPLC if you need
    # lower-noise current samples more than speed.
    smu.write(":SENS:CURR:NPLC 0.1")
    # Return only current to reduce serial payload and simplify parsing.
    smu.write(":FORM:ELEM CURR")


def read_current(smu: Keithley2400IO) -> tuple[float | None, str]:
    raw = smu.query(":READ?", delay_s=0.05)
    return parse_current_from_2400_read(raw), raw


class Keithley2400BiasSupply:
    """Small adapter used by calibration loops."""

    name = "keithley_2400"

    def __init__(self, device: str = DEFAULT_DEVICE):
        self.device = device
        self.smu = Keithley2400IO(device)
        self.idn = ""

    def open(self) -> None:
        self.smu.open()
        self.idn = self.smu.query("*IDN?")
        if not self.idn:
            raise RuntimeError("Keithley 2400 opened but did not respond to *IDN?")

    def close(self) -> None:
        self.smu.close()

    def configure_voltage(self, voltage: float, current_limit: float) -> None:
        configure_voltage_source(self.smu, voltage, current_limit)

    def output_on(self) -> None:
        self.smu.write(":OUTP ON")

    def output_off(self) -> None:
        self.smu.write(":OUTP OFF")

    def read_current(self) -> tuple[float | None, str]:
        return read_current(self.smu)

    def drain_errors(self, max_reads: int = 10) -> list[str]:
        return drain_error_queue(self.smu, max_reads=max_reads)


# Alias matching the generic Keithley helper API.
KeithleyBiasSupply = Keithley2400BiasSupply


def connect_keithley_2400(device: str = DEFAULT_DEVICE) -> Keithley2400BiasSupply:
    """Open the Keithley 2400 and query *IDN? so connection problems are caught early."""
    supply = Keithley2400BiasSupply(device)
    try:
        supply.open()
        return supply
    except Exception:
        supply.close()
        raise


# Alias so generic code can use the same connector name if this module is loaded directly.
def connect_keithley(device: str = DEFAULT_DEVICE) -> Keithley2400BiasSupply:
    return connect_keithley_2400(device)
