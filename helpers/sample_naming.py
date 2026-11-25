"""Canonical sample names for interactive ETROC measurements."""

from __future__ import annotations

import re


_INPUT_NAME = re.compile(r"W(?P<wafer>[0-9]{2})[-_](?P<number>[0-9]{1,3})", re.IGNORECASE)


def canonical_sample_name(value: str) -> str:
    """Map a Gel-Pak label or short name to W##_###.

    For example, W15-19:HPK-W64-12 and W15-19 both become W15_019.
    """
    prefix = value.split(":", 1)[0].strip()
    match = _INPUT_NAME.fullmatch(prefix)
    if match is None:
        raise ValueError("Use W##-number or W##_### (for example W15-19)")
    return f"W{match['wafer']}_{int(match['number']):03d}"
