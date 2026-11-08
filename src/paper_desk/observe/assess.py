"""Map public payloads onto the existing veto rules.

Creator-wallet percent is stored and ignored. An insider count is stored
and ignored. A burned or unscanned LP percentage is not a verified lock.
Missing fields stay unknown.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from paper_desk.numbers import dec
from paper_desk.observe.config import ObserveSettings
from paper_desk.security import honeypot_state
from paper_desk.signals import normalize_contract
from paper_desk.timeutil import ensure_utc

NETWORKS = {"solana": "sol", "base": "base", "bsc": "bsc"}
# Dex ids seen on the keyless GeckoTerminal smoke. Anything else is unknown.
_BONDING_CURVE_DEX = frozenset({"pump-fun"})
_AMM_DEX = frozenset(
    {
        "uniswap-v4-base",
        "pancakeswap_v2",
        "meteora",
        "meteora-damm-v2",
        "pumpswap",
        # Seen on a keyless solana trending_pools read. Not the pump-fun curve.
        "raydium",
    }
)
_BURN_WORDS = frozenset({"burn", "burned", "burnt"})
_LOCK_WORDS = frozenset({"lock", "locker", "vesting", "locked"})
_HUNDRED = Decimal(100)


@dataclass
class PoolView:
    """One pool row from a public list."""

    chain: str
    contract: str
    pool_address: str
    symbol: str
    dex_id: str
    price_usd: Decimal | None
    liquidity_usd: Decimal | None
    change_m5: Decimal | None
    volume_m5: Decimal | None
    change_h1: Decimal | None
    volume_h1: Decimal | None
    fetched_at: datetime
    list_name: str


@dataclass
class RiskView:
    """Security fields this mode is willing to use, plus ignored evidence."""

    lock_ratio: Decimal | None = None
    lock_ratio_source: str | None = None
    lock_kind: str = "missing"
    lock_expiry: str = "missing"
    dev_team_hold_rate: Decimal | None = None
    dev_team_source: str | None = None
    bundler_rate: Decimal | None = None
    bundler_source: str | None = None
    honeypot: str = "missing"
    honeypot_source: str | None = None
    creator_percent_ignored: Decimal | None = None
    insider_count_ignored: int | None = None
    fetched_at: datetime | None = None
    provider: str = ""
    field_notes: dict[str, str] = field(default_factory=dict)


def parse_lp_locked_pct(value: object) -> Decimal | None:
    """RugCheck `lpLockedPct` as a 0-1 ratio.

    Live reports returned 0 and 100, and the OpenAPI types the field as a
    number named Pct. 100 means fully locked. Values outside 0..100 are
    unusable rather than rescaled.
    """
    parsed = dec(value)
    if parsed is None or parsed < 0 or parsed > _HUNDRED:
        return None
    return parsed / _HUNDRED


def curve_for_dex(dex_id: str) -> tuple[str, str]:
    """Bonding-curve venues are ungraduated. Unknown dex ids stay unknown."""
    if dex_id in _BONDING_CURVE_DEX:
        return "ungraduated", f"geckoterminal dex {dex_id} is a bonding curve"
    if dex_id in _AMM_DEX:
        return "graduated", f"geckoterminal dex {dex_id} is an AMM pool"
    return "unknown", f"geckoterminal dex {dex_id} is not in the checked set"


def parse_gecko_pool(
    network: str,
    pool: object,
    *,
    fetched_at: datetime,
    list_name: str,
) -> tuple[PoolView | None, str]:
    """Read one GeckoTerminal pool. The second value is a drop reason or empty."""
    chain = NETWORKS.get(network)
    if chain is None or not isinstance(pool, dict):
        return None, "unsupported_network_or_row"
    relationships = pool.get("relationships")
    attributes = pool.get("attributes")
    if not isinstance(relationships, dict) or not isinstance(attributes, dict):
        return None, "pool_shape"
    base = _nested_id(relationships, "base_token")
    if base is None or "_" not in base:
        return None, "base_token_id"
    _prefix, address = base.split("_", 1)
    contract = normalize_contract(chain, address)
    if contract is None:
        return None, "address_shape"
    dex_id = _nested_id(relationships, "dex") or ""
    name = attributes.get("name")
    symbol = name.split(" / ", 1)[0] if isinstance(name, str) else ""
    volume = attributes.get("volume_usd")
    change = attributes.get("price_change_percentage")
    volume = volume if isinstance(volume, dict) else {}
    change = change if isinstance(change, dict) else {}
    pool_address = attributes.get("address")
    return (
        PoolView(
            chain=chain,
            contract=contract,
            pool_address=pool_address if isinstance(pool_address, str) else "",
            symbol=symbol,
            dex_id=dex_id,
            price_usd=dec(attributes.get("base_token_price_usd")),
            liquidity_usd=dec(attributes.get("reserve_in_usd")),
            change_m5=dec(change.get("m5")),
            volume_m5=dec(volume.get("m5")),
            change_h1=dec(change.get("h1")),
            volume_h1=dec(volume.get("h1")),
            fetched_at=ensure_utc(fetched_at),
            list_name=list_name,
        ),
        "",
    )


def scout_reason(pool: PoolView, settings: ObserveSettings) -> str | None:
    """Volume spike or gainer. A missing change or volume is not a signal."""
    if (
        pool.change_m5 is not None
        and pool.volume_m5 is not None
        and pool.change_m5 >= settings.scout_min_spike_pct
        and pool.volume_m5 >= settings.scout_min_spike_volume_usd
    ):
        return "volume_spike"
    if (
        pool.change_h1 is not None
        and pool.volume_h1 is not None
        and pool.change_h1 >= settings.scout_min_gainer_pct
        and pool.volume_h1 >= settings.scout_min_gainer_volume_usd
    ):
        return "top_gainer"
    return None


def risk_from_rugcheck(
    report: object, pool_address: str, fetched_at: datetime
) -> RiskView:
    """Solana report. Lock percent is read; locker evidence is separate."""
    view = RiskView(fetched_at=ensure_utc(fetched_at), provider="rugcheck")
    if not isinstance(report, dict):
        view.field_notes["report"] = "not_an_object"
        return view
    market = _rugcheck_market(report.get("markets"), pool_address)
    if isinstance(market, dict) and isinstance(market.get("lp"), dict):
        ratio = parse_lp_locked_pct(market["lp"].get("lpLockedPct"))
        view.lock_ratio = ratio
        if "lpLockedPct" in market["lp"]:
            view.lock_ratio_source = "rugcheck.markets.lp.lpLockedPct"
            if ratio is None:
                view.field_notes["lpLockedPct"] = "present_but_unusable_scale"
    _apply_lockers(
        view,
        report.get("lockers"),
        report.get("lockerScanStatus"),
        ensure_utc(fetched_at),
    )
    explicit = _explicit_ratio(report, ("dev_team_hold_rate",))
    view.dev_team_hold_rate = explicit
    if explicit is not None:
        view.dev_team_source = "rugcheck.dev_team_hold_rate"
    creator = dec(report.get("creatorBalance"))
    if creator is not None:
        view.field_notes["creatorBalance"] = "ignored_not_dev_team_rate"
    bundle = _explicit_ratio(report, ("bundler_rate", "bundled_supply_ratio"))
    view.bundler_rate = bundle
    if bundle is not None:
        view.bundler_source = "rugcheck.bundler_rate"
    insiders = report.get("graphInsidersDetected")
    if isinstance(insiders, int) and not isinstance(insiders, bool):
        view.insider_count_ignored = insiders
        view.field_notes["graphInsidersDetected"] = "ignored_not_bundled_supply"
    if "is_honeypot" in report:
        view.honeypot = honeypot_state(report.get("is_honeypot"))
        view.honeypot_source = "rugcheck.is_honeypot"
    else:
        view.honeypot = "missing"
        view.honeypot_source = "rugcheck_has_no_honeypot_field"
    return view


def risk_from_goplus(row: object, fetched_at: datetime) -> RiskView:
    """EVM token-security row. Creator percent is not a dev-team rate."""
    view = RiskView(fetched_at=ensure_utc(fetched_at), provider="goplus")
    if not isinstance(row, dict):
        view.field_notes["row"] = "not_an_object"
        return view
    if "is_honeypot" in row:
        view.honeypot = honeypot_state(row.get("is_honeypot"))
        view.honeypot_source = "goplus.is_honeypot"
    explicit = _explicit_ratio(row, ("dev_team_hold_rate",))
    view.dev_team_hold_rate = explicit
    if explicit is not None:
        view.dev_team_source = "goplus.dev_team_hold_rate"
    for ignored in ("creator_percent", "owner_percent"):
        parsed = dec(row.get(ignored))
        if parsed is not None:
            view.creator_percent_ignored = parsed
            view.field_notes[ignored] = "ignored_not_dev_team_rate"
    bundle = _explicit_ratio(row, ("bundler_rate", "bundled_supply_ratio"))
    view.bundler_rate = bundle
    if bundle is not None:
        view.bundler_source = "goplus.bundler_rate"
    if "lpLockedPct" in row:
        view.lock_ratio = parse_lp_locked_pct(row.get("lpLockedPct"))
        view.lock_ratio_source = "goplus.lpLockedPct"
    if "lockers" in row or "lockerScanStatus" in row:
        _apply_lockers(
            view,
            row.get("lockers"),
            row.get("lockerScanStatus"),
            ensure_utc(fetched_at),
        )
    else:
        view.field_notes.setdefault(
            "lockers", "goplus_token_security_has_no_locker_list"
        )
        view.lock_kind = "missing"
        view.lock_expiry = "missing"
    return view


def vetoes(
    pool: PoolView,
    risk: RiskView | None,
    settings: ObserveSettings,
    now: datetime,
) -> list[str]:
    """Would-be entry vetoes. An empty list is not a trade."""
    reasons: list[str] = []
    now_utc = ensure_utc(now)
    curve, _evidence = curve_for_dex(pool.dex_id)
    if curve != "graduated":
        reasons.append(f"curve_{curve}")
    if pool.price_usd is None or pool.price_usd <= 0:
        reasons.append("quote_price_invalid")
    elif _stale(pool.fetched_at, now_utc, settings.quote_max_age_seconds):
        reasons.append("quote_stale")
    if pool.liquidity_usd is None or pool.liquidity_usd < settings.min_liquidity_usd:
        reasons.append("liquidity_unknown_or_thin")
    if risk is None:
        reasons.append("security_not_fetched")
        return reasons
    if risk.fetched_at is None or _stale(
        risk.fetched_at, now_utc, settings.security_max_age_seconds
    ):
        reasons.append("security_stale")
    if risk.honeypot == "yes":
        reasons.append("honeypot")
    elif risk.honeypot != "no":
        reasons.append("honeypot_unknown")
    reasons.extend(_lock_vetoes(risk, settings))
    if risk.dev_team_hold_rate is None:
        reasons.append("dev_holdings_unknown")
    elif risk.dev_team_hold_rate >= settings.max_dev_hold_rate:
        reasons.append("dev_holdings_not_under_5pct")
    if risk.bundler_rate is None:
        reasons.append("bundled_supply_unknown")
    elif risk.bundler_rate != 0:
        reasons.append("bundled_supply_not_zero")
    return reasons


def _lock_vetoes(risk: RiskView, settings: ObserveSettings) -> list[str]:
    if risk.lock_kind == "burn":
        return ["liquidity_burned_not_locked"]
    if risk.lock_ratio is None:
        return ["liquidity_lock_unknown"]
    if risk.lock_kind != "locker":
        return ["liquidity_lock_pct_without_locker"]
    if risk.lock_expiry not in {"permanent", "future"}:
        return ["lock_expiry_unknown"]
    if risk.lock_ratio < settings.min_lock_percent:
        return ["liquidity_not_locked"]
    return []


def _apply_lockers(
    view: RiskView, lockers: object, scan_status: object, now: datetime
) -> None:
    records = _locker_records(lockers)
    status = scan_status if isinstance(scan_status, str) else ""
    view.field_notes["lockerScanStatus"] = status or "missing"
    if not records or status in {"", "none"}:
        view.lock_kind = "pct_only" if view.lock_ratio is not None else "missing"
        view.lock_expiry = "unknown" if view.lock_ratio is not None else "missing"
        return
    kinds = [_locker_kind(item) for item in records]
    if kinds and all(kind == "burn" for kind in kinds):
        view.lock_kind = "burn"
        view.lock_expiry = "not_applicable"
        return
    if any(kind == "unknown" for kind in kinds):
        view.lock_kind = "unknown"
        view.lock_expiry = "unknown"
        return
    expiries = [
        _locker_expiry(item, now) for item in records if _locker_kind(item) == "locker"
    ]
    view.lock_kind = "locker"
    if not expiries or any(item == "unknown" for item in expiries):
        view.lock_expiry = "unknown"
    elif all(item == "permanent" for item in expiries):
        view.lock_expiry = "permanent"
    elif all(item in {"permanent", "future"} for item in expiries):
        view.lock_expiry = "future"
    else:
        view.lock_expiry = "expired"


def _locker_records(lockers: object) -> list[dict[str, object]]:
    if isinstance(lockers, dict):
        values = list(lockers.values())
    elif isinstance(lockers, list):
        values = lockers
    else:
        return []
    return [item for item in values if isinstance(item, dict)]


def _locker_kind(item: dict[str, object]) -> str:
    raw = item.get("type", item.get("kind", ""))
    text = raw.strip().lower() if isinstance(raw, str) else ""
    if text in _BURN_WORDS:
        return "burn"
    if text in _LOCK_WORDS:
        return "locker"
    return "unknown"


def _locker_expiry(item: dict[str, object], now: datetime) -> str:
    if item.get("permanent") is True:
        return "permanent"
    now_ts = Decimal(int(now.timestamp()))
    for key in ("unlockDate", "unlock_date", "unlock_ts"):
        if key not in item:
            continue
        parsed = dec(item.get(key))
        if parsed is None:
            return "unknown"
        if parsed > now_ts:
            return "future"
        return "expired"
    return "unknown"


def _explicit_ratio(
    payload: dict[str, object], keys: tuple[str, ...]
) -> Decimal | None:
    for key in keys:
        if key not in payload:
            continue
        parsed = dec(payload.get(key))
        if parsed is None or parsed < 0 or parsed > 1:
            return None
        return parsed
    return None


def _rugcheck_market(markets: object, pool_address: str) -> dict[str, object] | None:
    if not isinstance(markets, list):
        return None
    rows = [item for item in markets if isinstance(item, dict)]
    if pool_address:
        for item in rows:
            if item.get("pubkey") == pool_address:
                return item
        return None
    if len(rows) == 1:
        return rows[0]
    return None


def _nested_id(relationships: dict[str, object], name: str) -> str | None:
    node = relationships.get(name)
    if not isinstance(node, dict):
        return None
    data = node.get("data")
    if not isinstance(data, dict):
        return None
    ident = data.get("id")
    if not isinstance(ident, str) or not ident:
        return None
    return ident


def _stale(fetched_at: datetime, now: datetime, max_age: int) -> bool:
    age = (now - ensure_utc(fetched_at)).total_seconds()
    return age > max_age or age < -5
