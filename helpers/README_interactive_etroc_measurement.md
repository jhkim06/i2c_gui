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
at the sample-name prompt to exit.
In the terminal UI, Up/Down at the sample-name prompt recalls names from the
current run; typing a prefix first searches matching names. Hybrid and Bare
ETROC histories are separate, and `q` is not stored.

The default PSU device is auto-detected; use
`--psu-device /dev/usbtmc0` if necessary. The E36312A CH1/CH2 presets come from
`keysight_e36312a_interactive.py` (currently 1.2 V/0.6 A and 1.0 V/0.5 A).
They are printed at startup. For a session-specific override, use for example
`--ch1-current-limit 1.2`. The workflow turns **all three** PSU outputs off
during contact changes and on exit; it turns on only CH1/CH2 after contact.

In an interactive terminal, a text UI keeps the sample, probe/BV contact,
PSU, and ETROC I²C status fixed at the top. Confirmed inactive states
(not contacted, PSU off, I²C not checked/detected) are green; contacted,
PSU on, and I²C detected states are red. Unknown/checking states stay neutral.
Calibration/IV messages scroll in the middle, and the current menu stays
at the bottom. Child scans run with their output streamed into that message
area with Python output unbuffered, so IV progress appears during the scan.
Ctrl-C is forwarded to the child, and the workflow waits for its shutdown
before returning. Use `--plain` for the original scrolling prompts (also used
automatically when stdin/stdout is not a terminal). The separate X11 plot
windows still open outside the terminal UI when a display is available.

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

Auto-calibration calls `i2c_test_with_rpi.py` with the optional
10-consecutive-ScanDone-timeout cutoff enabled. It passes operator, sample type,
contact trials, and contact state through `--plot-footer-metadata`, placing that
text once at the bottom center of each BL/NW 2D and 1D figure. It does not pass
`--save_notes`; the calibration script retains its existing notes, plot-title,
and SQLite behavior. BV-menu calibration starts without an additional bias
confirmation prompt; its plot footer labels the contacted-tip state as `BV=0 V`.

IV calls `smu_etroc_calibration_loop.py` with
`iv_configs/experiment_smu_voltage_scan_keithley_2400.json`.
The workflow does not pass `--save-notes` to the IV script; the config supplies
`Keithley 2400`. Each BL/NW plot title adds the step bias (for example,
`Keithley 2400 BV=-100V`), while `--plot-footer-metadata` adds operator and
contact information once at the bottom, identifying this run as a bias sweep.
This config also performs ETROC auto-calibration at **−100 V**. The existing IV
script now defers BL/NW PNG rendering until after its bias shutdown; SQLite
rows and a recoverable plot-job file are saved during calibration. If rendering
fails, the job remains in `helpers/output/deferred_bl_nw_plots/` and can be
retried with `python render_deferred_bl_nw_plots.py JOB.json`. Pass
`--no-defer-bl-nw-plots` directly to the IV script to restore immediate PNG
saving. `--no-show-plots` still only controls X-window display.
If bias shutdown fails, this wrapper stops and asks
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
