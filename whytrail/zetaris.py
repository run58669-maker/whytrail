"""Zetaris data harness clients.

Live mode talks to Zetaris over the direct REST API documented in
zetaris/getting-started (docs/connections/client-setup.md, scripts/zetaris_sql.py):

    POST {ZETARIS_REST_URL}/sql-editor/sqls/run
    headers: Authorization: Bearer <ZETARIS_API_KEY>, X-Org-ID, X-Request-ID (UUID)
    body:    {"queryId": <UUID>, "sql": <one statement>, "source": "SqlEditor", "limit": N}

One Lightning SQL statement per call. REST returns values as strings, so numeric
values are converted before comparison.

Mock mode (`MockZetaris`) serves the same qualified names (SOURCE.table) from
SQLite databases attached under the source names, so the identical SQL text,
including cross-source joins, runs in both modes.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

from . import sample_data

DEFAULT_REST_URL = "http://localhost:8888/api/v1.0"


class ZetarisError(RuntimeError):
    pass


@dataclass
class QueryResult:
    sql: str
    columns: list[str] | None
    rows: list[list[Any]]
    source: str  # "zetaris-rest" | "mock-sqlite"
    elapsed_ms: float
    sha256: str = field(default="")

    def __post_init__(self) -> None:
        if not self.sha256:
            payload = json.dumps(self.rows, default=str, sort_keys=True)
            self.sha256 = hashlib.sha256(payload.encode()).hexdigest()[:16]

    def scalar(self) -> float | None:
        if not self.rows or not self.rows[0]:
            return None
        return to_number(self.rows[0][0])


def to_number(value: Any) -> float | None:
    """REST returns strings, sometimes in exponent format."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except ValueError:
        return None


def _error_text(resp: httpx.Response, limit: int = 500) -> str:
    """Zetaris errors arrive as {"msg": "..."}; return that message, else the raw body."""
    try:
        out = resp.json()
        if isinstance(out, dict) and out.get("msg"):
            return str(out["msg"])[:limit]
    except ValueError:
        pass
    return resp.text[:limit]


class ZetarisRestClient:
    name = "zetaris-rest"

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        org_id: str | None = None,
        timeout_s: float = 20.0,
        retries: int = 1,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base = (base_url or os.environ.get("ZETARIS_REST_URL") or DEFAULT_REST_URL).rstrip("/")
        self._key = api_key or os.environ.get("ZETARIS_API_KEY")
        self.org = org_id or os.environ.get("ZETARIS_ORG_ID") or ""
        if not self._key:
            raise ZetarisError("ZETARIS_API_KEY is not set (create it in the Zetaris UI: Management -> API Tokens).")
        if not self.org.isdigit():
            raise ZetarisError("ZETARIS_ORG_ID must be the numeric organisation ID.")
        self.retries = retries
        self._http = httpx.Client(timeout=timeout_s, transport=transport)
        self.query_count = 0

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._key}",
            "X-Org-ID": self.org,
            "X-Request-ID": str(uuid.uuid4()),
            "Content-Type": "application/json",
        }

    def query(self, sql: str, limit: int = 1000) -> QueryResult:
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            # Fresh queryId per attempt: Zetaris rejects a reused id with "Query id already exists",
            # which would hide the real error of the first attempt.
            body = {"queryId": str(uuid.uuid4()), "sql": sql, "source": "SqlEditor", "limit": limit}
            t0 = time.perf_counter()
            try:
                resp = self._http.post(f"{self.base}/sql-editor/sqls/run", json=body, headers=self._headers())
                if resp.status_code >= 400:
                    raise ZetarisError(f"HTTP {resp.status_code}: {_error_text(resp)}")
                out = resp.json()
                self.query_count += 1
                rows = out.get("data") if isinstance(out, dict) else None
                columns = None
                if isinstance(out, dict):
                    # Live Zetaris returns column names under "headers".
                    cols = out.get("headers") or out.get("columns") or out.get("schema")
                    if isinstance(cols, list):
                        columns = [c.get("name") if isinstance(c, dict) else str(c) for c in cols]
                return QueryResult(sql, columns, rows or [], self.name, (time.perf_counter() - t0) * 1000)
            except (httpx.HTTPError, ZetarisError, ValueError) as e:
                last = e
        raise ZetarisError(f"Zetaris query failed after {self.retries + 1} attempts: {last}")

    def ping(self) -> bool:
        return self.query("SELECT 1").scalar() == 1.0


class MockZetaris:
    """SQLite stand-in exposing the same SOURCE.table names as the live instance."""

    name = "mock-sqlite"

    SOURCES = {
        "NY_TAXI": ("taxi_daily", "trip_date TEXT, borough TEXT, trips INTEGER, fare_total_usd REAL",
                    sample_data.taxi_rows),
        "NOAA_GHCN": ("central_park_daily", "obs_date TEXT, prcp_mm REAL, snow_mm REAL, tmax_c REAL, tmin_c REAL",
                      sample_data.weather_rows),
        "REF": ("us_holidays", "holiday_date TEXT, name TEXT", sample_data.holiday_rows),
    }

    def __init__(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        for source, (table, ddl, rows_fn) in self.SOURCES.items():
            self.conn.execute(f"ATTACH DATABASE ':memory:' AS {source}")
            self.conn.execute(f"CREATE TABLE {source}.{table} ({ddl})")
            rows = rows_fn()
            marks = ",".join("?" * len(rows[0]))
            self.conn.executemany(f"INSERT INTO {source}.{table} VALUES ({marks})", rows)
        self.conn.commit()
        self.query_count = 0

    def query(self, sql: str, limit: int = 1000) -> QueryResult:
        if ";" in sql.strip().rstrip(";"):
            raise ZetarisError("one statement per call")
        t0 = time.perf_counter()
        try:
            cur = self.conn.execute(sql)
        except sqlite3.Error as e:
            raise ZetarisError(str(e)) from e
        rows = [list(r) for r in cur.fetchmany(limit)]
        cols = [d[0] for d in cur.description] if cur.description else None
        self.query_count += 1
        return QueryResult(sql, cols, rows, self.name, (time.perf_counter() - t0) * 1000)

    def ping(self) -> bool:
        return self.query("SELECT 1").scalar() == 1.0


def make_client(mock: bool):
    return MockZetaris() if mock else ZetarisRestClient()
