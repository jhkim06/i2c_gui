# Interactive ETROC measurement

This terminal workflow guides one operator through repeated Bare ETROC or Hybrid
measurements. It calls the existing calibration and IV scripts; it does not
replace their scan logic.

## Start

On the Raspberry Pi with the probe hardware connected:

```bash
source ~/ETL/i2c_gui/helpers/setup_i2c_measurement_env.sh
python interactive_etroc_measurement.py
```

The program asks for the operator, sample type, and each sample name. Enter `q`
at the sample-name prompt to exit. The default PSU device is auto-detected; use
`--psu-device /dev/usbtmc0` if necessary. The E36312A CH1/CH2 presets come from
`keysight_e36312a_interactive.py` (currently 1.2 V/0.6 A and 1.0 V/0.5 A).
They are printed at startup. For a session-specific override, use for example
`--ch1-current-limit 1.2`. The workflow turns **all three** PSU outputs off
during contact changes and on exit; it turns on only CH1/CH2 after contact.

## Menus

- Probe contact: PSU off, display trial number, wait for `d` (done) or `q`
  (finish this sample). On `d`, enable CH1/CH2, run `i2cdetect -y 1`, and require
  `0x60`. A missing address switches the PSU off and repeats contact.
- Probe menu: `a` auto-calibration, `b` BV-tip contact, `u` un-contact probe.
  Un-contact waits for operator confirmation and returns to probe contact.
- BV contact: PSU off while positioning the tip, then enable CH1/CH2 and check
  I²C again. `q` cancels BV contact and returns to the probe menu after a fresh
  power-on/I²C check.
- BV menu: `a` auto-calibration, `i` IV scan, `b` un-contact BV tip only, `u`
  un-contact both BV tip and probe. Each un-contact action switches the PSU off
  and waits for confirmation. BV-tip only then restores power/checks I²C and
  returns to the probe menu; both returns to probe contact with power off.

Auto-calibration calls `i2c_test_with_rpi.py` with per-pixel PSU readback and
the optional 10-consecutive-ScanDone-timeout cutoff enabled. Its plots are saved
but are not opened over X11. Notes/plot titles include operator, sample type,
contact trial, and whether the BV tip is contacted. BV-menu calibration is
labelled `BV=0 V`.
Before each such calibration the operator must confirm that the Keithley bias
output is off and BV is 0 V; otherwise that calibration is skipped.

IV calls `smu_etroc_calibration_loop.py` with
`iv_configs/experiment_smu_voltage_scan_keithley_2400.json` **unchanged**.
This config also performs ETROC auto-calibration at **−100 V**. The existing IV
script handles bias output shutdown; if it fails, this wrapper stops and asks
the operator to verify that the Keithley output is off before continuing.

The controller only recognizes an exact `60` cell in the `i2cdetect` scan;
`UU` or an empty cell is reported as not confirmed. An I²C ACK shows that an
address responded, not that the device identity has been verified.

## Verification before hardware use

```bash
python -m unittest test_interactive_etroc_measurement.py -v
python interactive_etroc_measurement.py --help
```

These checks use fake instruments and do not operate the PSU, ETROC, or SMU.
