"""Observation loop. It never calls `execute_trade`."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from paper_desk.observe.assess import (
    PoolView,
    RiskView,
    curve_for_dex,
    parse_gecko_pool,
    risk_from_goplus,
    risk_from_rugcheck,
    scout_reason,
    vetoes,
)
from paper_desk.observe.config import ObserveSettings
from paper_desk.observe.providers import Feeds, ReadError
from paper_desk.observe.report import build_observe_report, write_observe_report
from paper_desk.observe.store import ObserveStore
from paper_desk.timeutil import (
    add_seconds,
    ensure_utc,
    format_et,
    format_utc,
    parse_utc,
    utc_now,
)

ROTATION = (
    ("solana", "sol", "trending"),
    ("base", "base", "trending"),
    ("bsc", "bsc", "trending"),
    ("solana", "sol", "new"),
    ("base", "base", "new"),
    ("bsc", "bsc", "new"),
)


class ObservationDoesNotTradeError(RuntimeError):
    """Raised if something tries to send an order from observation mode."""


def execute_trade(*_args: object, **_kwargs: object) -> None:
    """Guard. The loop does not call this."""
    raise ObservationDoesNotTradeError("observation mode does not send orders")


async def run_observation(
    settings: ObserveSettings,
    data_dir: Path,
    duration_seconds: int,
    *,
    resume_id: str | None = None,
    feeds: Feeds | None = None,
    now_fn: Any = None,
    sleep_fn: Any = None,
) -> Path:
    """Run until the deadline, then write the recap. No orders are sent."""
    clock = now_fn or utc_now
    pause = sleep_fn or asyncio.sleep
    data_dir.mkdir(parents=True, exist_ok=True)
    if resume_id:
        folder = data_dir / resume_id
        store = ObserveStore(folder / "observe.sqlite")
        if store.get_meta("mode") != "OBSERVATION_ONLY":
            store.close()
            msg = "refusing to resume a session that is not observation"
            raise ValueError(msg)
        ends = parse_utc(str(store.get_meta("ends_at")))
        session_id = resume_id
    else:
        started = ensure_utc(clock())
        session_id = started.strftime("%Y%m%dT%H%M%SZ") + "-" + os.urandom(3).hex()
        ends = add_seconds(started, duration_seconds)
        folder = data_dir / session_id
        folder.mkdir(parents=True, exist_ok=True)
        store = ObserveStore(folder / "observe.sqlite")
        store.set_meta("mode", "OBSERVATION_ONLY")
        store.set_meta("session_id", session_id)
        store.set_meta("status", "running")
        store.set_meta("started_at", format_utc(started))
        store.set_meta("ends_at", format_utc(ends))
        store.set_meta(
            "settings", json.dumps(settings.to_public_dict(), sort_keys=True)
        )
        store.set_meta("rotation", "0")
        _note_agents(store, started)
    (data_dir / "CURRENT").write_text(session_id + "\n", encoding="utf-8")
    try:
        if feeds is None:
            await _run_live(store, settings, folder, ends, clock, pause)
        else:
            await _loop(store, settings, folder, ends, feeds, clock, pause)
        store.set_meta("status", "completed")
        store.set_meta("ended_at", format_utc(ensure_utc(clock())))
        report = build_observe_report(store)
        write_observe_report(folder, report)
        _write_manifest(folder, store, "completed")
    finally:
        store.close()
    return folder


async def _run_live(
    store: ObserveStore,
    settings: ObserveSettings,
    folder: Path,
    ends: datetime,
    clock: Any,
    pause: Any,
) -> None:
    import aiohttp

    from paper_desk.observe.limits import MinuteBudget
    from paper_desk.observe.providers import LiveFeeds

    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        feeds = LiveFeeds(
            session,
            gecko_budget=MinuteBudget(settings.gecko_per_minute),
            rugcheck_budget=MinuteBudget(settings.rugcheck_per_minute),
            goplus_budget=MinuteBudget(settings.goplus_per_minute),
        )
        await _loop(store, settings, folder, ends, feeds, clock, pause, live=feeds)


async def _loop(
    store: ObserveStore,
    settings: ObserveSettings,
    folder: Path,
    ends: datetime,
    feeds: Feeds,
    clock: Any,
    pause: Any,
    live: Any = None,
) -> None:
    while True:
        now = ensure_utc(clock())
        if now >= ends:
            return
        await _cycle(store, settings, feeds, now)
        _flush_calls(store, live, now)
        _write_heartbeat(folder, store, now)
        remaining = (ends - ensure_utc(clock())).total_seconds()
        if remaining <= 0:
            return
        await pause(min(settings.poll_seconds, remaining))


async def _cycle(
    store: ObserveStore, settings: ObserveSettings, feeds: Feeds, now: datetime
) -> None:
    index = int(store.get_meta("rotation") or "0")
    network, chain, kind = ROTATION[index % len(ROTATION)]
    store.set_meta("rotation", str(index + 1))
    try:
        rows = await (
            feeds.trending(network) if kind == "trending" else feeds.new_pools(network)
        )
    except ReadError as exc:
        _record_read_error(store, now, exc)
        store.add_coverage(
            now,
            chain=chain,
            list_name=kind,
            returned_rows=0,
            address_drops=0,
            scout_signals=0,
            sniper_candidates=0,
            enriched=0,
            not_enriched=0,
        )
        return
    parsed: list[PoolView] = []
    drops = 0
    for row in rows:
        pool, _reason = parse_gecko_pool(network, row, fetched_at=now, list_name=kind)
        if pool is None:
            drops += 1
        else:
            parsed.append(pool)
    queue: list[PoolView] = []
    scout_hits = 0
    sniper_candidates = 0
    for pool in parsed:
        if kind == "new":
            curve, evidence = curve_for_dex(pool.dex_id)
            if curve == "ungraduated":
                _decision(
                    store,
                    now,
                    pool,
                    side="veto",
                    reason="curve_ungraduated",
                    votes=[],
                    vetoes=["curve_ungraduated"],
                    provenance={
                        "dex_id": pool.dex_id,
                        "curve_evidence": evidence,
                        "clears_filters": False,
                    },
                )
                continue
            queue.append(pool)
            sniper_candidates += 1
        elif scout_reason(pool, settings):
            queue.append(pool)
            scout_hits += 1
    chosen = queue[: settings.enrich_per_cycle]
    deferred = queue[settings.enrich_per_cycle :]
    for pool in deferred:
        signal = scout_reason(pool, settings) if kind == "trending" else None
        if signal:
            store.upsert_vote(pool.chain, pool.contract, "scout", now, signal)
        _decision(
            store,
            now,
            pool,
            side="signal" if signal else "veto",
            reason=signal or "security_not_fetched",
            votes=["scout"] if signal else [],
            vetoes=["security_not_fetched"],
            provenance={"clears_filters": False, "list": kind},
        )
    for pool in chosen:
        await _enrich(store, settings, feeds, pool, now, kind)
    if kind == "trending":
        scout_hits = sum(1 for pool in parsed if scout_reason(pool, settings))
    store.add_coverage(
        now,
        chain=chain,
        list_name=kind,
        returned_rows=len(rows),
        address_drops=drops,
        scout_signals=scout_hits,
        sniper_candidates=sniper_candidates,
        enriched=len(chosen),
        not_enriched=len(deferred),
    )


async def _enrich(
    store: ObserveStore,
    settings: ObserveSettings,
    feeds: Feeds,
    pool: PoolView,
    now: datetime,
    kind: str,
) -> None:
    risk: RiskView | None = None
    try:
        if pool.chain == "sol":
            report = await feeds.rugcheck(pool.contract)
            risk = risk_from_rugcheck(report, pool.pool_address, now)
        else:
            row = await feeds.goplus(pool.chain, pool.contract)
            risk = risk_from_goplus(row, now)
    except ReadError as exc:
        _record_read_error(store, now, exc)
    reasons = vetoes(pool, risk, settings, now)
    signal = scout_reason(pool, settings) if kind == "trending" else None
    if signal:
        store.upsert_vote(pool.chain, pool.contract, "scout", now, signal)
        _decision(
            store,
            now,
            pool,
            side="signal",
            reason=signal,
            votes=["scout"],
            vetoes=reasons,
            provenance=_provenance(pool, risk, reasons),
        )
    if kind == "new" and not reasons:
        store.upsert_vote(pool.chain, pool.contract, "sniper", now, "clean_observation")
        _decision(
            store,
            now,
            pool,
            side="signal",
            reason="clean_observation",
            votes=["sniper"],
            vetoes=[],
            provenance=_provenance(pool, risk, []),
        )
    elif reasons:
        _decision(
            store,
            now,
            pool,
            side="veto",
            reason=reasons[0],
            votes=["scout"] if signal else [],
            vetoes=reasons,
            provenance=_provenance(pool, risk, reasons),
        )
    fresh = _fresh_agents(
        store, pool.chain, pool.contract, now, settings.vote_ttl_seconds
    )
    if not reasons and {"scout", "sniper"} <= fresh:
        _decision(
            store,
            now,
            pool,
            side="would_enter_not_traded",
            reason="filters_passed_not_traded",
            votes=sorted(fresh),
            vetoes=[],
            provenance=_provenance(pool, risk, []),
        )


def _record_read_error(store: ObserveStore, now: datetime, exc: ReadError) -> None:
    if exc.recorded:
        return
    store.add_call(
        now,
        exc.provider,
        exc.endpoint,
        http_status=exc.status,
        ok=False,
        error=exc.detail,
        rows=None,
        elapsed_ms=None,
    )


def _note_agents(store: ObserveStore, now: datetime) -> None:
    store.add_decision(
        dedup_key="agent:whale",
        ts=now,
        chain=None,
        contract=None,
        side="unavailable",
        reason="whale_unavailable_no_free_smart_money_feed",
        votes=[],
        vetoes=["whale_unavailable"],
        price=None,
        provenance={
            "agent": "whale",
            "replaces_quorum": False,
            "note": "Momentum and paid boosts are not a smart-money vote.",
        },
    )
    store.add_decision(
        dedup_key="agent:exit",
        ts=now,
        chain=None,
        contract=None,
        side="idle",
        reason="exit_idle_no_positions",
        votes=[],
        vetoes=[],
        price=None,
        provenance={"agent": "exit", "positions": 0},
    )


def _decision(
    store: ObserveStore,
    now: datetime,
    pool: PoolView,
    *,
    side: str,
    reason: str,
    votes: list[str],
    vetoes: list[str],
    provenance: dict[str, Any],
) -> None:
    price = format(pool.price_usd, "f") if pool.price_usd is not None else None
    store.add_decision(
        dedup_key=f"{pool.chain}:{pool.contract}:{side}:{reason}",
        ts=now,
        chain=pool.chain,
        contract=pool.contract,
        side=side,
        reason=reason,
        votes=votes,
        vetoes=vetoes,
        price=price,
        provenance=provenance,
    )


def _provenance(
    pool: PoolView, risk: RiskView | None, reasons: list[str]
) -> dict[str, Any]:
    curve, evidence = curve_for_dex(pool.dex_id)
    payload: dict[str, Any] = {
        "source_price": "geckoterminal",
        "list": pool.list_name,
        "dex_id": pool.dex_id,
        "pool_address": pool.pool_address,
        "fetched_at": format_utc(pool.fetched_at),
        "curve": curve,
        "curve_evidence": evidence,
        "clears_filters": not reasons,
        "liquidity_usd": format(pool.liquidity_usd, "f")
        if pool.liquidity_usd is not None
        else None,
    }
    if risk is not None:
        payload.update(
            {
                "risk_provider": risk.provider,
                "risk_fetched_at": format_utc(risk.fetched_at)
                if risk.fetched_at
                else None,
                "lock_ratio": format(risk.lock_ratio, "f")
                if risk.lock_ratio is not None
                else None,
                "lock_ratio_source": risk.lock_ratio_source,
                "lock_kind": risk.lock_kind,
                "lock_expiry": risk.lock_expiry,
                "dev_team_hold_rate": (
                    format(risk.dev_team_hold_rate, "f")
                    if risk.dev_team_hold_rate is not None
                    else None
                ),
                "dev_team_source": risk.dev_team_source,
                "bundler_rate": format(risk.bundler_rate, "f")
                if risk.bundler_rate is not None
                else None,
                "bundler_source": risk.bundler_source,
                "honeypot": risk.honeypot,
                "creator_percent_ignored": (
                    format(risk.creator_percent_ignored, "f")
                    if risk.creator_percent_ignored is not None
                    else None
                ),
                "insider_count_ignored": risk.insider_count_ignored,
                "field_notes": risk.field_notes,
            }
        )
    return payload


def _fresh_agents(
    store: ObserveStore, chain: str, contract: str, now: datetime, ttl: int
) -> set[str]:
    found: set[str] = set()
    for row in store.votes():
        if row["chain"] != chain or row["contract"] != contract:
            continue
        if row["agent"] not in {"scout", "sniper"}:
            continue
        age = (now - parse_utc(str(row["ts"]))).total_seconds()
        if -5 <= age <= ttl:
            found.add(str(row["agent"]))
    return found


def _flush_calls(store: ObserveStore, live: Any, now: datetime) -> None:
    if live is None:
        return
    pending = list(live.calls)
    live.calls.clear()
    for call in pending:
        store.add_call(
            now,
            str(call["provider"]),
            str(call["endpoint"]),
            http_status=call["http_status"],
            ok=bool(call["ok"]),
            error=call["error"],
            rows=call["rows"],
            elapsed_ms=call["elapsed_ms"],
        )


def _write_heartbeat(folder: Path, store: ObserveStore, now: datetime) -> None:
    store.set_meta("heartbeat_at", format_utc(now))
    health = {
        "mode": "OBSERVATION_ONLY",
        "session_id": store.get_meta("session_id"),
        "beat_utc": format_utc(now),
        "beat_et": format_et(now),
        "open_positions": 0,
        "orders_sent": 0,
        "agents": {
            "whale": "unavailable",
            "exit": "idle",
            "scout": "sampling",
            "sniper": "sampling",
            "shield": "veto_only",
        },
        "virtual_balance": "unused_not_deployed",
    }
    (folder / "health.json").write_text(
        json.dumps(health, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (folder / "status.json").write_text(
        json.dumps(
            {
                "session_id": store.get_meta("session_id"),
                "status": "running",
                "beat_utc": format_utc(now),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (folder / "checkpoint.json").write_text(
        json.dumps(
            {"beat_utc": format_utc(now), "rotation": store.get_meta("rotation")},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    _write_manifest(folder, store, "running")


def _write_manifest(folder: Path, store: ObserveStore, status: str) -> None:
    payload = {
        "mode": "OBSERVATION_ONLY",
        "session_id": store.get_meta("session_id"),
        "status": status,
        "started_at_utc": store.get_meta("started_at"),
        "ends_at_utc": store.get_meta("ends_at"),
        "heartbeat_utc": store.get_meta("heartbeat_at"),
        "pid": os.getpid(),
        "live_trading": False,
        "orders_sent": 0,
        "positions": 0,
        "virtual_balance": "unused_not_deployed",
        "credentials_recorded": False,
    }
    (folder / "manifest.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
