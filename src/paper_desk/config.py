"""Paper-desk settings.

Hard limits from the operator brief are constants. YAML may set the
unspecified knobs (take-profit fractions, trail, windows, scout thresholds)
and may not loosen the hard limits.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from paper_desk.numbers import SUPPORTED_CHAINS, dec

# Fixed by the brief. A config file that disagrees is rejected at load.
MAX_POSITION_USD = Decimal("50")
MAX_OPEN_POSITIONS = 10
STOP_TRIGGER = Decimal("0.06")
MAX_DEV_HOLD_RATE = Decimal("0.05")
MIN_LOCK_PERCENT = Decimal("1")
INITIAL_CASH_USD = Decimal("500")

_HARD_TEXT = {
    "max_position_usd": "50",
    "max_open_positions": "10",
    "stop_trigger": "0.06",
    "max_dev_hold_rate": "0.05",
    "min_lock_percent": "1",
}


@dataclass(frozen=True)
class Settings:
    """Resolved paper-desk configuration."""

    mode: str
    initial_cash_usd: Decimal
    buy_usd: Decimal
    max_position_usd: Decimal
    max_open_positions: int
    stop_trigger: Decimal
    max_dev_hold_rate: Decimal
    min_lock_percent: Decimal
    tp1_multiple: Decimal
    tp1_fraction: Decimal
    tp2_multiple: Decimal
    tp2_fraction: Decimal
    trail_drawdown: Decimal
    vote_ttl_seconds: int
    quote_max_age_seconds: int
    security_max_age_seconds: int
    min_liquidity_usd: Decimal
    slippage: Decimal
    max_impact: Decimal
    default_fee_bps: int
    whale_window_seconds: int
    whale_min_wallets: int
    whale_min_buy_usd: Decimal
    scout_min_spike_pct: Decimal
    scout_min_spike_volume_usd: Decimal
    scout_min_gainer_pct: Decimal
    scout_min_gainer_volume_usd: Decimal
    scout_spike_interval: str
    scout_gainer_interval: str
    rank_limit: int
    chains: tuple[str, ...]
    listener: str
    poll_seconds: int
    mark_seconds: int
    entry_retry_seconds: int
    queue_size: int
    gmgn_plan: str
    evm_gas_units: int
    max_plausible_gas_usd: Decimal
    gas_fallback_usd: dict[str, Decimal]
    trenches_limit: int

    def to_public_dict(self) -> dict[str, Any]:
        """JSON-safe settings with no endpoints and no secrets."""
        raw: dict[str, Any] = {}
        for key, value in self.__dict__.items():
            if isinstance(value, Decimal):
                raw[key] = format(value, "f")
            elif isinstance(value, dict):
                raw[key] = {k: format(v, "f") for k, v in value.items()}
            else:
                raw[key] = value
        return raw


def default_settings(*, mode: str = "paper", cash: Decimal | None = None) -> Settings:
    """Documented defaults. `mode` is `paper` or `synthetic`."""
    if mode not in {"paper", "synthetic"}:
        msg = "mode must be paper or synthetic"
        raise ValueError(msg)
    starting = INITIAL_CASH_USD if cash is None else cash
    if starting <= 0:
        msg = "initial cash must be positive"
        raise ValueError(msg)
    return Settings(
        mode=mode,
        initial_cash_usd=starting,
        buy_usd=MAX_POSITION_USD,
        max_position_usd=MAX_POSITION_USD,
        max_open_positions=MAX_OPEN_POSITIONS,
        stop_trigger=STOP_TRIGGER,
        max_dev_hold_rate=MAX_DEV_HOLD_RATE,
        min_lock_percent=MIN_LOCK_PERCENT,
        tp1_multiple=Decimal("2"),
        tp1_fraction=Decimal("0.50"),
        tp2_multiple=Decimal("3"),
        tp2_fraction=Decimal("0.25"),
        trail_drawdown=Decimal("0.15"),
        vote_ttl_seconds=90,
        quote_max_age_seconds=20,
        security_max_age_seconds=120,
        min_liquidity_usd=Decimal("1000"),
        slippage=Decimal("0.01"),
        max_impact=Decimal("0.10"),
        default_fee_bps=30,
        whale_window_seconds=1800,
        whale_min_wallets=2,
        whale_min_buy_usd=Decimal("100"),
        scout_min_spike_pct=Decimal("15"),
        scout_min_spike_volume_usd=Decimal("25000"),
        scout_min_gainer_pct=Decimal("25"),
        scout_min_gainer_volume_usd=Decimal("10000"),
        scout_spike_interval="5m",
        scout_gainer_interval="1h",
        rank_limit=50,
        chains=("sol", "base", "bsc"),
        listener="auto",
        poll_seconds=60,
        mark_seconds=20,
        entry_retry_seconds=30,
        queue_size=256,
        gmgn_plan="free",
        evm_gas_units=300_000,
        max_plausible_gas_usd=Decimal("5"),
        gas_fallback_usd={
            "sol": Decimal("0.02"),
            "base": Decimal("0.02"),
            "bsc": Decimal("0.05"),
        },
        trenches_limit=80,
    )


def load_settings(
    path: Path | None,
    *,
    mode: str,
    cash: Decimal | None = None,
    plan: str | None = None,
) -> Settings:
    """Load YAML overrides over the defaults.

    Raises:
        ValueError: On an unknown key, a loosened hard limit, or a bad value.
    """
    base = default_settings(mode=mode, cash=cash)
    if path is None or not path.exists():
        return _with_plan(base, plan)
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if loaded is None:
        return _with_plan(base, plan)
    if not isinstance(loaded, dict):
        msg = "paper desk config must be a mapping"
        raise TypeError(msg)
    _reject_hard_overrides(loaded)
    values = base.__dict__.copy()
    known = set(values)
    for key, raw in loaded.items():
        if key in _HARD_TEXT or key in {"mode", "initial_cash_usd"}:
            continue
        if key not in known:
            msg = f"unknown paper desk setting {key}"
            raise ValueError(msg)
        values[key] = _coerce(key, raw, values[key])
    _validate(values)
    if plan:
        values["gmgn_plan"] = _coerce("gmgn_plan", plan, values["gmgn_plan"])
    return Settings(**values)


def _with_plan(settings: Settings, plan: str | None) -> Settings:
    if not plan:
        return settings
    values = settings.__dict__.copy()
    values["gmgn_plan"] = _coerce("gmgn_plan", plan, settings.gmgn_plan)
    return Settings(**values)


def _reject_hard_overrides(loaded: dict[str, Any]) -> None:
    for key, required in _HARD_TEXT.items():
        if key not in loaded:
            continue
        got = dec(loaded[key])
        need = dec(required)
        if got != need:
            msg = f"{key} is fixed at {required} and cannot be changed"
            raise ValueError(msg)


def _coerce(key: str, raw: object, sample: object) -> object:
    if isinstance(sample, Decimal):
        parsed = dec(raw)
        if parsed is None:
            msg = f"{key} must be a number"
            raise ValueError(msg)
        return parsed
    if isinstance(sample, int) and not isinstance(sample, bool):
        parsed = dec(raw)
        if parsed is None or parsed != parsed.to_integral_value():
            msg = f"{key} must be an integer"
            raise ValueError(msg)
        return int(parsed)
    if isinstance(sample, str):
        if not isinstance(raw, str) or not raw.strip():
            msg = f"{key} must be a string"
            raise ValueError(msg)
        return raw.strip()
    if isinstance(sample, tuple):
        if not isinstance(raw, list) or not raw:
            msg = f"{key} must be a non-empty list"
            raise ValueError(msg)
        return tuple(str(item).strip() for item in raw)
    if isinstance(sample, dict):
        if not isinstance(raw, dict):
            msg = f"{key} must be a mapping"
            raise TypeError(msg)
        out: dict[str, Decimal] = {}
        for name, value in raw.items():
            parsed = dec(value)
            if parsed is None:
                msg = f"{key}.{name} must be a number"
                raise ValueError(msg)
            out[str(name)] = parsed
        return out
    msg = f"cannot override {key}"
    raise ValueError(msg)


def _validate(values: dict[str, Any]) -> None:
    chains = tuple(values["chains"])
    unknown = [chain for chain in chains if chain not in SUPPORTED_CHAINS]
    if unknown or len(set(chains)) != len(chains):
        msg = "chains must be a unique subset of sol, base, bsc"
        raise ValueError(msg)
    if "robinhood" in chains:
        msg = "robinhood is not a paper-desk chain"
        raise ValueError(msg)
    values["chains"] = chains
    if values["buy_usd"] <= 0 or values["buy_usd"] > MAX_POSITION_USD:
        msg = "buy_usd must be in (0, 50]"
        raise ValueError(msg)
    if values["tp1_fraction"] <= 0 or values["tp2_fraction"] <= 0:
        msg = "take-profit fractions must be positive"
        raise ValueError(msg)
    if values["tp1_fraction"] + values["tp2_fraction"] >= 1:
        msg = "take-profit fractions must leave a remainder to trail"
        raise ValueError(msg)
    if values["tp1_multiple"] <= 1 or values["tp2_multiple"] <= values["tp1_multiple"]:
        msg = "take-profit multiples must be increasing and above 1"
        raise ValueError(msg)
    if not (Decimal("0") < values["trail_drawdown"] < 1):
        msg = "trail_drawdown must be between 0 and 1"
        raise ValueError(msg)
    if values["whale_min_wallets"] < 2:
        msg = "whale_min_wallets must be at least 2"
        raise ValueError(msg)
    if values["listener"] not in {"auto", "off", "logs_and_blocks", "geyser"}:
        msg = "listener must be auto, off, logs_and_blocks, or geyser"
        raise ValueError(msg)
    if values["gmgn_plan"] not in {"free", "plus", "pro"}:
        msg = "gmgn_plan must be free, plus, or pro"
        raise ValueError(msg)
    if values["rank_limit"] < 1 or values["rank_limit"] > 100:
        msg = "rank_limit must be 1..100"
        raise ValueError(msg)
    if values["trenches_limit"] < 1 or values["trenches_limit"] > 80:
        msg = "trenches_limit must be 1..80"
        raise ValueError(msg)
    if values["queue_size"] < 1:
        msg = "queue_size must be positive"
        raise ValueError(msg)
    for name in (
        "vote_ttl_seconds",
        "quote_max_age_seconds",
        "security_max_age_seconds",
        "poll_seconds",
        "mark_seconds",
        "entry_retry_seconds",
        "whale_window_seconds",
        "default_fee_bps",
        "evm_gas_units",
    ):
        if values[name] <= 0:
            msg = f"{name} must be positive"
            raise ValueError(msg)
    if not (Decimal("0") <= values["slippage"] < 1):
        msg = "slippage must be in [0, 1)"
        raise ValueError(msg)
    if not (Decimal("0") < values["max_impact"] < 1):
        msg = "max_impact must be in (0, 1)"
        raise ValueError(msg)
    fallback: dict[str, Decimal] = values["gas_fallback_usd"]
    if set(fallback) != set(SUPPORTED_CHAINS):
        msg = "gas_fallback_usd must name sol, base, and bsc"
        raise ValueError(msg)
    if any(amount < 0 for amount in fallback.values()):
        msg = "gas fallbacks must be non-negative"
        raise ValueError(msg)
