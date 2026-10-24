"""Run Lightning SQL against Zetaris over the direct REST API and print the result.

    python scripts/zetaris/zsql.py "SELECT 1" [timeout_seconds]
    python scripts/zetaris/zsql.py --file scripts/zetaris/03_register_tables.sql [timeout_seconds]

`--file` strips `--` comment lines, splits the file on `;` and sends the statements one per request
(Lightning SQL accepts one statement per call), stopping at the first failure.
Reads ZETARIS_REST_URL (default http://localhost:8888/api/v1.0), ZETARIS_ORG_ID (default 1) and
ZETARIS_API_KEY from the environment or from a `.env` in the current directory / repo root.
The key is never printed. This is the helper the dataset in `scripts/zetaris/` was built with.
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from whytrail.env import load_dotenv  # noqa: E402


def statements_from_file(path: Path) -> list[str]:
    body = "\n".join(l for l in path.read_text(encoding="utf-8").splitlines() if not l.lstrip().startswith("--"))
    return [" ".join(s.split()) for s in body.split(";") if s.strip()]


def run_sql(sql: str, timeout: float) -> int:
    key = os.environ.get("ZETARIS_API_KEY")
    if not key:
        print("ZETARIS_API_KEY is not set (create it in the Zetaris UI: Management -> API Tokens)", file=sys.stderr)
        return 2
    base = os.environ.get("ZETARIS_REST_URL", "http://localhost:8888/api/v1.0").rstrip("/")
    org = os.environ.get("ZETARIS_ORG_ID", "1")
    print(f"> {sql[:160]}{'...' if len(sql) > 160 else ''}")
    t0 = time.time()
    r = httpx.post(f"{base}/sql-editor/sqls/run",
                   json={"queryId": str(uuid.uuid4()), "sql": sql, "source": "SqlEditor", "limit": 1000},
                   headers={"Authorization": f"Bearer {key}", "X-Org-ID": org, "X-Request-ID": str(uuid.uuid4())},
                   timeout=timeout)
    print(f"HTTP {r.status_code} in {time.time() - t0:.1f}s")
    try:
        j = r.json()
    except ValueError:
        print(r.text[:1500])
        return 1 if r.status_code >= 400 else 0
    if isinstance(j, dict) and isinstance(j.get("data"), list):
        print("headers:", j.get("headers"))
        print("rows:", len(j["data"]))
        for row in j["data"][:40]:
            print(row)
    else:
        print(json.dumps(j)[:1500])
    return 1 if r.status_code >= 400 else 0


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    load_dotenv()
    if argv[0] == "--file":
        if len(argv) < 2:
            print(__doc__)
            return 2
        timeout = float(argv[2]) if len(argv) > 2 else 600.0
        for sql in statements_from_file(Path(argv[1])):
            rc = run_sql(sql, timeout)
            if rc:
                return rc
        return 0
    return run_sql(argv[0], float(argv[1]) if len(argv) > 1 else 600.0)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
