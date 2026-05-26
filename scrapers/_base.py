"""Shared infrastructure for scrapers.

Every one-shot scraper subclasses `BaseScraper`. Responsibilities:
  * Signal handling — SIGTERM commits the open DB txn, writes a checkpoint, and
    exits cleanly within the 5s budget that app.py:_SUBPROC_KILL_TIMEOUT allows.
  * Checkpoint I/O — `data/checkpoints/{name}.json`, read on start, written
    after each completed unit of work and on shutdown.
  * Raw HTML cache — fetched bytes are stashed under
    `data/raw/{source}/{date or key}.html` so reruns are offline-friendly
    and audits can replay the exact bytes the parser saw.
  * UPSERT helper — `INSERT ... ON CONFLICT(...) DO UPDATE SET ... `
    tables that all use natural-key UNIQUE constraints.
  * Progress printing — newline-flushed stdout lines that app.py's
    `_run_scraper` streams to /ws/progress as `scraper_log` messages.

Network fetch uses httpx for static HTML; subclasses that need JS-rendered
pages (HKJC tote-odds JSON endpoints work without JS, but trackwork pages
may need Playwright) override `fetch()`.
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import httpx

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "racing.db"
CHECKPOINT_DIR = DATA_DIR / "checkpoints"
RAW_CACHE_DIR = DATA_DIR / "raw"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9,zh-HK;q=0.8",
}


def log(msg: str) -> None:
    """Print to stdout; app.py streams these to /ws/progress."""
    print(msg, flush=True)


class BaseScraper:
    """Common scaffolding for scrapers.

    Subclass and implement:
      * `name` (class attribute): used for checkpoint filename + raw cache subdir.
      * `run(args)`: do the work. Should call `self.checkpoint(...)` periodically
        and `self.upsert(...)` for DB writes. Should poll `self.should_stop()`
        in any long loop.
    """

    name: str = "base"

    def __init__(self, *, db_path: Path = DB_PATH) -> None:
        self.db_path = db_path
        self._stop = False
        self._conn: sqlite3.Connection | None = None
        CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
        (RAW_CACHE_DIR / self.name).mkdir(parents=True, exist_ok=True)
        self._install_signals()

    # ─── lifecycle ─────────────────────────────────────────────
    def _install_signals(self) -> None:
        def _handler(signum, _frame):  # noqa: ANN001
            self._stop = True
            log(f"[{self.name}] received signal {signum}, draining…")

        signal.signal(signal.SIGTERM, _handler)
        signal.signal(signal.SIGINT, _handler)

    def should_stop(self) -> bool:
        return self._stop

    # ─── DB helpers ────────────────────────────────────────────
    def db(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path)
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.execute("PRAGMA journal_mode = WAL")
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.commit()
            finally:
                self._conn.close()
                self._conn = None

    def upsert(self, table: str, row: dict[str, Any], conflict_cols: Iterable[str]) -> None:
        """INSERT ... ON CONFLICT(natural-key) DO UPDATE SET ...

        Uses the table's UNIQUE constraint as defined in db.py. Any column
        in `row` not in `conflict_cols` is updated on conflict.
        """
        cols = list(row.keys())
        placeholders = ",".join("?" for _ in cols)
        col_list = ",".join(cols)
        update_cols = [c for c in cols if c not in set(conflict_cols)]
        update_clause = (
            "DO UPDATE SET " + ",".join(f"{c}=excluded.{c}" for c in update_cols)
            if update_cols
            else "DO NOTHING"
        )
        sql = (
            f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) "
            f"ON CONFLICT({','.join(conflict_cols)}) {update_clause}"
        )
        self.db().execute(sql, [row[c] for c in cols])

    # ─── checkpoint ────────────────────────────────────────────
    def _checkpoint_path(self) -> Path:
        return CHECKPOINT_DIR / f"{self.name}.json"

    def load_checkpoint(self) -> dict[str, Any]:
        path = self._checkpoint_path()
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text())
        except Exception:
            return {}

    def checkpoint(self, state: dict[str, Any]) -> None:
        """Commit DB + write checkpoint file atomically."""
        if self._conn is not None:
            self._conn.commit()
        state = {**state, "updated_at": datetime.now().isoformat()}
        path = self._checkpoint_path()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2))
        os.replace(tmp, path)

    # ─── raw cache ─────────────────────────────────────────────
    def cache_path(self, key: str) -> Path:
        return RAW_CACHE_DIR / self.name / f"{key}.html"

    def read_cached(self, key: str) -> str | None:
        p = self.cache_path(key)
        if p.exists():
            return p.read_text(encoding="utf-8")
        return None

    def write_cache(self, key: str, body: str) -> None:
        p = self.cache_path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")

    # ─── fetch with retry + cache ──────────────────────────────
    def fetch(
        self,
        url: str,
        *,
        cache_key: str | None = None,
        max_attempts: int = 3,
        backoff: float = 1.5,
        force_refresh: bool = False,
        timeout: float = 20.0,
    ) -> str:
        if cache_key and not force_refresh:
            cached = self.read_cached(cache_key)
            if cached is not None:
                return cached

        last_err: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            if self.should_stop():
                raise RuntimeError("stop requested")
            try:
                with httpx.Client(headers=DEFAULT_HEADERS, timeout=timeout, follow_redirects=True) as client:
                    r = client.get(url)
                    r.raise_for_status()
                    body = r.text
                if cache_key:
                    self.write_cache(cache_key, body)
                return body
            except Exception as exc:
                last_err = exc
                log(f"[{self.name}] fetch attempt {attempt} failed for {url}: {exc}")
                if attempt < max_attempts:
                    time.sleep(backoff ** attempt)
        raise RuntimeError(f"fetch failed after {max_attempts} attempts: {last_err}")

    # ─── entry point ───────────────────────────────────────────
    def run(self, args: list[str] | None = None) -> int:  # noqa: ARG002
        raise NotImplementedError

    @classmethod
    def main(cls, args: list[str] | None = None) -> int:
        s = cls()
        try:
            return s.run(args or sys.argv[1:])
        except RuntimeError as exc:
            log(f"[{s.name}] aborted: {exc}")
            return 1
        finally:
            s.close()


# ─── tiny helpers reused across scrapers ──────────────────────────────────────
def lookup_horse_id(conn: sqlite3.Connection, brand: str) -> int | None:
    row = conn.execute("SELECT id FROM horses WHERE brand = ?", (brand,)).fetchone()
    return row[0] if row else None


def lookup_race_id(conn: sqlite3.Connection, date: str, course: str, race_no: int) -> int | None:
    row = conn.execute(
        "SELECT id FROM races WHERE date = ? AND course = ? AND race_no = ?",
        (date, course, race_no),
    ).fetchone()
    return row[0] if row else None


@contextmanager
def txn(conn: sqlite3.Connection):
    """Yield, then commit on success / rollback on exception."""
    try:
        yield
        conn.commit()
    except Exception:
        conn.rollback()
        raise
