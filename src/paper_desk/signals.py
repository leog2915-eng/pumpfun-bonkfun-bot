"""Scout and whale signal rules. They vote; they do not clear security."""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal

from paper_desk.config import Settings
from paper_desk.numbers import dec
from paper_desk.timeutil import ensure_utc

_SOL = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
_EVM = re.compile(r"^0x[0-9a-fA-F]{40}$")


def normalize_contract(chain: str, address: str) -> str | None:
    """Canonical contract key. EVM addresses are lowercased."""
    text = address.strip()
    if chain == "sol":
        if _SOL.fullmatch(text) is None:
            return None
        return text
    if chain in {"base", "bsc"}:
        if _EVM.fullmatch(text) is None:
            return None
        return text.lower()
    return None


def scout_hits(
    rows: list[object],
    *,
    chain: str,
    role: str,
    settings: Settings,
) -> list[dict[str, object]]:
    """Bullish rank rows.

    `price_change_percent` is read as percent points: the GMGN heat-rank
    skill prints the raw number with a `%` suffix and cites values such as
    +128168.0. Rows are skipped when price, volume, or the change is missing.
    """
    hits: list[dict[str, object]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        address = row.get("address")
        if not isinstance(address, str):
            continue
        contract = normalize_contract(chain, address)
        if contract is None:
            continue
        change = dec(row.get("price_change_percent"))
        volume = dec(row.get("volume"))
        price = dec(row.get("price"))
        if change is None or volume is None or price is None or price <= 0:
            continue
        if role == "spike":
            if (
                change < settings.scout_min_spike_pct
                or volume < settings.scout_min_spike_volume_usd
            ):
                continue
            reason = "volume_spike"
        elif role == "gainer":
            if (
                change < settings.scout_min_gainer_pct
                or volume < settings.scout_min_gainer_volume_usd
            ):
                continue
            reason = "top_gainer"
        else:
            continue
        symbol = row.get("symbol") if isinstance(row.get("symbol"), str) else ""
        hits.append(
            {
                "chain": chain,
                "contract": contract,
                "symbol": symbol,
                "reason": reason,
                "price": price,
                "change_pct": change,
                "volume_usd": volume,
            }
        )
    return hits


def whale_buys(
    rows: list[object],
    *,
    chain: str,
    now: datetime,
    settings: Settings,
) -> list[dict[str, object]]:
    """Recent smart-money buys with a hash, maker, price, and timestamp.

    Rows missing any of those are dropped. The feed is not treated as a buy
    when `side` is absent.
    """
    now_utc = ensure_utc(now)
    kept: list[dict[str, object]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("side") != "buy":
            continue
        tx_hash = row.get("transaction_hash")
        maker = row.get("maker")
        address = row.get("base_address")
        if (
            not isinstance(tx_hash, str)
            or not isinstance(maker, str)
            or not isinstance(address, str)
            or not tx_hash
            or not maker
            or not address
        ):
            continue
        contract = normalize_contract(chain, address)
        maker_key = maker if chain == "sol" else maker.lower()
        if contract is None or not maker_key:
            continue
        amount = dec(row.get("amount_usd"))
        price = dec(row.get("price_usd"))
        stamp = dec(row.get("timestamp"))
        if (
            amount is None
            or price is None
            or stamp is None
            or amount < settings.whale_min_buy_usd
            or price <= 0
        ):
            continue
        traded_at = datetime.fromtimestamp(int(stamp), tz=now_utc.tzinfo)
        age = (now_utc - traded_at).total_seconds()
        if age < -5 or age > settings.whale_window_seconds:
            continue
        symbol = ""
        base = row.get("base_token")
        if isinstance(base, dict) and isinstance(base.get("symbol"), str):
            symbol = base["symbol"]
        kept.append(
            {
                "chain": chain,
                "contract": contract,
                "symbol": symbol,
                "maker": maker_key,
                "tx_hash": tx_hash,
                "amount_usd": amount,
                "price": price,
                "traded_at": traded_at,
            }
        )
    return kept


def distinct_makers(buys: list[dict[str, object]]) -> set[str]:
    """Wallet set for one token."""
    return {str(item["maker"]) for item in buys}


def cluster_ready(buys: list[dict[str, object]], settings: Settings) -> bool:
    """True when enough distinct wallets bought this token inside the window."""
    return len(distinct_makers(buys)) >= settings.whale_min_wallets


def reference_price(buys: list[dict[str, object]]) -> Decimal:
    """Latest buy price in the cluster. The fill still needs its own quote."""
    ordered = sorted(buys, key=lambda item: item["traded_at"], reverse=True)
    price = ordered[0]["price"]
    if not isinstance(price, Decimal):
        msg = "cluster price missing"
        raise TypeError(msg)
    return price
