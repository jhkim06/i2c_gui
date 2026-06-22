#!/usr/bin/env python3
"""Keithley USBTMC support for ETROC bias scans.

This module intentionally contains only the Keithley-specific SCPI/USBTMC
logic.  Higher-level calibration scripts can import ``KeithleyBiasSupply`` or
``connect_keithley`` and later swap in another supply implementation (for
example CAEN) with the same small method surface.
"""

from __future__ import annotations

import os
import time


DEFAULT_DEVICE = "/dev/usbtmc0"


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


class KeithleyBiasSupply:
    """Small adapter used by calibration loops.

    The adapter keeps Keithley SCPI details out of the auto-calibration script.
    Future CAEN support can provide the same methods: ``configure_voltage``,
    ``output_on``, ``output_off``, ``read_current``, ``drain_errors``, and
    ``close``.
    """

    name = "keithley"

    def __init__(self, device: str = DEFAULT_DEVICE):
        self.device = device
        self.smu = KeithleyUSBTMC(device)
        self.idn = ""

    def open(self) -> None:
        self.smu.open()
        self.idn = self.smu.query("*IDN?")
        if not self.idn:
            raise RuntimeError("Keithley opened but did not respond to *IDN?")

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


def connect_keithley(device: str = DEFAULT_DEVICE) -> KeithleyBiasSupply:
    """Open the Keithley and query *IDN? so connection problems are caught early."""
    supply = KeithleyBiasSupply(device)
    try:
        supply.open()
        return supply
    except Exception:
        supply.close()
        raise
