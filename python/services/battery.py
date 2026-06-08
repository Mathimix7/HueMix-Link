"""Shared battery helpers used across device views and services."""
from __future__ import annotations

from typing import Optional


def calculate_battery_percent(voltage_mv: int, battery_type: str = 'li_ion') -> Optional[int]:
    """Estimate battery percentage from voltage using chemistry-specific curves."""
    if voltage_mv == 0:
        return None

    normalized_type = (battery_type or 'li_ion').strip().lower()
    if normalized_type == 'cr123a':
        curve = [
            (2200, 0),
            (2400, 5),
            (2500, 10),
            (2600, 20),
            (2700, 35),
            (2800, 50),
            (2900, 65),
            (3000, 80),
            (3050, 92),
            (3100, 100),
        ]
    else:
        curve = [
            (3000, 0),
            (3300, 5),
            (3400, 10),
            (3500, 20),
            (3600, 35),
            (3700, 50),
            (3800, 70),
            (3900, 80),
            (4000, 90),
            (4100, 95),
            (4200, 100),
        ]

    if voltage_mv <= curve[0][0]:
        return 0
    if voltage_mv >= curve[-1][0]:
        return 100

    lower_v, lower_p = curve[0]
    for upper_v, upper_p in curve[1:]:
        if voltage_mv <= upper_v:
            if upper_v == lower_v:
                return int(upper_p)
            fraction = (voltage_mv - lower_v) / (upper_v - lower_v)
            percent = lower_p + fraction * (upper_p - lower_p)
            return max(0, min(100, int(round(percent))))
        lower_v, lower_p = upper_v, upper_p

    return 0