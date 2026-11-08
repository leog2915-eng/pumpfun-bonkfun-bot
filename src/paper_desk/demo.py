"""Labeled synthetic scenario. Nothing here is a market read."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal

from paper_desk.book import Book
from paper_desk.security import CURVE_GRADUATED, CURVE_UNGRADUATED, Quote, SecurityView
from paper_desk.timeutil import UTC, ensure_utc

WIN = "11111111111111111111111111111111"
LOSS = "11111111111111111111111111111112"
CURVE = "11111111111111111111111111111113"
UNLOCKED = "11111111111111111111111111111114"
STALE = "11111111111111111111111111111115"
HONEY = "0x00000000000000000000000000000000000000aa"


class ManualClock:
    """Test clock. Starts aware so stored timestamps stay UTC."""

    def __init__(self, start: datetime | None = None) -> None:
        self.stamp = (
            ensure_utc(start) if start else datetime(2026, 11, 8, 17, 0, tzinfo=UTC)
        )

    def __call__(self) -> datetime:
        return self.stamp

    def advance(self, seconds: int) -> None:
        """Move the clock forward."""
        self.stamp = self.stamp + timedelta(seconds=seconds)


def run_scripted_scenario(book: Book, clock: ManualClock) -> None:
    """Drive the book through quorum, partials, a gap stop, and fail-closed vetoes.

    Every quote and security view is `origin=synthetic`. Paper mode rejects
    these. This function does not call GMGN or an RPC.
    """
    book.note(
        "coverage",
        "SYNTHETIC DEMO. Events below are scripted, not market data and not safety evidence.",
        {"origin": "synthetic"},
    )
    _pair(book, "sol", WIN, "WIN")
    book.try_entry(
        chain="sol",
        contract=WIN,
        symbol="WIN",
        quote=_quote(clock, "sol", WIN, Decimal("1")),
        view=_view(clock, "sol", WIN),
        gas_usd=Decimal(0),
        gas_source="synthetic_zero",
    )
    book.on_mark(_quote(clock, "sol", WIN, Decimal("4")), Decimal(0), "synthetic_zero")
    clock.advance(1)
    book.on_mark(_quote(clock, "sol", WIN, Decimal("3")), Decimal(0), "synthetic_zero")
    clock.advance(1)
    _pair(book, "sol", LOSS, "LOSS")
    book.try_entry(
        chain="sol",
        contract=LOSS,
        symbol="LOSS",
        quote=_quote(clock, "sol", LOSS, Decimal("1")),
        view=_view(clock, "sol", LOSS),
        gas_usd=Decimal(0),
        gas_source="synthetic_zero",
    )
    clock.advance(1)
    book.on_mark(
        _quote(clock, "sol", LOSS, Decimal("0.80")), Decimal(0), "synthetic_zero"
    )
    book.block_once(
        key=f"curve:{CURVE}:create",
        chain="sol",
        contract=CURVE,
        reason="ungraduated_bonding_curve",
        vetoes=["curve_ungraduated"],
        detail={
            "origin": "synthetic",
            "note": "Bonding-curve virtual reserves are not locked liquidity.",
        },
    )
    book.block_once(
        key=f"sniper:{UNLOCKED}:liquidity_lock_unknown",
        chain="sol",
        contract=UNLOCKED,
        reason="liquidity_lock_unknown",
        vetoes=["liquidity_lock_unknown"],
        detail={"origin": "synthetic", "curve": CURVE_GRADUATED},
    )
    _pair(book, "sol", STALE, "STALE")
    stale = _quote(clock, "sol", STALE, Decimal("1"))
    stale = Quote(
        chain=stale.chain,
        contract=stale.contract,
        price_usd=stale.price_usd,
        liquidity_usd=stale.liquidity_usd,
        fee_ratio_raw=None,
        fetched_at=clock() - timedelta(hours=1),
        source="synthetic",
        origin="synthetic",
    )
    book.try_entry(
        chain="sol",
        contract=STALE,
        symbol="STALE",
        quote=stale,
        view=_view(clock, "sol", STALE),
        gas_usd=Decimal(0),
        gas_source="synthetic_zero",
    )
    _pair(book, "base", HONEY, "HONEY")
    honey = replace(_view(clock, "base", HONEY), honeypot="yes")
    book.try_entry(
        chain="base",
        contract=HONEY,
        symbol="HONEY",
        quote=_quote(clock, "base", HONEY, Decimal("1")),
        view=honey,
        gas_usd=Decimal(0),
        gas_source="synthetic_zero",
    )


def _pair(book: Book, chain: str, contract: str, symbol: str) -> None:
    book.record_signal(
        agent="scout",
        chain=chain,
        contract=contract,
        price=Decimal("1"),
        reason="volume_spike",
        symbol=symbol,
        evidence={"origin": "synthetic"},
    )
    book.record_signal(
        agent="whale",
        chain=chain,
        contract=contract,
        price=Decimal("1"),
        reason="smart_money_convergence",
        symbol=symbol,
        evidence={"origin": "synthetic", "wallets": ["w1", "w2"]},
    )


def _quote(clock: ManualClock, chain: str, contract: str, price: Decimal) -> Quote:
    return Quote(
        chain=chain,
        contract=contract,
        price_usd=price,
        liquidity_usd=Decimal("1000000000"),
        fee_ratio_raw=None,
        fetched_at=clock(),
        source="synthetic",
        origin="synthetic",
    )


def _view(clock: ManualClock, chain: str, contract: str) -> SecurityView:
    honeypot = "empty" if chain == "sol" else "no"
    return SecurityView(
        chain=chain,
        contract=contract,
        fetched_at=clock(),
        origin="synthetic",
        lock_percent=None,
        dev_team_hold_rate=None,
        bundler_rate=None,
        honeypot=honeypot,
        curve=CURVE_UNGRADUATED,
        curve_evidence="synthetic view is not a launch clearance",
        fetch_error=None,
        field_sources={},
    )
