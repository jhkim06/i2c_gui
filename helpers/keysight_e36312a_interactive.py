#!/usr/bin/env python3
"""
Interactive controller for a Keysight E36312A triple-output DC power supply via Linux USBTMC.

Default device: auto-detect /dev/usbtmc* and prefer an instrument whose *IDN? contains E36312A.

Interactive commands:
  status                    Show output state, set V/I, and measured V/I for channels 1-3
  on <ch> [ch ...]          Apply presets, then turn one or more channels on
  off <ch> [ch ...]         Turn one or more channels off
  set <ch> <volt> <amp>     Change the preset for this session only
  presets                   Show current presets
  help                      Show commands
  quit                      Exit

Examples:
  on 1
  on 1 2
  on 1,2
  on 1, on 2
  off 1 2

Edit DEFAULT_PRESETS below for your usual setup.
"""

from __future__ import annotations

import atexit
import glob
import os
import readline
import sys
import time
from dataclasses import dataclass
from typing import Optional

# Your usual settings. Channel 3 is intentionally unset for safety.
DEFAULT_PRESETS = {
    1: (1.2, 0.6),
    2: (1.0, 0.5),
    3: None,  # example: (2.5, 0.2)
}

CHANNELS = (1, 2, 3)
READ_TIMEOUT_S = 2.0
HISTORY_PATH = os.path.expanduser("~/.keysight_e36312a_interactive_history")


class InstrumentError(RuntimeError):
    pass


@dataclass
class ChannelStatus:
    channel: int
    output_on: Optional[bool]
    set_voltage: Optional[float]
    set_current: Optional[float]
    meas_voltage: Optional[float]
    meas_current: Optional[float]


class USBTMCInstrument:
    def __init__(self, path: str, timeout_s: float = READ_TIMEOUT_S):
        self.path = path
        self.timeout_s = timeout_s

    def __enter__(self) -> "USBTMCInstrument":
        # Some Linux USBTMC setups, including this E36312A/RPi combination,
        # do not return query responses on a persistent O_RDWR file descriptor.
        # Keep the context-manager API, but open/close separately for each
        # write and read. This matches the working shell pattern:
        #   printf '*IDN?\n' > /dev/usbtmc0 ; cat /dev/usbtmc0
        return self

    def __exit__(self, exc_type, exc, tb):
        return None

    def write(self, cmd: str) -> None:
        with open(self.path, "wb", buffering=0) as f:
            f.write((cmd.rstrip() + "\n").encode("ascii"))

    def read(self) -> str:
        # Do not use select()/O_NONBLOCK here. On some Linux USBTMC drivers,
        # including the tested E36312A setup, select() can report no data even
        # though a normal blocking readline() returns the response correctly.
        with open(self.path, "rb", buffering=0) as f:
            return f.readline().decode(errors="replace").strip()

    def query(self, cmd: str) -> str:
        self.write(cmd)
        return self.read()

    def select_channel(self, ch: int) -> None:
        require_channel(ch)
        self.write(f"INST:NSEL {ch}")

    def query_float(self, cmd: str) -> Optional[float]:
        try:
            return float(self.query(cmd))
        except Exception:
            return None

    def get_channel_status(self, ch: int) -> ChannelStatus:
        self.select_channel(ch)
        time.sleep(0.03)

        output_on: Optional[bool]
        try:
            output_on = bool(int(float(self.query("OUTP?"))))
        except Exception:
            output_on = None

        set_voltage = self.query_float("VOLT?")
        set_current = self.query_float("CURR?")
        meas_voltage = self.query_float("MEAS:VOLT?")
        meas_current = self.query_float("MEAS:CURR?")

        return ChannelStatus(ch, output_on, set_voltage, set_current, meas_voltage, meas_current)

    def configure_channel(self, ch: int, voltage: float, current: float) -> None:
        require_channel(ch)
        if voltage < 0 or current < 0:
            raise ValueError("Voltage/current must be non-negative")
        self.select_channel(ch)
        self.write(f"VOLT {voltage:.6g}")
        self.write(f"CURR {current:.6g}")

    def output(self, ch: int, enabled: bool) -> None:
        self.output_channels([ch], enabled)

    def output_channels(self, channels: list[int], enabled: bool) -> None:
        channels = unique_channels(channels)
        state = "ON" if enabled else "OFF"
        if len(channels) == 1:
            self.select_channel(channels[0])
            self.write(f"OUTP {state}")
            return

        # Use the channel-list form so multiple outputs switch together.
        channel_list = ",".join(str(ch) for ch in channels)
        self.write(f"OUTP {state}, (@{channel_list})")


def require_channel(ch: int) -> None:
    if ch not in CHANNELS:
        raise ValueError("Channel must be 1, 2, or 3")


def unique_channels(channels: list[int]) -> list[int]:
    unique: list[int] = []
    for ch in channels:
        require_channel(ch)
        if ch not in unique:
            unique.append(ch)
    return unique


def setup_history() -> None:
    try:
        readline.read_history_file(HISTORY_PATH)
    except FileNotFoundError:
        pass
    except Exception:
        pass

    try:
        readline.set_history_length(1000)
    except Exception:
        pass

    def save_history() -> None:
        try:
            readline.write_history_file(HISTORY_PATH)
        except Exception:
            pass

    atexit.register(save_history)


def discover_device() -> Optional[str]:
    devices = sorted(glob.glob("/dev/usbtmc*"))
    if not devices:
        return None

    fallback = devices[0]
    for path in devices:
        try:
            with USBTMCInstrument(path, timeout_s=0.8) as inst:
                idn = inst.query("*IDN?")
            if "E36312A" in idn.upper():
                return path
        except Exception:
            continue
    return fallback


def fmt_float(value: Optional[float], unit: str) -> str:
    if value is None:
        return "?"
    return f"{value:.6g} {unit}"


def show_status(inst: USBTMCInstrument) -> None:
    print("\nChannel status:")
    print("CH  OUT  Set V      Set I      Meas V     Meas I")
    print("--  ---  ---------  ---------  ---------  ---------")
    for ch in CHANNELS:
        st = inst.get_channel_status(ch)
        out = "ON " if st.output_on is True else "OFF" if st.output_on is False else "?  "
        print(
            f"{ch:<2}  {out:<3}  "
            f"{fmt_float(st.set_voltage, 'V'):<9}  "
            f"{fmt_float(st.set_current, 'A'):<9}  "
            f"{fmt_float(st.meas_voltage, 'V'):<9}  "
            f"{fmt_float(st.meas_current, 'A'):<9}"
        )
    print()


def show_presets(presets) -> None:
    print("\nPresets used by 'on <ch>':")
    for ch in CHANNELS:
        preset = presets.get(ch)
        if preset is None:
            print(f"  CH{ch}: unset")
        else:
            voltage, current = preset
            print(f"  CH{ch}: {voltage:g} V, {current:g} A limit")
    print()


def print_help() -> None:
    print(__doc__.split("Interactive commands:", 1)[1].strip())


def parse_channel(token: str) -> int:
    ch = int(token)
    require_channel(ch)
    return ch


def parse_channels(tokens: list[str], command: str) -> list[int]:
    channels: list[int] = []
    for token in tokens:
        token = token.strip().lower().strip(",;")
        if not token or token == command:
            continue
        for part in token.split(","):
            part = part.strip()
            if not part:
                continue
            channels.append(parse_channel(part))

    if not channels:
        raise ValueError(f"Usage: {command} <ch> [ch ...]")
    return unique_channels(channels)


def interactive_loop(inst: USBTMCInstrument) -> None:
    presets = dict(DEFAULT_PRESETS)
    setup_history()
    print("Connected. Type 'help' for commands. Use arrow keys for command history.")
    show_status(inst)
    show_presets(presets)

    while True:
        try:
            line = input("E36312A> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            return

        if not line:
            continue

        should_show_status = False
        try:
            # Allow multiple commands on one line, e.g. "on 1; off 2".
            for command_line in [item.strip() for item in line.split(";") if item.strip()]:
                parts = command_line.split()
                cmd = parts[0].lower().strip(",;")

                if cmd in {"quit", "exit", "q"}:
                    print("Bye.")
                    return
                if cmd in {"help", "h", "?"}:
                    print_help()
                elif cmd in {"status", "st"}:
                    should_show_status = True
                elif cmd == "presets":
                    show_presets(presets)
                elif cmd == "set":
                    if len(parts) != 4:
                        print("Usage: set <ch> <volt> <amp>")
                        continue
                    ch = parse_channel(parts[1])
                    voltage = float(parts[2])
                    current = float(parts[3])
                    if voltage < 0 or current < 0:
                        print("Voltage/current must be non-negative.")
                        continue
                    presets[ch] = (voltage, current)
                    print(f"Preset CH{ch} = {voltage:g} V, {current:g} A")
                    should_show_status = True
                elif cmd == "on":
                    channels = parse_channels(parts[1:], "on")
                    missing = [ch for ch in channels if presets.get(ch) is None]
                    if missing:
                        print(f"No preset for: {', '.join(f'CH{ch}' for ch in missing)}. Use: set <ch> <volt> <amp>")
                        continue

                    for ch in channels:
                        voltage, current = presets[ch]
                        inst.configure_channel(ch, voltage, current)
                    inst.output_channels(channels, True)
                    print(
                        "ON: "
                        + ", ".join(
                            f"CH{ch} at {presets[ch][0]:g} V with {presets[ch][1]:g} A current limit"
                            for ch in channels
                        )
                    )
                    should_show_status = True
                elif cmd == "off":
                    channels = parse_channels(parts[1:], "off")
                    inst.output_channels(channels, False)
                    print("OFF: " + ", ".join(f"CH{ch}" for ch in channels))
                    should_show_status = True
                else:
                    print("Unknown command. Type 'help'.")

            if should_show_status:
                show_status(inst)
        except PermissionError:
            print("Permission denied accessing USBTMC device. Try running with proper udev rules or sudo.")
        except Exception as exc:
            print(f"Error: {exc}")


def main() -> int:
    device = discover_device()
    if device is None:
        print("No USBTMC device found. Connect the Keysight E36312A and check /dev/usbtmc*.", file=sys.stderr)
        return 1

    print(f"Using {device}")
    try:
        with USBTMCInstrument(device) as inst:
            try:
                print(f"IDN: {inst.query('*IDN?')}")
            except Exception as exc:
                print(f"Warning: found {device}, but *IDN? failed: {exc}")
            interactive_loop(inst)
    except PermissionError:
        print(f"Permission denied opening {device}. Try sudo or add a udev rule for USBTMC access.", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"Failed to use {device}: {exc}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
