#!/usr/bin/env python3
"""Read and print the ETROC2 eFuse chip ID.

This is a small script version of the eFuse read helpers in
``/home/ellie/ETL/i2c_gui/efuse_test.ipynb``.

Example:
    python3 helpers/print_efuse_chip_id.py --port /dev/ttyACM4 --chip-address 0x60

For Raspberry Pi I2C helper instead of USB-ISS:
    python3 helpers/print_efuse_chip_id.py --rpi-i2c --chip-address 0x60
"""

import argparse

import i2c_gui2_helpers as helpers


CHIP_ID_MASK_17BIT = 0x0001FFFF


def parse_int(value: str) -> int:
    """Parse decimal/hex/binary CLI integers, e.g. 96, 0x60, 0b1100000."""
    return int(value, 0)


def setup_efuse_read_mode(chip) -> None:
    """Configure eFuse controller for read mode, following efuse_test.ipynb."""
    peri_config = {
        "EFuse_EnClk": 0b1,
        "EFuse_Bypass": 0b0,
        "EFuse_Rstn": 0b1,
        "EFuse_Start": 0b0,
        "EFuse_Mode": 0b10,  # read mode; programming mode is 0b01
        "EFuse_TCKHP": 0x4,
    }

    chip.read_all_block("ETROC2", "Peripheral Config")
    for key, value in peri_config.items():
        chip.set_decoded_value("ETROC2", "Peripheral Config", key, value)
    chip.write_all_block("ETROC2", "Peripheral Config")


def reset_efuse_controller(chip) -> None:
    """Pulse EFuse_Rstn low then high, as in efuse_test.ipynb."""
    chip.read_decoded_value("ETROC2", "Peripheral Config", "EFuse_Rstn")
    chip.set_decoded_value("ETROC2", "Peripheral Config", "EFuse_Rstn", 0b0)
    chip.write_decoded_value("ETROC2", "Peripheral Config", "EFuse_Rstn")
    chip.set_decoded_value("ETROC2", "Peripheral Config", "EFuse_Rstn", 0b1)
    chip.write_decoded_value("ETROC2", "Peripheral Config", "EFuse_Rstn")


def read_efuse_values(chip) -> tuple[int, int]:
    """Return (EFuse_Prog, EFuseQ)."""
    chip.read_decoded_value("ETROC2", "Peripheral Config", "EFuse_Prog")
    efuse_prog = chip.get_decoded_value("ETROC2", "Peripheral Config", "EFuse_Prog")

    chip.read_decoded_value("ETROC2", "Peripheral Status", "EFuseQ")
    efuse_q = chip.get_decoded_value("ETROC2", "Peripheral Status", "EFuseQ")

    return efuse_prog, efuse_q


def main() -> None:
    parser = argparse.ArgumentParser(description="Print ETROC2 chip ID from eFuse.")
    parser.add_argument("--port", default="/dev/ttyACM4", help="USB-ISS serial port, default: /dev/ttyACM4")
    parser.add_argument("--chip-address", type=parse_int, default=0x60, help="ETROC I2C address, default: 0x60")
    parser.add_argument("--ws-address", type=parse_int, default=None, help="Optional waveform sampler I2C address")
    parser.add_argument("--chip-name", default="ETROC2", help="Label used by helper connection")
    parser.add_argument("--rpi-i2c", action="store_true", help="Use Raspberry Pi I2C helper instead of USB-ISS")
    parser.add_argument("--no-reset", action="store_true", help="Do not pulse EFuse_Rstn before reading")
    args = parser.parse_args()

    i2c_conn = helpers.i2c_connection(
        port=args.port,
        chip_addresses=[args.chip_address],
        ws_addresses=[args.ws_address],
        chip_names=[args.chip_name],
        use_usb_iss=not args.rpi_i2c,
    )

    chip = i2c_conn.get_chip_i2c_connection(args.chip_address, args.ws_address)

    setup_efuse_read_mode(chip)
    if not args.no_reset:
        reset_efuse_controller(chip)

    efuse_prog, efuse_q = read_efuse_values(chip)
    chip_id = efuse_q & CHIP_ID_MASK_17BIT

    print(f"Chip address : {args.chip_address:#04x}")
    print(f"EFuse_Prog   : {efuse_prog:#010x}  {efuse_prog:032b}")
    print(f"EFuseQ       : {efuse_q:#010x}  {efuse_q:032b}")
    print(f"Chip ID      : {chip_id:#07x}  {chip_id:017b}")


if __name__ == "__main__":
    main()
