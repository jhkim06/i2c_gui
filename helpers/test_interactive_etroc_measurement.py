"""Hardware-free checks for the interactive measurement workflow."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from interactive_etroc_measurement import EtrocController, MeasurementWorkflow, PowerSupplyController, SessionMetadata  # noqa: E402


class FakePSU:
    def __init__(self) -> None:
        self.events: list[str] = []

    def on(self) -> None:
        self.events.append("on")

    def off(self) -> None:
        self.events.append("off")


class FakeETROC:
    def __init__(self, detected: bool = True) -> None:
        self.events: list[tuple] = []
        self.present = detected

    def detected(self) -> bool:
        self.events.append(("detect",))
        return self.present

    def calibrate(self, metadata: SessionMetadata, *, bv_contacted: bool) -> bool:
        self.events.append(("calibrate", metadata.probe_trial, metadata.bv_trial, bv_contacted))
        return True

    def iv_scan(self, metadata: SessionMetadata) -> bool:
        self.events.append(("iv", metadata.probe_trial, metadata.bv_trial))
        return True


class WorkflowTests(unittest.TestCase):
    def test_calibrations_iv_and_stepwise_uncontact(self) -> None:
        psu, etroc = FakePSU(), FakeETROC()
        answers = iter(("Alice", "h", "H1", "d", "a", "b", "d", "a", "y", "i", "b", "d", "u", "d", "q", "q"))
        MeasurementWorkflow(psu, etroc, lambda _: next(answers)).run()

        self.assertEqual([event for event in etroc.events if event[0] == "calibrate"],
                         [("calibrate", 1, 0, False), ("calibrate", 1, 1, True)])
        self.assertIn(("iv", 1, 1), etroc.events)
        self.assertEqual(psu.events, ["off", "on", "off", "on", "off", "on", "off", "off"])

    def test_uncontact_bv_and_probe_returns_before_probe(self) -> None:
        psu, etroc = FakePSU(), FakeETROC()
        answers = iter(("Alice", "b", "E1", "d", "b", "d", "u", "d", "q", "q"))
        MeasurementWorkflow(psu, etroc, lambda _: next(answers)).run()
        self.assertEqual(psu.events, ["off", "on", "off", "on", "off", "off"])
        self.assertEqual(sum(event == ("detect",) for event in etroc.events), 2)

    def test_failed_i2c_check_switches_psu_off_and_retries(self) -> None:
        psu, etroc = FakePSU(), FakeETROC(detected=False)
        answers = iter(("Alice", "b", "E1", "d", "q", "q"))
        MeasurementWorkflow(psu, etroc, lambda _: next(answers)).run()
        self.assertEqual(psu.events, ["off", "on", "off", "off"])

    def test_i2cdetect_accepts_only_exact_address(self) -> None:
        controller = EtrocController("/dev/usbtmc0", Path("iv.json"))
        with patch("interactive_etroc_measurement.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = "60: 60 -- -- -- -- -- -- -- -- -- -- -- -- -- -- --\n"
            self.assertTrue(controller.detected())
            run.return_value.stdout = "60: UU -- -- -- -- -- -- -- -- -- -- -- -- -- -- --\n"
            self.assertFalse(controller.detected())
            run.assert_called_with(["i2cdetect", "-y", "1"], capture_output=True,
                                   text=True, timeout=10, check=False)

    def test_script_arguments_include_metadata_and_iv_config(self) -> None:
        controller = EtrocController("/dev/usbtmc0", Path("/tmp/iv.json"))
        metadata = SessionMetadata("Alice", "Hybrid", "H1", 2, 1)
        with patch("interactive_etroc_measurement.subprocess.run") as run:
            run.return_value.returncode = 0
            self.assertTrue(controller.calibrate(metadata, bv_contacted=True))
            cal_command = run.call_args.args[0]
            self.assertIn("--measure-1v2", cal_command)
            self.assertIn("--stop-after-10-scan-failures", cal_command)
            self.assertIn("BV=0 V", cal_command[cal_command.index("--save_notes") + 1])
            self.assertTrue(controller.iv_scan(metadata))
            iv_command = run.call_args.args[0]
            self.assertEqual(iv_command[iv_command.index("--config") + 1], "/tmp/iv.json")
            self.assertIn("IV bias sweep", iv_command[iv_command.index("--save-notes") + 1])
            self.assertNotIn("BV=0 V", iv_command[iv_command.index("--save-notes") + 1])

    def test_psu_off_disables_and_checks_all_three_channels(self) -> None:
        with patch("interactive_etroc_measurement.USBTMCInstrument") as instrument_class:
            instrument = instrument_class.return_value
            instrument.query.return_value = "KEYSIGHT,E36312A,123,1"
            instrument.get_channel_status.return_value = SimpleNamespace(output_on=False)
            psu = PowerSupplyController("/dev/usbtmc0")
            psu.off()
            instrument.output_channels.assert_called_once_with([1, 2, 3], False)
            self.assertEqual([call.args[0] for call in instrument.get_channel_status.call_args_list],
                             [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
