"""SQLite for observation sessions. There is no position or fill table."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from paper_desk.timeutil import format_utc

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dedup_key TEXT NOT NULL UNIQUE,
    ts TEXT NOT NULL,
    last_ts TEXT NOT NULL,
    seen_count INTEGER NOT NULL,
    chain TEXT,
    contract TEXT,
    side TEXT NOT NULL,
    reason TEXT NOT NULL,
    votes_json TEXT NOT NULL,
    vetoes_json TEXT NOT NULL,
    price TEXT,
    provenance_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS votes (
    chain TEXT NOT NULL,
    contract TEXT NOT NULL,
    agent TEXT NOT NULL,
    ts TEXT NOT NULL,
    reason TEXT NOT NULL,
    PRIMARY KEY (chain, contract, agent)
);
CREATE TABLE IF NOT EXISTS provider_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    provider TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    http_status INTEGER,
    ok INTEGER NOT NULL,
    error TEXT,
    rows INTEGER,
    elapsed_ms INTEGER
);
CREATE TABLE IF NOT EXISTS coverage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    chain TEXT NOT NULL,
    list_name TEXT NOT NULL,
    returned_rows INTEGER NOT NULL,
    address_drops INTEGER NOT NULL,
    scout_signals INTEGER NOT NULL,
    sniper_candidates INTEGER NOT NULL,
    enriched INTEGER NOT NULL,
    not_enriched INTEGER NOT NULL
);
"""


class ObserveStore:
    """One observation database. Writers take a process-local lock."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            path, check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        """Close the connection."""
        with self._lock:
            self._conn.close()

    def get_meta(self, key: str) -> str | None:
        """One metadata value."""
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key = ?", (key,)
            ).fetchone()
        return None if row is None else str(row["value"])

    def set_meta(self, key: str, value: str) -> None:
        """Insert or replace one metadata value."""
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO meta (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    def add_decision(
        self,
        *,
        dedup_key: str,
        ts: datetime,
        chain: str | None,
        contract: str | None,
        side: str,
        reason: str,
        votes: list[str],
        vetoes: list[str],
        price: str | None,
        provenance: dict[str, Any],
    ) -> bool:
        """Insert a decision, or increment it when the same key was already stored.

        Returns True when this is the first time the key is seen.
        """
        stamp = format_utc(ts)
        payload = json.dumps(provenance, default=str, sort_keys=True)
        with self._lock:
            try:
                self._conn.execute(
                    """
                    INSERT INTO decisions (
                        dedup_key, ts, last_ts, seen_count, chain, contract, side,
                        reason, votes_json, vetoes_json, price, provenance_json
                    ) VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        dedup_key,
                        stamp,
                        stamp,
                        chain,
                        contract,
                        side,
                        reason,
                        json.dumps(votes),
                        json.dumps(vetoes),
                        price,
                        payload,
                    ),
                )
            except sqlite3.IntegrityError:
                self._conn.execute(
                    """
                    UPDATE decisions
                    SET seen_count = seen_count + 1, last_ts = ?
                    WHERE dedup_key = ?
                    """,
                    (stamp, dedup_key),
                )
                return False
        return True

    def upsert_vote(
        self, chain: str, contract: str, agent: str, ts: datetime, reason: str
    ) -> None:
        """Replace one agent's observation vote. Whale is never written."""
        if agent not in {"scout", "sniper"}:
            msg = "observation votes are only scout or sniper"
            raise ValueError(msg)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO votes (chain, contract, agent, ts, reason)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(chain, contract, agent) DO UPDATE SET
                    ts = excluded.ts, reason = excluded.reason
                """,
                (chain, contract, agent, format_utc(ts), reason),
            )

    def votes(self) -> list[sqlite3.Row]:
        """Every stored vote."""
        with self._lock:
            return self._conn.execute("SELECT * FROM votes").fetchall()

    def add_call(
        self,
        ts: datetime,
        provider: str,
        endpoint: str,
        *,
        http_status: int | None,
        ok: bool,
        error: str | None,
        rows: int | None,
        elapsed_ms: int | None,
    ) -> None:
        """One outbound read."""
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO provider_calls (
                    ts, provider, endpoint, http_status, ok, error, rows, elapsed_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    format_utc(ts),
                    provider,
                    endpoint,
                    http_status,
                    1 if ok else 0,
                    error,
                    rows,
                    elapsed_ms,
                ),
            )

    def add_coverage(
        self,
        ts: datetime,
        *,
        chain: str,
        list_name: str,
        returned_rows: int,
        address_drops: int,
        scout_signals: int,
        sniper_candidates: int,
        enriched: int,
        not_enriched: int,
    ) -> None:
        """Denominator for one list read."""
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO coverage (
                    ts, chain, list_name, returned_rows, address_drops, scout_signals,
                    sniper_candidates, enriched, not_enriched
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    format_utc(ts),
                    chain,
                    list_name,
                    returned_rows,
                    address_drops,
                    scout_signals,
                    sniper_candidates,
                    enriched,
                    not_enriched,
                ),
            )

    def decisions(self) -> list[sqlite3.Row]:
        """Decisions in insert order."""
        with self._lock:
            return self._conn.execute("SELECT * FROM decisions ORDER BY id").fetchall()

    def coverage_rows(self) -> list[sqlite3.Row]:
        """Coverage rows in insert order."""
        with self._lock:
            return self._conn.execute("SELECT * FROM coverage ORDER BY id").fetchall()

    def call_rows(self) -> list[sqlite3.Row]:
        """Provider calls in insert order."""
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM provider_calls ORDER BY id"
            ).fetchall()

    def table_names(self) -> set[str]:
        """Tables actually created. Positions and fills are not among them."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        return {str(row["name"]) for row in rows}
