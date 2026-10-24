"""Build the two reference CSVs that WhyTrail registers in Zetaris as file sources.

    python scripts/zetaris/prep_reference_tables.py --noaa USW00094728.csv --out out/

Input: the official NOAA GHCN-Daily by-station file for Central Park (station USW00094728, CC0):
    https://noaa-ghcn-pds.s3.amazonaws.com/csv/by_station/USW00094728.csv
Output (one directory per table, so each can be registered as a CSV folder):
    out/central_park_daily/central_park_daily.csv   obs_date, prcp_mm, snow_mm, tmax_c, tmin_c  (one row per day)
    out/us_holidays/us_holidays.csv                 holiday_date, name                            (US federal holidays, OPM)

GHCN stores PRCP/TMAX/TMIN in tenths (mm / degrees C) and SNOW in mm; rows with a quality flag are dropped.
"""

from __future__ import annotations

import argparse
import collections
import csv
from pathlib import Path

# US federal holidays (OPM list), observed dates, 2024-2025.
US_HOLIDAYS = [
    ("2024-01-01", "New Year's Day"), ("2024-01-15", "Martin Luther King Jr. Day"), ("2024-02-19", "Washington's Birthday"),
    ("2024-05-27", "Memorial Day"), ("2024-06-19", "Juneteenth National Independence Day"), ("2024-07-04", "Independence Day"),
    ("2024-09-02", "Labor Day"), ("2024-10-14", "Columbus Day"), ("2024-11-11", "Veterans Day"), ("2024-11-28", "Thanksgiving Day"),
    ("2024-12-25", "Christmas Day"), ("2025-01-01", "New Year's Day"), ("2025-01-20", "Martin Luther King Jr. Day"),
    ("2025-02-17", "Washington's Birthday"), ("2025-05-26", "Memorial Day"), ("2025-06-19", "Juneteenth National Independence Day"),
    ("2025-07-04", "Independence Day"), ("2025-09-01", "Labor Day"), ("2025-10-13", "Columbus Day"), ("2025-11-11", "Veterans Day"),
    ("2025-11-27", "Thanksgiving Day"), ("2025-12-25", "Christmas Day"),
]


def pivot_noaa(path: Path, since: str = "20240101") -> list[list]:
    by_day: dict[str, dict[str, int]] = collections.defaultdict(dict)
    with path.open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["DATE"] >= since and r["ELEMENT"] in ("PRCP", "SNOW", "TMAX", "TMIN") and not r["Q_FLAG"]:
                by_day[r["DATE"]][r["ELEMENT"]] = int(r["DATA_VALUE"])
    rows = []
    for k in sorted(by_day):
        v = by_day[k]
        iso = f"{k[:4]}-{k[4:6]}-{k[6:]}"

        def scaled(element: str, factor: float):
            return "" if element not in v else round(v[element] * factor, 1)

        rows.append([iso, scaled("PRCP", 0.1), scaled("SNOW", 1.0), scaled("TMAX", 0.1), scaled("TMIN", 0.1)])
    return rows


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--noaa", required=True, type=Path, help="GHCN-Daily by-station CSV (USW00094728.csv)")
    p.add_argument("--out", default=Path("out"), type=Path)
    p.add_argument("--since", default="20240101", help="first day to keep, YYYYMMDD")
    a = p.parse_args()

    weather = pivot_noaa(a.noaa, a.since)
    d = a.out / "central_park_daily"
    d.mkdir(parents=True, exist_ok=True)
    with (d / "central_park_daily.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["obs_date", "prcp_mm", "snow_mm", "tmax_c", "tmin_c"])
        w.writerows(weather)
    print(f"central_park_daily: {len(weather)} days {weather[0][0]}..{weather[-1][0]} -> {d}")

    d = a.out / "us_holidays"
    d.mkdir(parents=True, exist_ok=True)
    with (d / "us_holidays.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["holiday_date", "name"])
        w.writerows(US_HOLIDAYS)
    print(f"us_holidays: {len(US_HOLIDAYS)} rows -> {d}")


if __name__ == "__main__":
    main()
