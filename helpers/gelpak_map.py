"""Read-only Gel-Pak registry lookup and terminal-friendly slot formatting."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import urlopen

from sample_naming import canonical_sample_name


REGISTRY_URL = "https://gpm-etl.app.cern.ch"


@dataclass(frozen=True)
class GelPakMap:
    identifier: str
    name: str
    slots: tuple[str | None, ...]

    def slot_lines(self, selected_sample: str = "") -> list[tuple[str, bool]]:
        """Return A1–C3 labels and whether each slot matches the sample."""
        lines = []
        try:
            selected_name = canonical_sample_name(selected_sample)
        except ValueError:
            selected_name = selected_sample.casefold()
        for position, sample in enumerate(self.slots):
            coordinate = f"{'ABC'[position // 3]}{position % 3 + 1}"
            try:
                sample_name = canonical_sample_name(sample or "")
            except ValueError:
                sample_name = (sample or "").casefold()
            selected = bool(sample and selected_sample and sample_name == selected_name)
            display_name = sample.split(":", 1)[0].strip() if sample else "— empty —"
            lines.append((f"{coordinate}  {display_name}", selected))
        return lines

    def grid_rows(self, selected_sample: str = "") -> list[list[tuple[str, bool]]]:
        """Group A1–A3, B1–B3, C1–C3 as on the physical carrier."""
        slots = self.slot_lines(selected_sample)
        return [slots[start:start + 3] for start in (0, 3, 6)]


def fetch_gelpak(identifier: str, timeout: float = 5.0) -> GelPakMap:
    """Fetch one public map. This function issues GET only and uses no login."""
    identifier = identifier.strip()
    if not re.fullmatch(r"gp-[a-z0-9][a-z0-9-]{0,60}", identifier):
        raise ValueError("Enter a Gel-Pak ID such as gp-123")
    url = f"{REGISTRY_URL}/api/gelpaks/{quote(identifier, safe='')}"
    try:
        with urlopen(url, timeout=timeout) as response:
            data = json.load(response)
    except HTTPError as exc:
        if exc.code == 404:
            raise ValueError(f"Gel-Pak {identifier!r} was not found") from exc
        raise RuntimeError(f"Gel-Pak lookup failed (HTTP {exc.code})") from exc
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Gel-Pak lookup unavailable: {exc}") from exc

    if not isinstance(data, dict) or data.get("id") != identifier:
        raise ValueError("Gel-Pak response has an unexpected ID")
    if data.get("deleted_at"):
        raise ValueError(f"Gel-Pak {identifier!r} is in the registry trash")
    raw_slots = data.get("slots")
    if not isinstance(raw_slots, list) or len(raw_slots) != 9:
        raise ValueError("Gel-Pak response does not contain nine slots")
    slots: list[str | None] = [None] * 9
    seen: set[int] = set()
    for slot in raw_slots:
        if not isinstance(slot, dict) or type(slot.get("position")) is not int:
            raise ValueError("Gel-Pak response contains an invalid position")
        position = slot["position"]
        sample = slot.get("chip_id")
        if position not in range(9) or position in seen or not (sample is None or isinstance(sample, str)):
            raise ValueError("Gel-Pak response contains an invalid slot")
        seen.add(position)
        slots[position] = sample or None
    name = data.get("name")
    return GelPakMap(identifier, name if isinstance(name, str) else "", tuple(slots))
