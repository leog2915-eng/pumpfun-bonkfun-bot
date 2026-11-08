"""Security clearance. Missing, stale, or contradictory evidence is a veto."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from paper_desk.config import Settings
from paper_desk.numbers import dec
from paper_desk.timeutil import ensure_utc

CURVE_UNGRADUATED = "ungraduated"
CURVE_GRADUATED = "graduated"
CURVE_UNKNOWN = "unknown"


@dataclass(frozen=True)
class Quote:
    """One priced market read."""

    chain: str
    contract: str
    price_usd: Decimal
    liquidity_usd: Decimal | None
    fee_ratio_raw: str | None
    fetched_at: datetime
    source: str
    origin: str


@dataclass(frozen=True)
class SecurityView:
    """Fields this desk is willing to read, and nothing inferred beyond them."""

    chain: str
    contract: str
    fetched_at: datetime
    origin: str
    lock_percent: Decimal | None
    dev_team_hold_rate: Decimal | None
    bundler_rate: Decimal | None
    honeypot: str
    curve: str
    curve_evidence: str
    fetch_error: str | None
    field_sources: dict[str, str]


def honeypot_state(value: object) -> str:
    """Map a documented honeypot flag to yes, no, empty, missing, or unknown."""
    if value is None:
        return "missing"
    if isinstance(value, str):
        text = value.strip().lower()
        if text == "":
            return "empty"
        if text in {"yes", "1", "true"}:
            return "yes"
        if text in {"no", "0", "false"}:
            return "no"
        return "unknown"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int) and value in {0, 1}:
        return "yes" if value == 1 else "no"
    return "unknown"


def _ratio(value: object) -> Decimal | None:
    """A documented 0-1 ratio. Values outside that range are not usable."""
    parsed = dec(value)
    if parsed is None or parsed < 0 or parsed > 1:
        return None
    return parsed


def _pick_ratio(
    documents: list[tuple[str, dict[str, Any]]],
    paths: tuple[tuple[str, ...], ...],
) -> tuple[Decimal | None, str | None]:
    for source, doc in documents:
        for path in paths:
            node: object = doc
            ok = True
            for key in path:
                if not isinstance(node, dict) or key not in node:
                    ok = False
                    break
                node = node[key]
            if not ok:
                continue
            parsed = _ratio(node)
            if parsed is None:
                continue
            label = source + "." + ".".join(path)
            return parsed, label
    return None, None


def build_security_view(
    *,
    chain: str,
    contract: str,
    fetched_at: datetime,
    origin: str,
    curve: str,
    curve_evidence: str,
    documents: list[tuple[str, dict[str, Any]]],
    fetch_error: str | None = None,
) -> SecurityView:
    """Read lock, dev holdings, bundle ratio, and honeypot from known paths.

    `bundler_trader_amount_rate` is a volume ratio in the GMGN docs and is
    not accepted as bundled supply. `locked_ratio` is supply locked, not LP.
    """
    lock_percent, lock_src = _pick_ratio(documents, (("lock_percent",),))
    dev_rate, dev_src = _pick_ratio(
        documents,
        (("dev_team_hold_rate",), ("stat", "dev_team_hold_rate")),
    )
    bundler, bundler_src = _pick_ratio(documents, (("bundler_rate",),))
    honeypot = "missing"
    honeypot_src = ""
    for source, doc in documents:
        if "is_honeypot" in doc:
            honeypot = honeypot_state(doc.get("is_honeypot"))
            honeypot_src = source + ".is_honeypot"
            break
    sources = {
        key: value
        for key, value in (
            ("lock_percent", lock_src),
            ("dev_team_hold_rate", dev_src),
            ("bundler_rate", bundler_src),
            ("is_honeypot", honeypot_src),
        )
        if value
    }
    return SecurityView(
        chain=chain,
        contract=contract,
        fetched_at=ensure_utc(fetched_at),
        origin=origin,
        lock_percent=lock_percent,
        dev_team_hold_rate=dev_rate,
        bundler_rate=bundler,
        honeypot=honeypot,
        curve=curve,
        curve_evidence=curve_evidence,
        fetch_error=fetch_error,
        field_sources=sources,
    )


def _age_ok(fetched_at: datetime, now: datetime, max_age: int) -> bool:
    age = (ensure_utc(now) - ensure_utc(fetched_at)).total_seconds()
    return -5 <= age <= max_age


def shield_vetoes(
    view: SecurityView,
    quote: Quote,
    settings: Settings,
    now: datetime,
) -> list[str]:
    """Entry clearance. Shield is not an entry vote; these are vetoes."""
    vetoes: list[str] = []
    if settings.mode != "synthetic":
        if view.origin == "synthetic" or quote.origin == "synthetic":
            vetoes.append("synthetic_data_refused")
    if view.fetch_error:
        vetoes.append("security_fetch_failed")
    if view.chain != quote.chain or view.contract != quote.contract:
        vetoes.append("security_quote_mismatch")
    if view.chain not in settings.chains:
        vetoes.append("chain_unsupported")
    if not _age_ok(view.fetched_at, now, settings.security_max_age_seconds):
        vetoes.append("security_stale")
    if not _age_ok(quote.fetched_at, now, settings.quote_max_age_seconds):
        vetoes.append("quote_stale")
    if quote.price_usd <= 0:
        vetoes.append("quote_price_invalid")
    if quote.liquidity_usd is None or quote.liquidity_usd < settings.min_liquidity_usd:
        vetoes.append("liquidity_unknown_or_thin")
    if view.chain in {"base", "bsc"} and view.honeypot != "no":
        vetoes.append("honeypot" if view.honeypot == "yes" else "honeypot_unknown")
    elif view.honeypot == "yes":
        vetoes.append("honeypot")
    return vetoes


def sniper_blocks(view: SecurityView, settings: Settings) -> list[str]:
    """Launch filters. Ungraduated curves are blocked even if other fields pass."""
    blocks: list[str] = []
    if view.curve != CURVE_GRADUATED:
        blocks.append(f"curve_{view.curve}")
    if view.lock_percent is None:
        blocks.append("liquidity_lock_unknown")
    elif view.lock_percent < settings.min_lock_percent:
        blocks.append("liquidity_not_locked")
    if view.dev_team_hold_rate is None:
        blocks.append("dev_holdings_unknown")
    elif view.dev_team_hold_rate >= settings.max_dev_hold_rate:
        blocks.append("dev_holdings_not_under_5pct")
    if view.bundler_rate is None:
        blocks.append("bundled_supply_unknown")
    elif view.bundler_rate != 0:
        blocks.append("bundled_supply_not_zero")
    return blocks
