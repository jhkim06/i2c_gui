"""Read-only voltage/current snapshots from an E36312A output.

The interactive controller may stay open at its prompt during acquisition.
Do not issue interactive PSU commands while a sample is being read.
"""

from dataclasses import dataclass
from datetime import datetime
from math import isfinite

from keysight_e36312a_interactive import USBTMCInstrument, discover_device, require_channel


@dataclass(frozen=True)
class RailSample:
    timestamp: datetime
    voltage_v: float
    current_a: float


class KeysightRailMonitor:
    def __init__(self, channel: int = 1, device: str | None = None):
        require_channel(channel)
        self.channel = channel
        self.device = device or discover_device()
        if self.device is None:
            raise RuntimeError("No USBTMC power supply found")
        self.instrument = USBTMCInstrument(self.device)
        identity = self.instrument.query("*IDN?")
        if "E36312A" not in identity.upper():
            raise RuntimeError(f"Expected Keysight E36312A on {self.device}, got {identity!r}")

    def sample(self) -> RailSample:
        """Read the selected output without changing voltage, limit, or on/off state."""
        self.instrument.select_channel(self.channel)
        voltage = float(self.instrument.query("MEAS:VOLT?"))
        current = float(self.instrument.query("MEAS:CURR?"))
        timestamp = datetime.now()
        if not (isfinite(voltage) and isfinite(current)):
            raise ValueError("Power supply returned a non-finite measurement")
        return RailSample(timestamp, voltage, current)


def ensure_baseline_rail_columns(connection) -> None:
    """Extend an existing baseline table; leave historical rows unchanged."""
    existing = {column[1] for column in connection.execute('PRAGMA table_info(baselines)')}
    if not existing:
        return  # pandas creates the full table on the first save
    for column, kind in (
        ('rail_sample_time', 'TIMESTAMP'),
        ('rail_meas_voltage_V', 'REAL'),
        ('rail_meas_current_A', 'REAL'),
        ('rail_measurement_error', 'TEXT'),
    ):
        if column not in existing:
            connection.execute(f'ALTER TABLE baselines ADD COLUMN {column} {kind}')
