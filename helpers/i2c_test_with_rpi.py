import argparse
import i2c_gui2_helpers as helpers
import datetime
import os
from pathlib import Path
import subprocess
import sys
import numpy as np
from tqdm import tqdm
from keysight_e36312a_monitor import KeysightRailMonitor


# --------------------------
# Argument parser
# --------------------------
parser = argparse.ArgumentParser(description="ETROC calibration script")

parser.add_argument("--chip_name", type=str, default="test", help="Name of the chip")
parser.add_argument("--save_notes", type=str, default="", help="Extra notes for saving")
parser.add_argument("--plot-footer-metadata", type=str, default="",
                    help="Optional text shown once at the bottom center of each BL/NW figure; independent of --save_notes")
parser.add_argument("--measure-1v2", action="store_true", help="Record Keysight CH1 measured voltage/current for each calibrated pixel")
parser.add_argument(
    "--stop-after-10-scan-failures",
    action="store_true",
    help="Stop auto-calibration after 10 consecutive pixels whose ScanDone remains unset after five retries; do not save partial results",
)
parser.add_argument("--psu-device", type=str, help="USBTMC path for the E36312A (auto-detected if omitted)")
parser.add_argument(
    "--no-show-plots",
    action="store_true",
    help="Do not open the saved baseline/noise-width map (shown by default when a display is available)",
)
parser.add_argument("--defer-bl-nw-plots", action="store_true",
                    help="Save BL/NW rows to SQLite now and render plots later from a deferred job")
parser.add_argument(
    "--acc_scurve_pixels",
    type=str,
    default="",
    help="Optional ACC S-curve scan after auto-calibration. Format: 'row,col;row,col' or 'all'. Example: '3,3;8,8'.",
)
parser.add_argument("--acc_scurve_half_range", type=int, default=40, help="DAC range around BL for ACC S-curve: BL +/- this value")
parser.add_argument("--acc_scurve_step", type=int, default=1, help="DAC step for ACC S-curve scan")
parser.add_argument(
    "--power_mode",
    type=str,
    default="high",
    choices=["low", "010", "101", "high"],
    help="Pixel power mode used when disabling/configuring pixels before auto-calibration. Use 'low' to keep ETROC in low power mode.",
)
args = parser.parse_args()
if args.psu_device and not args.measure_1v2:
    parser.error("--psu-device requires --measure-1v2")
if args.defer_bl_nw_plots and not args.no_show_plots:
    parser.error("--defer-bl-nw-plots requires --no-show-plots")

try:
    _, output_dir, _ = helpers.measurement_output_paths()
except ValueError as exc:
    parser.error(str(exc))


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
rail_monitor = KeysightRailMonitor(channel=1, device=args.psu_device) if args.measure_1v2 else None
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

print(f'Run auto BL and NW calibration with pixel power_mode={args.power_mode}')
i2c_conn.config_chips(
    do_pixel_check=False,
    do_basic_peripheral_register_check=False, ### Need to re-visit
    do_disable_all_pixels=False,
    do_auto_calibration=False,
    do_disable_and_calibration=True,
    do_prepare_ws_testing=False,
    power_mode=args.power_mode,
    rail_monitor=rail_monitor,
    max_consecutive_scan_failures=10 if args.stop_after_10_scan_failures else None,
)

### Save BL and NW
now = datetime.datetime.now().isoformat(sep=' ', timespec='seconds')
full_notes = f"{args.save_notes}" if args.save_notes else now

preview_paths = i2c_conn.save_baselines(hist_dir=output_dir, save_notes=full_notes,
                                       preview_plots=not args.no_show_plots,
                                       plot_footer_metadata=args.plot_footer_metadata,
                                       defer_plots=args.defer_bl_nw_plots)

if not args.no_show_plots:
    if os.environ.get("DISPLAY") or os.environ.get("TMUX"):
        for preview_path in preview_paths:
            subprocess.Popen(
                [sys.executable, str(Path(__file__).with_name("show_png_x11.py")), str(preview_path)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                start_new_session=True,
            )
            print(f"Opening baseline/noise-width preview: {preview_path}")
    else:
        print("No graphical display is available; saved baseline/noise-width plots without opening a window.")

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
        hist_dir=output_dir,
        save_notes=full_notes,
    )
