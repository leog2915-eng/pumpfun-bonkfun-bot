"""Restart-safe SQLite ledger. Cash and positions change inside one transaction."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from paper_desk.numbers import as_text, money
from paper_desk.timeutil import ensure_utc, format_utc, parse_utc

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    ended_at TEXT,
    initial_cash_usd TEXT NOT NULL,
    config_json TEXT NOT NULL,
    coverage_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cash (
    session_id TEXT PRIMARY KEY,
    cash_usd TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS positions (
    session_id TEXT NOT NULL,
    chain TEXT NOT NULL,
    contract TEXT NOT NULL,
    symbol TEXT NOT NULL,
    qty TEXT NOT NULL,
    remaining_cost_usd TEXT NOT NULL,
    avg_exec_price TEXT NOT NULL,
    original_qty TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    tp1_done INTEGER NOT NULL,
    tp2_done INTEGER NOT NULL,
    trail_armed INTEGER NOT NULL,
    trail_peak TEXT,
    status TEXT NOT NULL,
    realized_pnl_usd TEXT NOT NULL,
    fees_usd TEXT NOT NULL,
    PRIMARY KEY (session_id, chain, contract)
);
CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    chain TEXT NOT NULL,
    contract TEXT NOT NULL,
    side TEXT NOT NULL,
    reason TEXT NOT NULL,
    qty TEXT NOT NULL,
    exec_price TEXT NOT NULL,
    mid_price TEXT NOT NULL,
    gross_usd TEXT NOT NULL,
    fee_usd TEXT NOT NULL,
    gas_usd TEXT NOT NULL,
    cash_delta TEXT NOT NULL,
    cash_after TEXT NOT NULL,
    realized_pnl_usd TEXT NOT NULL,
    fee_source TEXT NOT NULL,
    gas_source TEXT NOT NULL,
    votes_json TEXT NOT NULL,
    vetoes_json TEXT NOT NULL,
    detail_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    chain TEXT,
    contract TEXT,
    side TEXT NOT NULL,
    reason TEXT NOT NULL,
    votes_json TEXT NOT NULL,
    vetoes_json TEXT NOT NULL,
    price TEXT,
    fee_usd TEXT,
    gas_usd TEXT,
    pnl_usd TEXT,
    detail_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    message TEXT NOT NULL,
    detail_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS votes (
    session_id TEXT NOT NULL,
    chain TEXT NOT NULL,
    contract TEXT NOT NULL,
    agent TEXT NOT NULL,
    ts TEXT NOT NULL,
    price TEXT NOT NULL,
    reason TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    PRIMARY KEY (session_id, chain, contract, agent)
);
CREATE TABLE IF NOT EXISTS whale_trades (
    session_id TEXT NOT NULL,
    tx_hash TEXT NOT NULL,
    chain TEXT NOT NULL,
    contract TEXT NOT NULL,
    maker TEXT NOT NULL,
    symbol TEXT NOT NULL,
    ts TEXT NOT NULL,
    amount_usd TEXT NOT NULL,
    price TEXT NOT NULL,
    PRIMARY KEY (session_id, tx_hash)
);
CREATE TABLE IF NOT EXISTS once (
    session_id TEXT NOT NULL,
    key TEXT NOT NULL,
    PRIMARY KEY (session_id, key)
);
"""


@dataclass(frozen=True)
class Position:
    """Open or closed paper lot."""

    chain: str
    contract: str
    symbol: str
    qty: Decimal
    remaining_cost_usd: Decimal
    avg_exec_price: Decimal
    original_qty: Decimal
    opened_at: datetime
    tp1_done: bool
    tp2_done: bool
    trail_armed: bool
    trail_peak: Decimal | None
    status: str
    realized_pnl_usd: Decimal
    fees_usd: Decimal


@dataclass(frozen=True)
class StoredVote:
    """One agent's latest bullish vote."""

    chain: str
    contract: str
    agent: str
    ts: datetime
    price: Decimal
    reason: str


def _d(text: str | None) -> Decimal:
    if text is None:
        return Decimal(0)
    return Decimal(text)


class Ledger:
    """One SQLite file. Writers take a process-local lock and BEGIN IMMEDIATE."""

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

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Atomic write section. Nested calls are not supported."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def create_session(
        self,
        *,
        session_id: str,
        mode: str,
        started_at: datetime,
        ends_at: datetime,
        cash: Decimal,
        config: dict[str, object],
        coverage: dict[str, object],
    ) -> None:
        """Insert a new session and its starting cash."""
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO sessions (
                    id, mode, status, started_at, ends_at, initial_cash_usd,
                    config_json, coverage_json
                ) VALUES (?, ?, 'running', ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    mode,
                    format_utc(started_at),
                    format_utc(ends_at),
                    as_text(cash),
                    json.dumps(config, sort_keys=True),
                    json.dumps(coverage, sort_keys=True),
                ),
            )
            conn.execute(
                "INSERT INTO cash (session_id, cash_usd) VALUES (?, ?)",
                (session_id, as_text(cash)),
            )

    def set_status(
        self, session_id: str, status: str, ended_at: datetime | None
    ) -> None:
        """Mark a session running, completed, or interrupted."""
        ended = format_utc(ended_at) if ended_at else None
        with self.transaction() as conn:
            conn.execute(
                "UPDATE sessions SET status = ?, ended_at = ? WHERE id = ?",
                (status, ended, session_id),
            )

    def session_row(self, session_id: str) -> sqlite3.Row | None:
        """One session, or None."""
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()

    def cash(self, session_id: str) -> Decimal:
        """Current paper cash."""
        with self._lock:
            row = self._conn.execute(
                "SELECT cash_usd FROM cash WHERE session_id = ?", (session_id,)
            ).fetchone()
        if row is None:
            msg = "session cash missing"
            raise KeyError(msg)
        return _d(row["cash_usd"])

    def upsert_vote(
        self, session_id: str, vote: StoredVote, evidence: dict[str, object]
    ) -> None:
        """Replace that agent's vote on this token."""
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO votes (
                    session_id, chain, contract, agent, ts, price, reason, evidence_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (session_id, chain, contract, agent) DO UPDATE SET
                    ts = excluded.ts,
                    price = excluded.price,
                    reason = excluded.reason,
                    evidence_json = excluded.evidence_json
                """,
                (
                    session_id,
                    vote.chain,
                    vote.contract,
                    vote.agent,
                    format_utc(vote.ts),
                    as_text(vote.price),
                    vote.reason,
                    json.dumps(evidence, default=str, sort_keys=True),
                ),
            )

    def fresh_votes(
        self, session_id: str, chain: str, contract: str, now: datetime, ttl: int
    ) -> list[StoredVote]:
        """Votes whose age is inside the TTL."""
        now_utc = ensure_utc(now)
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM votes
                WHERE session_id = ? AND chain = ? AND contract = ?
                """,
                (session_id, chain, contract),
            ).fetchall()
        fresh: list[StoredVote] = []
        for row in rows:
            stamp = parse_utc(row["ts"])
            age = (now_utc - stamp).total_seconds()
            if -5 <= age <= ttl:
                fresh.append(
                    StoredVote(
                        chain=row["chain"],
                        contract=row["contract"],
                        agent=row["agent"],
                        ts=stamp,
                        price=_d(row["price"]),
                        reason=row["reason"],
                    )
                )
        return fresh

    def quorum_keys(
        self, session_id: str, now: datetime, ttl: int
    ) -> list[tuple[str, str]]:
        """Tokens with at least two distinct fresh entry agents."""
        now_utc = ensure_utc(now)
        with self._lock:
            rows = self._conn.execute(
                "SELECT chain, contract, agent, ts FROM votes WHERE session_id = ?",
                (session_id,),
            ).fetchall()
        counts: dict[tuple[str, str], set[str]] = {}
        for row in rows:
            age = (now_utc - parse_utc(row["ts"])).total_seconds()
            if not (-5 <= age <= ttl):
                continue
            key = (row["chain"], row["contract"])
            counts.setdefault(key, set()).add(row["agent"])
        return [key for key, agents in counts.items() if len(agents) >= 2]

    def get_position(
        self, session_id: str, chain: str, contract: str
    ) -> Position | None:
        """The lot for this token, open or closed."""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM positions
                WHERE session_id = ? AND chain = ? AND contract = ?
                """,
                (session_id, chain, contract),
            ).fetchone()
        return _position(row) if row else None

    def open_positions(self, session_id: str) -> list[Position]:
        """Lots that still have quantity."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM positions
                WHERE session_id = ? AND status = 'open'
                ORDER BY opened_at
                """,
                (session_id,),
            ).fetchall()
        return [_position(row) for row in rows]

    def closed_positions(self, session_id: str) -> list[Position]:
        """Fully sold lots."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM positions
                WHERE session_id = ? AND status = 'closed'
                ORDER BY opened_at
                """,
                (session_id,),
            ).fetchall()
        return [_position(row) for row in rows]

    def count_open(self, session_id: str) -> int:
        """Open lot count."""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT COUNT(*) AS n FROM positions
                WHERE session_id = ? AND status = 'open'
                """,
                (session_id,),
            ).fetchone()
        return int(row["n"])

    def add_decision(
        self,
        session_id: str,
        ts: datetime,
        *,
        chain: str | None,
        contract: str | None,
        side: str,
        reason: str,
        votes: list[str],
        vetoes: list[str],
        price: Decimal | None,
        fee_usd: Decimal | None,
        gas_usd: Decimal | None,
        pnl_usd: Decimal | None,
        detail: dict[str, object],
    ) -> None:
        """Append one audit row."""
        with self.transaction() as conn:
            insert_decision_row(
                conn,
                session_id,
                ts,
                chain=chain,
                contract=contract,
                side=side,
                reason=reason,
                votes=votes,
                vetoes=vetoes,
                price=price,
                fee_usd=fee_usd,
                gas_usd=gas_usd,
                pnl_usd=pnl_usd,
                detail=detail,
            )

    def add_event(
        self,
        session_id: str,
        ts: datetime,
        kind: str,
        message: str,
        detail: dict[str, object],
    ) -> None:
        """Append an outage, coverage, or health note."""
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO events (session_id, ts, kind, message, detail_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    format_utc(ts),
                    kind,
                    message,
                    json.dumps(detail, default=str, sort_keys=True),
                ),
            )

    def claim_once(self, session_id: str, key: str) -> bool:
        """True the first time `key` is seen in this session."""
        with self.transaction() as conn:
            try:
                conn.execute(
                    "INSERT INTO once (session_id, key) VALUES (?, ?)",
                    (session_id, key),
                )
            except sqlite3.IntegrityError:
                return False
        return True

    def insert_whale_trade(
        self,
        session_id: str,
        *,
        tx_hash: str,
        chain: str,
        contract: str,
        maker: str,
        symbol: str,
        ts: datetime,
        amount_usd: Decimal,
        price: Decimal,
    ) -> bool:
        """Store a buy once. False when this hash was already stored."""
        with self.transaction() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO whale_trades (
                        session_id, tx_hash, chain, contract, maker, symbol,
                        ts, amount_usd, price
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        tx_hash,
                        chain,
                        contract,
                        maker,
                        symbol,
                        format_utc(ts),
                        as_text(amount_usd),
                        as_text(price),
                    ),
                )
            except sqlite3.IntegrityError:
                return False
        return True

    def whale_buys_for(
        self, session_id: str, chain: str, contract: str, since: datetime
    ) -> list[dict[str, object]]:
        """Stored buys at or after `since`."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM whale_trades
                WHERE session_id = ? AND chain = ? AND contract = ? AND ts >= ?
                """,
                (session_id, chain, contract, format_utc(since)),
            ).fetchall()
        return [dict(row) for row in rows]

    def recent_whale_keys(
        self, session_id: str, since: datetime
    ) -> list[tuple[str, str]]:
        """Tokens that have a stored buy inside the window."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT DISTINCT chain, contract FROM whale_trades
                WHERE session_id = ? AND ts >= ?
                """,
                (session_id, format_utc(since)),
            ).fetchall()
        return [(row["chain"], row["contract"]) for row in rows]

    def decisions(self, session_id: str) -> list[sqlite3.Row]:
        """Audit rows in order."""
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM decisions WHERE session_id = ? ORDER BY id",
                (session_id,),
            ).fetchall()

    def fills(self, session_id: str) -> list[sqlite3.Row]:
        """Fills in order."""
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM fills WHERE session_id = ? ORDER BY id",
                (session_id,),
            ).fetchall()

    def fee_total(self, session_id: str) -> Decimal:
        """Sum of fill fees and gas. Already inside PnL; reported separately."""
        with self._lock:
            row = self._conn.execute(
                """
                SELECT COALESCE(SUM(CAST(fee_usd AS REAL) + CAST(gas_usd AS REAL)), 0) AS n
                FROM fills WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
        return money(Decimal(str(row["n"])))


def _position(row: sqlite3.Row) -> Position:
    peak = row["trail_peak"]
    return Position(
        chain=row["chain"],
        contract=row["contract"],
        symbol=row["symbol"],
        qty=_d(row["qty"]),
        remaining_cost_usd=_d(row["remaining_cost_usd"]),
        avg_exec_price=_d(row["avg_exec_price"]),
        original_qty=_d(row["original_qty"]),
        opened_at=parse_utc(row["opened_at"]),
        tp1_done=bool(row["tp1_done"]),
        tp2_done=bool(row["tp2_done"]),
        trail_armed=bool(row["trail_armed"]),
        trail_peak=_d(peak) if peak is not None else None,
        status=row["status"],
        realized_pnl_usd=_d(row["realized_pnl_usd"]),
        fees_usd=_d(row["fees_usd"]),
    )


def insert_decision_row(
    conn: sqlite3.Connection,
    session_id: str,
    ts: datetime,
    *,
    chain: str | None,
    contract: str | None,
    side: str,
    reason: str,
    votes: list[str],
    vetoes: list[str],
    price: Decimal | None,
    fee_usd: Decimal | None,
    gas_usd: Decimal | None,
    pnl_usd: Decimal | None,
    detail: dict[str, object],
) -> None:
    conn.execute(
        """
        INSERT INTO decisions (
            session_id, ts, chain, contract, side, reason, votes_json, vetoes_json,
            price, fee_usd, gas_usd, pnl_usd, detail_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session_id,
            format_utc(ts),
            chain,
            contract,
            side,
            reason,
            json.dumps(votes),
            json.dumps(vetoes),
            as_text(price) if price is not None else None,
            as_text(fee_usd) if fee_usd is not None else None,
            as_text(gas_usd) if gas_usd is not None else None,
            as_text(pnl_usd) if pnl_usd is not None else None,
            json.dumps(detail, default=str, sort_keys=True),
        ),
    )
