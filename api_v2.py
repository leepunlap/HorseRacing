"""v2 FastAPI router.

Mounted into app.py behind the `RACING_V2=1` env flag so v1 stays untouched.
Routes land under `/api/v2/...`. Phase P0 only ships `/api/v2/health` and
`/api/v2/schema-info`; later phases extend with strategies_v2 CRUD, live
status, kill-switch toggle, CLV report, drift dashboard, etc.
"""

import os
import sqlite3
from pathlib import Path

from fastapi import APIRouter

DATA_DIR = Path(__file__).parent / "data"
V2_DB = DATA_DIR / "racing_v2.db"

router = APIRouter(prefix="/api/v2", tags=["v2"])


def _v2_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(V2_DB)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@router.get("/health")
def health() -> dict:
    """Confirm v2 router is mounted and the v2 DB exists + responds."""
    info: dict = {
        "v2_enabled": True,
        "db_path": str(V2_DB),
        "db_exists": V2_DB.exists(),
        "pid": os.getpid(),
    }
    if V2_DB.exists():
        conn = _v2_connect()
        try:
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
            info["table_count"] = len(tables)
        finally:
            conn.close()
    return info


@router.get("/schema-info")
def schema_info() -> dict:
    """Per-table row counts. Useful as a P0 smoke endpoint."""
    if not V2_DB.exists():
        return {"error": "v2 DB not initialized", "db_path": str(V2_DB)}
    conn = _v2_connect()
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()]
        counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
    finally:
        conn.close()
    return {"db_path": str(V2_DB), "tables": counts}
