#!/usr/bin/env python3
"""CAEN NDT/N1470 USB-serial support for ETROC bias scans.

The CAEN NDT1470 appears on Linux as a USB CDC ACM serial device.  This module
implements the same small adapter surface used by ``smu_etroc_calibration_loop``
as the Keithley driver:

    configure_voltage(voltage, current_limit)
    output_on()
    output_off()
    read_current()
    drain_errors()
    close()

Notes on units:
  - The calibration loop uses volts and amps.
  - CAEN ASCII ``VSET`` is sent in volts.
  - CAEN ASCII ``ISET`` is sent in microamps, so this adapter converts A -> µA.
  - CAEN ``IMON`` is read in microamps, so this adapter converts µA -> A.

For NDT1470 channels configured for negative polarity, ``VSET`` is still sent as
voltage magnitude by default.  The requested signed voltage is preserved by the
caller in logs/CSV as ``applied_voltage_V``.
"""

from __future__ import annotations

from dataclasses import dataclass


DEFAULT_DEVICE = "/dev/serial/by-id/usb-CAEN_SPA_NIM_Desktop_HV_Power_Supply-if00"
DEFAULT_BAUDRATE = 9600
DEFAULT_BOARD = 0
DEFAULT_CHANNEL = 3


class CAENProtocolError(RuntimeError):
    """Raised when the CAEN supply returns an unexpected response."""


@dataclass(frozen=True)
class CAENResponse:
    raw: str
    fields: dict[str, str]

    @property
    def ok(self) -> bool:
        return self.fields.get("CMD", "").upper() == "OK"

    @property
    def value(self) -> str | None:
        return self.fields.get("VAL")


class CAENNDT1470:
    """Line-oriented CAEN NDT/N1470 ASCII protocol wrapper."""

    def __init__(
        self,
        device: str = DEFAULT_DEVICE,
        *,
        board: int = DEFAULT_BOARD,
        baudrate: int = DEFAULT_BAUDRATE,
        timeout: float = 1.0,
    ):
        self.device = device
        self.board = int(board)
        self.baudrate = int(baudrate)
        self.timeout = float(timeout)
        self._serial = None

    def __enter__(self) -> "CAENNDT1470":
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def open(self) -> None:
        import serial

        self._serial = serial.Serial(self.device, self.baudrate, timeout=self.timeout)
        self._serial.reset_input_buffer()
        self._serial.reset_output_buffer()

    def close(self) -> None:
        if self._serial is not None:
            self._serial.close()
            self._serial = None

    def write_command(self, command: str) -> None:
        if self._serial is None:
            raise RuntimeError("CAEN device is not open")
        self._serial.write(command.encode("ascii"))

    def read_response(self) -> CAENResponse:
        if self._serial is None:
            raise RuntimeError("CAEN device is not open")
        raw_bytes = self._serial.readline()
        raw = raw_bytes.decode("ascii", errors="replace").strip()
        if not raw:
            raise CAENProtocolError("CAEN did not return a response before timeout")
        return parse_response(raw)

    def query(self, command: str) -> CAENResponse:
        self.write_command(command)
        return self.read_response()

    def _command_prefix(self) -> str:
        return f"$BD:{self.board:02d}"

    def set_param(self, channel: int, parameter: str, value: float | int | str | None = None) -> CAENResponse:
        parameter = parameter.upper()
        command = f"{self._command_prefix()},CMD:SET,CH:{int(channel)},PAR:{parameter}"
        if value is not None:
            command += f",VAL:{value}"
        command += "\r\n"
        response = self.query(command)
        require_ok(response, command)
        return response

    def monitor_param(self, channel: int, parameter: str) -> CAENResponse:
        parameter = parameter.upper()
        command = f"{self._command_prefix()},CMD:MON,CH:{int(channel)},PAR:{parameter}\r\n"
        response = self.query(command)
        require_ok(response, command)
        return response

    def read_float_param(self, channel: int, parameter: str) -> tuple[float, str]:
        response = self.monitor_param(channel, parameter)
        if response.value is None:
            raise CAENProtocolError(f"CAEN response has no VAL field: {response.raw!r}")
        try:
            return float(response.value), response.raw
        except ValueError as exc:
            raise CAENProtocolError(f"CAEN VAL is not a float in response: {response.raw!r}") from exc


def parse_response(raw: str) -> CAENResponse:
    text = raw.strip()
    if text.startswith("#") or text.startswith("$"):
        text = text[1:]
    fields: dict[str, str] = {}
    for part in text.split(","):
        if ":" not in part:
            continue
        key, value = part.split(":", 1)
        fields[key.strip().upper()] = value.strip()
    return CAENResponse(raw=raw, fields=fields)


def require_ok(response: CAENResponse, command: str) -> None:
    if not response.ok:
        raise CAENProtocolError(
            f"CAEN command failed or returned unexpected response. "
            f"command={command.strip()!r}, response={response.raw!r}"
        )


class CAENBiasSupply:
    """CAEN adapter used by the ETROC calibration loop."""

    name = "caen"

    def __init__(
        self,
        device: str = DEFAULT_DEVICE,
        *,
        channel: int = DEFAULT_CHANNEL,
        board: int = DEFAULT_BOARD,
        baudrate: int = DEFAULT_BAUDRATE,
        timeout: float = 1.0,
        voltage_magnitude: bool = True,
    ):
        self.device = device
        self.channel = int(channel)
        self.board = int(board)
        self.baudrate = int(baudrate)
        self.timeout = float(timeout)
        self.voltage_magnitude = bool(voltage_magnitude)
        self.caen = CAENNDT1470(
            device,
            board=self.board,
            baudrate=self.baudrate,
            timeout=self.timeout,
        )
        self.idn = f"CAEN NDT1470 board={self.board} channel={self.channel} device={self.device}"

    def open(self) -> None:
        self.caen.open()
        # Read-only communication check.  If this fails, the caller can fall
        # back to no-SMU mode or abort with --require-smu.
        vset, raw = self.caen.read_float_param(self.channel, "VSET")
        self.idn += f" VSET={vset:g}V raw={raw}"

    def close(self) -> None:
        self.caen.close()

    def configure_voltage(self, voltage: float, current_limit: float) -> None:
        vset = abs(float(voltage)) if self.voltage_magnitude else float(voltage)
        iset_uA = float(current_limit) * 1e6
        if iset_uA <= 0:
            raise ValueError("CAEN current_limit must be positive")
        self.caen.set_param(self.channel, "VSET", f"{vset:.1f}")
        self.caen.set_param(self.channel, "ISET", f"{iset_uA:.2f}")

    def output_on(self) -> None:
        self.caen.set_param(self.channel, "ON")

    def output_off(self) -> None:
        self.caen.set_param(self.channel, "OFF")

    def read_current(self) -> tuple[float | None, str]:
        imon_uA, raw = self.caen.read_float_param(self.channel, "IMON")
        return imon_uA * 1e-6, raw

    def drain_errors(self, max_reads: int = 10) -> list[str]:
        # The simple NDT1470 ASCII protocol used here reports command status in
        # each response; it does not expose a Keithley-style error queue.
        return []


def connect_caen(
    device: str = DEFAULT_DEVICE,
    *,
    channel: int = DEFAULT_CHANNEL,
    board: int = DEFAULT_BOARD,
    baudrate: int = DEFAULT_BAUDRATE,
    timeout: float = 1.0,
    voltage_magnitude: bool = True,
) -> CAENBiasSupply:
    """Open CAEN supply and perform a read-only channel communication check."""
    supply = CAENBiasSupply(
        device,
        channel=channel,
        board=board,
        baudrate=baudrate,
        timeout=timeout,
        voltage_magnitude=voltage_magnitude,
    )
    try:
        supply.open()
        return supply
    except Exception:
        supply.close()
        raise
