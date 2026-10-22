"""Deterministic synthetic sample data for SAMPLE_MODE / --mock.

The data is SYNTHETIC. It is shaped like the sources WhyTrail queries through
Zetaris in live mode (NY_TAXI trip aggregates, NOAA GHCN-Daily Central Park
weather, a US federal holiday reference table) so the full agent loop runs
without credentials. It is not real NYC TLC or NOAA data.
"""

from __future__ import annotations

import random
from datetime import date, timedelta

BOROUGHS = {
    # borough: (weekday base trips, weekend base trips)
    "Manhattan": (90_000, 75_000),
    "Brooklyn": (12_000, 10_000),
    "Queens": (15_000, 12_500),
}

HOLIDAYS = {
    date(2024, 1, 1): "New Year's Day",
    date(2024, 1, 15): "Martin Luther King Jr. Day",
}

# obs_date: (prcp_mm, snow_mm, tmax_c, tmin_c)
WEATHER_OVERRIDES = {
    date(2024, 1, 9): (25.0, 0.0, 9.0, 3.0),
    date(2024, 1, 16): (12.0, 36.0, -1.0, -6.0),
    date(2024, 1, 19): (4.0, 10.0, 0.0, -5.0),
}

START = date(2024, 1, 1)
END = date(2024, 1, 31)
SEED = 20240101


def _days():
    d = START
    while d <= END:
        yield d
        d += timedelta(days=1)


def weather_rows() -> list[tuple]:
    rng = random.Random(SEED + 1)
    rows = []
    for d in _days():
        if d in WEATHER_OVERRIDES:
            prcp, snow, tmax, tmin = WEATHER_OVERRIDES[d]
        else:
            prcp, snow = 0.0, 0.0
            tmax = round(rng.uniform(2, 8), 1)
            tmin = round(tmax - rng.uniform(4, 7), 1)
        rows.append((d.isoformat(), prcp, snow, tmax, tmin))
    return rows


def holiday_rows() -> list[tuple]:
    return [(d.isoformat(), name) for d, name in sorted(HOLIDAYS.items())]


def taxi_rows() -> list[tuple]:
    """(trip_date, borough, trips, fare_total_usd)."""
    rng = random.Random(SEED)
    weather = {r[0]: r for r in weather_rows()}
    rows = []
    for d in _days():
        _, prcp, snow, _, _ = weather[d.isoformat()]
        for borough, (wk, we) in BOROUGHS.items():
            base = we if d.weekday() >= 5 else wk
            factor = 1.0
            if d in HOLIDAYS:
                factor *= 0.72
            if snow > 0:
                factor *= max(0.5, 1 - 0.006 * snow)
            elif prcp > 15:
                factor *= 1.03
            factor *= rng.uniform(0.985, 1.015)
            trips = int(round(base * factor))
            fare = round(trips * rng.uniform(19.3, 19.7), 2)
            rows.append((d.isoformat(), borough, trips, fare))
    return rows
