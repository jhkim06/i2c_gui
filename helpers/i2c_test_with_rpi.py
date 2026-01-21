import i2c_gui2_helpers as helpers
import datetime
import numpy as np
from tqdm import tqdm

chip_names = ["test"]
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
i2c_conn.save_baselines(hist_dir='/home/ellie/ETL/i2c_gui/helpers/output', save_notes=f'{now}')
