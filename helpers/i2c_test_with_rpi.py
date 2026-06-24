import argparse
import i2c_gui2_helpers as helpers
import datetime
import numpy as np
from tqdm import tqdm


# --------------------------
# Argument parser
# --------------------------
parser = argparse.ArgumentParser(description="ETROC calibration script")

parser.add_argument("--chip_name", type=str, default="test", help="Name of the chip")
parser.add_argument("--save_notes", type=str, default="", help="Extra notes for saving")
parser.add_argument(
    "--acc_scurve_pixels",
    type=str,
    default="",
    help="Optional ACC S-curve scan after auto-calibration. Format: 'row,col;row,col' or 'all'. Example: '3,3;8,8'.",
)
parser.add_argument("--acc_scurve_half_range", type=int, default=40, help="DAC range around BL for ACC S-curve: BL +/- this value")
parser.add_argument("--acc_scurve_step", type=int, default=1, help="DAC step for ACC S-curve scan")
args = parser.parse_args()


def parse_pixel_list(pixel_string):
    if not pixel_string:
        return []
    if pixel_string.strip().lower() == "all":
        return [(row, col) for row in range(16) for col in range(16)]

    pixels = []
    for item in pixel_string.split(";"):
        item = item.strip()
        if not item:
            continue
        row_str, col_str = item.split(",")
        row, col = int(row_str), int(col_str)
        if not (0 <= row < 16 and 0 <= col < 16):
            raise ValueError(f"Pixel out of range: ({row},{col})")
        pixels.append((row, col))
    return pixels

chip_names = [args.chip_name]
port = "/dev/ttyACM0"
chip_addresses = [0x60]
ws_addresses = [None] * len(chip_addresses)
i2c_conn = helpers.i2c_connection(port,chip_addresses,ws_addresses,chip_names, use_usb_iss=False)

print('PLL and FC calibration')
# Calibrate PLL
for chip_address in chip_addresses[:]:
    i2c_conn.calibratePLL(chip_address, chip=None)
# Calibrate FC for all I2C
for chip_address in chip_addresses[:]:
    i2c_conn.asyResetGlobalReadout(chip_address, chip=None)
    i2c_conn.asyAlignFastcommand(chip_address, chip=None)

print('Run auto BL and NW calibration')
i2c_conn.config_chips(
    do_pixel_check=False,
    do_basic_peripheral_register_check=False, ### Need to re-visit
    do_disable_all_pixels=False,
    do_auto_calibration=False,
    do_disable_and_calibration=True,
    do_prepare_ws_testing=False
)

### Save BL and NW
now = datetime.datetime.now().isoformat(sep=' ', timespec='seconds')
full_notes = f"{args.save_notes}" if args.save_notes else now

i2c_conn.save_baselines(hist_dir='/home/ellie/ETL/i2c_gui/helpers/output', save_notes=full_notes)

acc_scurve_pixels = parse_pixel_list(args.acc_scurve_pixels)
if acc_scurve_pixels:
    print(f"Run ACC S-curve scan for {len(acc_scurve_pixels)} pixel(s)")
    acc_df = i2c_conn.scan_acc_scurves(
        chip_address=chip_addresses[0],
        pixel_list=acc_scurve_pixels,
        half_range=args.acc_scurve_half_range,
        step=args.acc_scurve_step,
    )
    i2c_conn.save_acc_scurves(
        acc_df,
        hist_dir='/home/ellie/ETL/i2c_gui/helpers/output',
        save_notes=full_notes,
    )
