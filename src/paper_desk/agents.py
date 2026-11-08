"""Five coordinated tasks: scout, whale, sniper, shield, and exit.

Shield and exit never cast an entry vote. Exit does not wait for quorum.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from paper_desk.book import Book
from paper_desk.config import Settings
from paper_desk.errors import MarketError
from paper_desk.security import (
    CURVE_GRADUATED,
    shield_vetoes,
    sniper_blocks,
)
from paper_desk.signals import (
    cluster_ready,
    normalize_contract,
    reference_price,
    scout_hits,
    whale_buys,
)
from paper_desk.source import MarketSource
from paper_desk.timeutil import ensure_utc

_CURVE_DETAIL = (
    "Ungraduated bonding-curve launches have no locked LP in this desk. "
    "Virtual reserves are not treated as locked liquidity."
)


async def scout_agent(
    book: Book,
    source: MarketSource,
    settings: Settings,
    stop: asyncio.Event,
    wake: asyncio.Event,
    health: dict[str, Any],
) -> None:
    """Poll GMGN rank for volume spikes and top gainers."""
    backoff = 1
    while not stop.is_set():
        try:
            voted = 0
            rows_seen = 0
            for chain in settings.chains:
                specs = (
                    ("spike", settings.scout_spike_interval, "volume"),
                    ("gainer", settings.scout_gainer_interval, "change"),
                )
                for role, interval, order in specs:
                    rows = await source.rank(chain, interval, order)
                    rows_seen += len(rows)
                    for hit in scout_hits(
                        rows, chain=chain, role=role, settings=settings
                    ):
                        voted += _signal(book, wake, "scout", hit, source.origin)
            health["scout"] = {"ok": True, "rows": rows_seen, "votes": voted}
            book.note(
                "coverage",
                "scout poll",
                {"rows": rows_seen, "votes": voted, "origin": source.origin},
            )
            backoff = 1
        except MarketError as exc:
            health["scout"] = {"ok": False, "error": str(exc)}
            book.note("outage", "scout poll failed", {"error": str(exc)})
            await _sleep(stop, backoff)
            backoff = min(backoff * 2, 60)
            continue
        await _sleep(stop, settings.poll_seconds)


async def whale_agent(
    book: Book,
    source: MarketSource,
    settings: Settings,
    stop: asyncio.Event,
    wake: asyncio.Event,
    health: dict[str, Any],
) -> None:
    """Track smart-money buys and vote when distinct wallets converge."""
    backoff = 1
    while not stop.is_set():
        try:
            inserted = 0
            for chain in settings.chains:
                rows = await source.smartmoney(chain)
                for buy in whale_buys(
                    rows, chain=chain, now=book.now(), settings=settings
                ):
                    traded_at = buy["traded_at"]
                    amount = buy["amount_usd"]
                    price = buy["price"]
                    if (
                        not isinstance(traded_at, datetime)
                        or not isinstance(amount, Decimal)
                        or not isinstance(price, Decimal)
                    ):
                        continue
                    if book.ledger.insert_whale_trade(
                        book.session_id,
                        tx_hash=str(buy["tx_hash"]),
                        chain=str(buy["chain"]),
                        contract=str(buy["contract"]),
                        maker=str(buy["maker"]),
                        symbol=str(buy["symbol"]),
                        ts=traded_at,
                        amount_usd=amount,
                        price=price,
                    ):
                        inserted += 1
            voted = _vote_clusters(book, settings, wake, source.origin)
            health["whale"] = {"ok": True, "inserted": inserted, "votes": voted}
            book.note(
                "coverage",
                "whale poll",
                {"inserted": inserted, "votes": voted, "origin": source.origin},
            )
            backoff = 1
        except MarketError as exc:
            health["whale"] = {"ok": False, "error": str(exc)}
            book.note("outage", "whale poll failed", {"error": str(exc)})
            await _sleep(stop, backoff)
            backoff = min(backoff * 2, 60)
            continue
        await _sleep(stop, settings.poll_seconds)


async def sniper_agent(
    book: Book,
    source: MarketSource,
    settings: Settings,
    stop: asyncio.Event,
    wake: asyncio.Event,
    launches: asyncio.Queue[dict[str, str]],
    health: dict[str, Any],
) -> None:
    """Watch launch samples. Clean votes require locked LP, dev < 5%, no bundle."""
    backoff = 1
    while not stop.is_set():
        drained = _drain(launches)
        for event in drained:
            await _from_listener(book, source, settings, wake, event)
        try:
            sections = await source.trenches()
            counts = {
                name: len(rows)
                for name, rows in sections.items()
                if isinstance(rows, list)
            }
            for name, rows in sections.items():
                for row in rows:
                    if isinstance(row, dict):
                        await _from_trench(book, source, settings, wake, name, row)
            health["sniper"] = {
                "ok": True,
                "sections": counts,
                "listener_events": len(drained),
            }
            book.note(
                "coverage",
                "sniper poll",
                {
                    "sections": counts,
                    "listener_events": len(drained),
                    "origin": source.origin,
                },
            )
            backoff = 1
        except MarketError as exc:
            health["sniper"] = {"ok": False, "error": str(exc)}
            book.note("outage", "sniper poll failed", {"error": str(exc)})
            await _sleep(stop, backoff)
            backoff = min(backoff * 2, 60)
            continue
        await _sleep(stop, settings.poll_seconds)


async def shield_agent(
    book: Book,
    source: MarketSource,
    settings: Settings,
    stop: asyncio.Event,
    wake: asyncio.Event,
    health: dict[str, Any],
) -> None:
    """Mandatory clearance. This task does not vote."""
    while not stop.is_set():
        try:
            await asyncio.wait_for(wake.wait(), timeout=settings.entry_retry_seconds)
        except TimeoutError:
            pass
        wake.clear()
        if stop.is_set():
            return
        tried = 0
        for chain, contract in book.ledger.quorum_keys(
            book.session_id, book.now(), settings.vote_ttl_seconds
        ):
            if book.ledger.get_position(book.session_id, chain, contract) is not None:
                continue
            tried += 1
            try:
                quote, view, gas_usd, gas_source = await source.bundle(
                    chain, contract, None
                )
            except MarketError as exc:
                book.note(
                    "outage", "shield read failed", {"error": str(exc), "chain": chain}
                )
                book.entry_unavailable(chain, contract, "security_fetch_failed")
                continue
            book.try_entry(
                chain=chain,
                contract=contract,
                symbol=contract,
                quote=quote,
                view=view,
                gas_usd=gas_usd,
                gas_source=gas_source,
            )
        health["shield"] = {"ok": True, "checked": tried}


async def exit_agent(
    book: Book,
    source: MarketSource,
    settings: Settings,
    stop: asyncio.Event,
    health: dict[str, Any],
    marks: dict[tuple[str, str], Any],
) -> None:
    """Mark open lots and exit on stop, partial targets, or the trail.

    Quorum is not consulted. A failed or stale read leaves the lot open.
    """
    while not stop.is_set():
        positions = book.ledger.open_positions(book.session_id)
        checked = 0
        for pos in positions:
            if stop.is_set():
                return
            try:
                quote, gas_usd, gas_source = await source.mark(pos.chain, pos.contract)
            except MarketError as exc:
                book.note(
                    "outage",
                    "exit quote failed",
                    {"error": str(exc), "chain": pos.chain, "token": pos.contract},
                )
                book.mark_unavailable(
                    pos.chain, pos.contract, "exit_blocked_source_outage"
                )
                continue
            marks[(pos.chain, pos.contract)] = quote
            book.on_mark(quote, gas_usd, gas_source)
            checked += 1
        health["exit"] = {"ok": True, "marks": checked}
        await _sleep(stop, settings.mark_seconds)


def _signal(
    book: Book,
    wake: asyncio.Event,
    agent: str,
    hit: dict[str, object],
    origin: str,
) -> int:
    price = hit["price"]
    if not isinstance(price, Decimal):
        return 0
    state = book.record_signal(
        agent=agent,
        chain=str(hit["chain"]),
        contract=str(hit["contract"]),
        price=price,
        reason=str(hit["reason"]),
        symbol=str(hit.get("symbol") or ""),
        evidence={"origin": origin, **{k: str(v) for k, v in hit.items()}},
    )
    if state == "quorum":
        wake.set()
    return 1


def _vote_clusters(
    book: Book, settings: Settings, wake: asyncio.Event, origin: str
) -> int:
    since = ensure_utc(book.now()) - timedelta(seconds=settings.whale_window_seconds)
    voted = 0
    for chain, contract in book.ledger.recent_whale_keys(book.session_id, since):
        rows = book.ledger.whale_buys_for(book.session_id, chain, contract, since)
        buys = [
            {
                "maker": row["maker"],
                "price": Decimal(str(row["price"])),
                "traded_at": _parse_row_time(str(row["ts"])),
            }
            for row in rows
        ]
        if not cluster_ready(buys, settings):
            continue
        symbol = str(rows[-1]["symbol"]) if rows else ""
        state = book.record_signal(
            agent="whale",
            chain=chain,
            contract=contract,
            price=reference_price(buys),
            reason="smart_money_convergence",
            symbol=symbol,
            evidence={
                "origin": origin,
                "wallets": sorted({str(item["maker"]) for item in buys}),
                "window_seconds": settings.whale_window_seconds,
            },
        )
        voted += 1
        if state == "quorum":
            wake.set()
    return voted


def _parse_row_time(text: str) -> Any:
    from paper_desk.timeutil import parse_utc

    return parse_utc(text)


def _drain(queue: asyncio.Queue[dict[str, str]]) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    while True:
        try:
            items.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            return items


async def _from_listener(
    book: Book,
    source: MarketSource,
    settings: Settings,
    wake: asyncio.Event,
    event: dict[str, str],
) -> None:
    contract = normalize_contract(event.get("chain", "sol"), event.get("contract", ""))
    if contract is None:
        return
    stage = event.get("stage", "create")
    if stage != "migrated":
        book.block_once(
            key=f"curve:{contract}:{stage}",
            chain="sol",
            contract=contract,
            reason="ungraduated_bonding_curve",
            vetoes=["curve_ungraduated"],
            detail={
                "stage": stage,
                "source": event.get("source", ""),
                "note": _CURVE_DETAIL,
                "platform": event.get("platform", ""),
            },
        )
        return
    await _consider(
        book,
        source,
        settings,
        wake,
        contract,
        CURVE_GRADUATED,
        "upstream listener reported a completed migration",
        None,
        event.get("symbol", ""),
    )


async def _from_trench(
    book: Book,
    source: MarketSource,
    settings: Settings,
    wake: asyncio.Event,
    section: str,
    row: dict[str, object],
) -> None:
    address = row.get("address")
    if not isinstance(address, str):
        return
    contract = normalize_contract("sol", address)
    if contract is None:
        return
    symbol = row.get("symbol") if isinstance(row.get("symbol"), str) else ""
    if section in {"new_creation", "near_completion"}:
        book.block_once(
            key=f"curve:{contract}:{section}",
            chain="sol",
            contract=contract,
            reason="ungraduated_bonding_curve",
            vetoes=["curve_ungraduated"],
            detail={"section": section, "note": _CURVE_DETAIL, "symbol": symbol},
        )
        return
    if section != "completed":
        book.note("coverage", "unsupported trenches section", {"section": section})
        return
    await _consider(
        book,
        source,
        settings,
        wake,
        contract,
        CURVE_GRADUATED,
        "gmgn trenches completed section",
        row,
        symbol,
    )


async def _consider(
    book: Book,
    source: MarketSource,
    settings: Settings,
    wake: asyncio.Event,
    contract: str,
    curve: str,
    evidence: str,
    context: dict[str, object] | None,
    symbol: str,
) -> None:
    try:
        quote, view, _gas, _source = await source.bundle("sol", contract, context)
    except MarketError as exc:
        book.block_once(
            key=f"sniper-read:{contract}",
            chain="sol",
            contract=contract,
            reason="security_fetch_failed",
            vetoes=["security_fetch_failed"],
            detail={"error": str(exc), "curve_evidence": evidence},
        )
        return
    view = replace(view, curve=curve, curve_evidence=evidence)
    blocks = sniper_blocks(view, settings)
    clearance = shield_vetoes(view, quote, settings, book.now())
    if blocks or clearance:
        reasons = blocks + clearance
        book.block_once(
            key=f"sniper:{contract}:{reasons[0]}",
            chain="sol",
            contract=contract,
            reason=reasons[0],
            vetoes=reasons,
            detail={
                "curve_evidence": evidence,
                "field_sources": view.field_sources,
                "note": _CURVE_DETAIL if curve != CURVE_GRADUATED else "",
                "symbol": symbol,
            },
        )
        return
    state = book.record_signal(
        agent="sniper",
        chain="sol",
        contract=contract,
        price=quote.price_usd,
        reason="clean_graduated_launch",
        symbol=symbol,
        evidence={
            "origin": source.origin,
            "curve": curve,
            "lock_percent": str(view.lock_percent),
            "dev_team_hold_rate": str(view.dev_team_hold_rate),
            "bundler_rate": str(view.bundler_rate),
        },
    )
    if state == "quorum":
        wake.set()


async def _sleep(stop: asyncio.Event, seconds: int | float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        return
