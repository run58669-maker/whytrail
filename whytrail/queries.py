"""Restricted SQL templates.

Agents pick templates and parameters; they never send free-form SQL. Parameters are
validated (ISO dates, segment from the discovered segment list) before rendering, so
the same statement is safe to send to Zetaris Lightning SQL or the mock.
Every statement uses qualified SOURCE.table names; `daily_joined` is a federated
join across three Zetaris sources.
"""

from __future__ import annotations

import re

TAXI = "NY_TAXI.taxi_daily"
WEATHER = "NOAA_GHCN.central_park_daily"
HOLIDAYS = "REF.us_holidays"
TABLES = [TAXI, WEATHER, HOLIDAYS]

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SEG_RE = re.compile(r"^[A-Za-z][A-Za-z .'-]{0,40}$")

TEMPLATES = {
    "schema_probe": "SELECT * FROM {table} LIMIT 1",
    "segments": f"SELECT DISTINCT borough FROM {TAXI}",
    "metric_total": f"SELECT SUM(trips) AS trips FROM {TAXI} WHERE borough = '{{seg}}' AND trip_date BETWEEN '{{start}}' AND '{{end}}'",
    "data_range": f"SELECT MIN(trip_date) AS first_day, MAX(trip_date) AS last_day FROM {TAXI} WHERE borough = '{{seg}}'",
    "weather": f"SELECT SUM(prcp_mm) AS prcp_mm, SUM(snow_mm) AS snow_mm FROM {WEATHER} WHERE obs_date BETWEEN '{{start}}' AND '{{end}}'",
    "holiday": f"SELECT COUNT(*) AS holidays FROM {HOLIDAYS} WHERE holiday_date BETWEEN '{{start}}' AND '{{end}}'",
    "price": f"SELECT SUM(fare_total_usd) / SUM(trips) AS fare_per_trip FROM {TAXI} WHERE borough = '{{seg}}' AND trip_date BETWEEN '{{start}}' AND '{{end}}'",
    "coverage": f"SELECT COUNT(DISTINCT trip_date) AS days FROM {TAXI} WHERE borough = '{{seg}}' AND trip_date BETWEEN '{{start}}' AND '{{end}}'",
    "peers": f"SELECT SUM(trips) AS trips FROM {TAXI} WHERE borough <> '{{seg}}' AND trip_date BETWEEN '{{start}}' AND '{{end}}'",
    "daily_joined": (
        f"SELECT t.trip_date, t.trips, w.prcp_mm, w.snow_mm, h.name AS holiday "
        f"FROM {TAXI} t JOIN {WEATHER} w ON t.trip_date = w.obs_date "
        f"LEFT JOIN {HOLIDAYS} h ON t.trip_date = h.holiday_date "
        f"WHERE t.borough = '{{seg}}' AND t.trip_date BETWEEN '{{start}}' AND '{{end}}' ORDER BY t.trip_date"
    ),
    # Independent formulation used by the Verifier: raw rows, summed client-side.
    "verify_rows": f"SELECT trip_date, trips FROM {TAXI} WHERE borough = '{{seg}}' AND trip_date BETWEEN '{{start}}' AND '{{end}}'",
}


class TemplateError(ValueError):
    pass


def render(name: str, **params: str) -> str:
    if name not in TEMPLATES:
        raise TemplateError(f"unknown template {name}")
    for k in ("start", "end"):
        if k in params and not DATE_RE.match(params[k]):
            raise TemplateError(f"bad date {params[k]!r}")
    if "seg" in params and not SEG_RE.match(params["seg"]):
        raise TemplateError(f"bad segment {params['seg']!r}")
    if "table" in params and params["table"] not in TABLES:
        raise TemplateError(f"unknown table {params['table']!r}")
    return TEMPLATES[name].format(**params)


# Follow-up tests the Skeptic may request. The orchestrator executes only names from this catalog;
# an unknown name is logged and replaced by "isolate_daytype".
FOLLOWUP_TESTS = {
    "isolate_daytype": ("federated join NY_TAXI x NOAA_GHCN x REF for both periods; weekday-matched gap per day type "
                        "(holiday / snow / other). Answers: is the change concentrated on the days the hypothesis names?"),
    "baseline_variation": ("total for the period of the same length just before period A (template metric_total). "
                           "Answers: is the A->B change within ordinary period-to-period variation?"),
}

# Competing hypotheses the Planner may propose. Each names the template that can falsify it.
HYPOTHESES = {
    "H_weather": {"claim": "Snow or heavy precipitation in period B suppressed taxi demand", "test": "weather"},
    "H_holiday": {"claim": "A public holiday inside period B lowered trips", "test": "holiday"},
    "H_price": {"claim": "A higher fare per trip in period B priced riders out", "test": "price"},
    "H_data_gap": {"claim": "Missing days in the data, not real demand, explain the drop", "test": "coverage"},
    "H_local": {"claim": "A segment-specific event, not a citywide one, caused the change", "test": "peers"},
}
