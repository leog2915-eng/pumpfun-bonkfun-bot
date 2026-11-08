"""One bounded keyless smoke. It does not start a 24h session."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp

from paper_desk.observe.assess import parse_gecko_pool
from paper_desk.observe.limits import MinuteBudget
from paper_desk.observe.providers import LiveFeeds, ReadError
from paper_desk.timeutil import utc_now


async def observe_preflight() -> dict[str, Any]:
    """Read one trending list, one new-pool list, and at most two risk calls."""
    report: dict[str, Any] = {
        "profile": "free-observe",
        "credentials_required": [],
        "credentials_recorded": False,
        "twenty_four_hour_session": "not_started",
        "orders_sent": 0,
        "live_trading": False,
        "providers": {},
    }
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        feeds = LiveFeeds(
            session,
            gecko_budget=MinuteBudget(10),
            rugcheck_budget=MinuteBudget(10),
            goplus_budget=MinuteBudget(10),
        )
        report["providers"]["geckoterminal_solana_trending"] = await _pools(
            feeds.trending, "solana", "trending"
        )
        report["providers"]["geckoterminal_base_new_pools"] = await _pools(
            feeds.new_pools, "base", "new"
        )
        sol = _first_contract(report["providers"]["geckoterminal_solana_trending"])
        if sol:
            report["providers"]["rugcheck"] = await _rug(feeds, sol)
        base = _first_contract(report["providers"]["geckoterminal_base_new_pools"])
        if base:
            report["providers"]["goplus_base"] = await _goplus(feeds, "base", base)
    report["provider_calls"] = feeds.calls
    return report


async def _pools(
    method: Callable[[str], Awaitable[list[object]]], network: str, kind: str
) -> dict[str, Any]:
    try:
        rows = await method(network)
    except ReadError as exc:
        return {"ok": False, "error": exc.detail, "http_status": exc.status}
    parsed = 0
    sample = None
    for row in rows:
        pool, _reason = parse_gecko_pool(
            network, row, fetched_at=utc_now(), list_name=kind
        )
        if pool is None:
            continue
        parsed += 1
        if sample is None:
            sample = {
                "chain": pool.chain,
                "contract": pool.contract,
                "dex_id": pool.dex_id,
                "has_price": pool.price_usd is not None,
                "has_m5_volume": pool.volume_m5 is not None,
                "has_m5_change": pool.change_m5 is not None,
            }
    return {
        "ok": True,
        "returned_rows": len(rows),
        "parsed_rows": parsed,
        "sample": sample,
    }


async def _rug(feeds: LiveFeeds, mint: str) -> dict[str, Any]:
    try:
        report = await feeds.rugcheck(mint)
    except ReadError as exc:
        return {"ok": False, "error": exc.detail, "http_status": exc.status}
    markets = report.get("markets") if isinstance(report, dict) else None
    lp = None
    if isinstance(markets, list):
        for market in markets:
            if isinstance(market, dict) and isinstance(market.get("lp"), dict):
                lp = market["lp"]
                break
    return {
        "ok": True,
        "mint": mint,
        "lpLockedPct_present": isinstance(lp, dict) and "lpLockedPct" in lp,
        "lockers_empty": not bool(report.get("lockers")),
        "lockerScanStatus": report.get("lockerScanStatus"),
        "dev_team_hold_rate_present": "dev_team_hold_rate" in report,
        "bundler_rate_present": "bundler_rate" in report
        or "bundled_supply_ratio" in report,
        "creatorBalance_present": "creatorBalance" in report,
        "graphInsidersDetected_present": "graphInsidersDetected" in report,
    }


async def _goplus(feeds: LiveFeeds, chain: str, address: str) -> dict[str, Any]:
    try:
        row = await feeds.goplus(chain, address)
    except ReadError as exc:
        return {"ok": False, "error": exc.detail, "http_status": exc.status}
    return {
        "ok": True,
        "chain": chain,
        "contract": address,
        "is_honeypot_present": "is_honeypot" in row,
        "dev_team_hold_rate_present": "dev_team_hold_rate" in row,
        "bundler_rate_present": "bundler_rate" in row or "bundled_supply_ratio" in row,
        "creator_percent_present": "creator_percent" in row,
        "lockers_present": "lockers" in row,
    }


def _first_contract(block: object) -> str | None:
    if not isinstance(block, dict):
        return None
    sample = block.get("sample")
    if not isinstance(sample, dict):
        return None
    contract = sample.get("contract")
    return contract if isinstance(contract, str) else None
