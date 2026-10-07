"""SQLite-backed state: candidate niches, actors in the fleet, events, LLM spend."""
from __future__ import annotations

import calendar
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY,
    domain TEXT UNIQUE NOT NULL,
    start_url TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    buyers TEXT NOT NULL DEFAULT '',
    score REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'new',      -- new | rejected | building | built | failed
    reason TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'scout',    -- scout | clone
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS actors (
    name TEXT PRIMARY KEY,
    apify_id TEXT NOT NULL DEFAULT '',
    domain TEXT NOT NULL,
    status TEXT NOT NULL,                    -- live | maintenance | needs_pricing | retired
    spec_json TEXT NOT NULL,
    code TEXT NOT NULL,
    sample_json TEXT NOT NULL DEFAULT '[]',
    version INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    published_at REAL,
    last_check_at REAL,
    last_ok_at REAL,
    maintenance_since REAL,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    heal_count INTEGER NOT NULL DEFAULT 0,
    cloned INTEGER NOT NULL DEFAULT 0,
    users7 INTEGER NOT NULL DEFAULT 0,
    users30 INTEGER NOT NULL DEFAULT 0,
    runs30_total INTEGER NOT NULL DEFAULT 0,
    runs30_failed INTEGER NOT NULL DEFAULT 0,
    rating REAL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT '',
    message TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    model TEXT NOT NULL,
    purpose TEXT NOT NULL,
    cost_usd REAL NOT NULL DEFAULT 0,
    ok INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

DAY = 86_400.0


def month_start(now: float | None = None) -> float:
    t = time.gmtime(now if now is not None else time.time())
    return float(calendar.timegm((t.tm_year, t.tm_mon, 1, 0, 0, 0)))


class State:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    # ---- key/value -------------------------------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        row = self._conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def put(self, key: str, value: Any) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )

    # ---- events ----------------------------------------------------------------------------
    def log(self, kind: str, message: str, actor: str = "") -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO events(ts, kind, actor, message) VALUES(?, ?, ?, ?)",
                (time.time(), kind, actor, message[:4000]),
            )

    def events_since(self, since: float, kinds: tuple[str, ...] | None = None) -> list[sqlite3.Row]:
        rows = self._conn.execute("SELECT * FROM events WHERE ts>=? ORDER BY ts", (since,)).fetchall()
        return [r for r in rows if kinds is None or r["kind"] in kinds]

    # ---- LLM spend -------------------------------------------------------------------------
    def record_llm_call(self, model: str, purpose: str, cost_usd: float, ok: bool) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO llm_calls(ts, model, purpose, cost_usd, ok) VALUES(?, ?, ?, ?, ?)",
                (time.time(), model, purpose, cost_usd, int(ok)),
            )

    def llm_spend_since(self, since: float) -> float:
        row = self._conn.execute("SELECT COALESCE(SUM(cost_usd), 0) AS s FROM llm_calls WHERE ts>=?", (since,)).fetchone()
        return float(row["s"])

    def llm_calls_since(self, since: float) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM llm_calls WHERE ts>=?", (since,)).fetchone()
        return int(row["n"])

    # ---- candidates ------------------------------------------------------------------------
    def add_candidate(self, domain: str, start_url: str, summary: str, buyers: str, score: float,
                      source: str = "scout", status: str = "new", reason: str = "") -> bool:
        now = time.time()
        with self.tx() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO candidates(domain, start_url, summary, buyers, score, status, reason, source,"
                " created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (domain, start_url, summary, buyers, score, status, reason, source, now, now),
            )
            return cur.rowcount > 0

    def known_domains(self) -> set[str]:
        rows = self._conn.execute("SELECT domain FROM candidates UNION SELECT domain FROM actors").fetchall()
        return {r["domain"] for r in rows}

    def candidates(self, status: str | None = None) -> list[sqlite3.Row]:
        if status:
            return self._conn.execute(
                "SELECT * FROM candidates WHERE status=? ORDER BY score DESC, id", (status,)
            ).fetchall()
        return self._conn.execute("SELECT * FROM candidates ORDER BY score DESC, id").fetchall()

    def update_candidate(self, cid: int, **fields: Any) -> None:
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.tx() as c:
            c.execute(f"UPDATE candidates SET {cols} WHERE id=?", (*fields.values(), cid))

    # ---- actors ----------------------------------------------------------------------------
    def add_actor(self, name: str, apify_id: str, domain: str, status: str, spec: dict, code: str,
                  sample: list[dict] | None = None) -> None:
        now = time.time()
        with self.tx() as c:
            c.execute(
                "INSERT INTO actors(name, apify_id, domain, status, spec_json, code, sample_json, created_at,"
                " published_at, last_check_at, last_ok_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (name, apify_id, domain, status, json.dumps(spec), code, json.dumps((sample or [])[:3], default=str),
                 now, now if status == "live" else None, now, now),
            )

    def actors(self, *statuses: str) -> list[sqlite3.Row]:
        if statuses:
            marks = ",".join("?" for _ in statuses)
            return self._conn.execute(
                f"SELECT * FROM actors WHERE status IN ({marks}) ORDER BY created_at", statuses
            ).fetchall()
        return self._conn.execute("SELECT * FROM actors ORDER BY created_at").fetchall()

    def actor(self, name: str) -> sqlite3.Row | None:
        return self._conn.execute("SELECT * FROM actors WHERE name=?", (name,)).fetchone()

    def update_actor(self, name: str, **fields: Any) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        with self.tx() as c:
            c.execute(f"UPDATE actors SET {cols} WHERE name=?", (*fields.values(), name))

    def actors_created_since(self, since: float) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM actors WHERE created_at>=?", (since,)).fetchone()
        return int(row["n"])
