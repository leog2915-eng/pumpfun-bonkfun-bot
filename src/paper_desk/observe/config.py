"""Observation settings. Safety thresholds are the paper-desk constants."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from paper_desk.config import (
    MAX_DEV_HOLD_RATE,
    MIN_LOCK_PERCENT,
    STOP_TRIGGER,
)
from paper_desk.numbers import SUPPORTED_CHAINS, dec

# Caps at or below the conservative public budgets checked in the smoke.
# GeckoTerminal did not send a rate header; 10/min is the operator cap.
# RugCheck sent X-Rate-Limit-Limit: 15, so 10/min stays under that window.
# GoPlus sent no rate header on the keyless calls; 10/min is the cap used here.
_MAX_PROVIDER_PER_MINUTE = 10
_PAPER_SPIKE_PCT = Decimal("15")
_PAPER_SPIKE_VOLUME = Decimal("25000")
_PAPER_GAINER_PCT = Decimal("25")
_PAPER_GAINER_VOLUME = Decimal("10000")


@dataclass(frozen=True)
class ObserveSettings:
    """Poll and sample knobs. Lock, dev-team, and bundle rules are fixed."""

    chains: tuple[str, ...] = ("sol", "base", "bsc")
    poll_seconds: int = 60
    gecko_per_minute: int = _MAX_PROVIDER_PER_MINUTE
    rugcheck_per_minute: int = _MAX_PROVIDER_PER_MINUTE
    goplus_per_minute: int = _MAX_PROVIDER_PER_MINUTE
    enrich_per_cycle: int = 2
    scout_min_spike_pct: Decimal = _PAPER_SPIKE_PCT
    scout_min_spike_volume_usd: Decimal = _PAPER_SPIKE_VOLUME
    scout_min_gainer_pct: Decimal = _PAPER_GAINER_PCT
    scout_min_gainer_volume_usd: Decimal = _PAPER_GAINER_VOLUME
    quote_max_age_seconds: int = 20
    security_max_age_seconds: int = 120
    min_liquidity_usd: Decimal = Decimal("1000")
    min_lock_percent: Decimal = MIN_LOCK_PERCENT
    max_dev_hold_rate: Decimal = MAX_DEV_HOLD_RATE
    stop_trigger: Decimal = STOP_TRIGGER
    vote_ttl_seconds: int = 90

    def to_public_dict(self) -> dict[str, Any]:
        """JSON settings with no endpoints and no credentials."""
        raw: dict[str, Any] = {}
        for key, value in self.__dict__.items():
            if isinstance(value, Decimal):
                raw[key] = format(value, "f")
            else:
                raw[key] = value
        return raw


def default_observe_settings() -> ObserveSettings:
    """Defaults for the free observation profile."""
    return ObserveSettings()


def load_observe_settings(path: Path | None) -> ObserveSettings:
    """Load YAML over the defaults.

    Raises:
        ValueError: On an unknown key, a higher provider budget, a looser
            scout threshold, or any attempt to change a safety constant.
    """
    base = default_observe_settings()
    if path is None or not path.exists():
        return base
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if loaded is None:
        return base
    if not isinstance(loaded, dict):
        msg = "observe config must be a mapping"
        raise TypeError(msg)
    values = base.__dict__.copy()
    for key, raw in loaded.items():
        if key not in values:
            msg = f"unknown observe setting {key}"
            raise ValueError(msg)
        if key in {"min_lock_percent", "max_dev_hold_rate", "stop_trigger"}:
            parsed = dec(raw)
            if parsed != values[key]:
                msg = f"{key} is fixed and cannot be changed"
                raise ValueError(msg)
            continue
        values[key] = _coerce(key, raw, values[key])
    _validate(values)
    return ObserveSettings(**values)


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
    if isinstance(sample, tuple):
        if not isinstance(raw, list) or not raw:
            msg = f"{key} must be a non-empty list"
            raise ValueError(msg)
        return tuple(str(item).strip() for item in raw)
    msg = f"cannot override {key}"
    raise ValueError(msg)


def _validate(values: dict[str, Any]) -> None:
    chains = tuple(values["chains"])
    if any(chain not in SUPPORTED_CHAINS for chain in chains) or len(
        set(chains)
    ) != len(chains):
        msg = "chains must be a unique subset of sol, base, bsc"
        raise ValueError(msg)
    values["chains"] = chains
    for name in ("gecko_per_minute", "rugcheck_per_minute", "goplus_per_minute"):
        if values[name] < 1 or values[name] > _MAX_PROVIDER_PER_MINUTE:
            msg = f"{name} must be 1..{_MAX_PROVIDER_PER_MINUTE}"
            raise ValueError(msg)
    if values["scout_min_spike_pct"] < _PAPER_SPIKE_PCT:
        msg = "scout_min_spike_pct cannot be below the paper-desk default"
        raise ValueError(msg)
    if values["scout_min_spike_volume_usd"] < _PAPER_SPIKE_VOLUME:
        msg = "scout_min_spike_volume_usd cannot be below the paper-desk default"
        raise ValueError(msg)
    if values["scout_min_gainer_pct"] < _PAPER_GAINER_PCT:
        msg = "scout_min_gainer_pct cannot be below the paper-desk default"
        raise ValueError(msg)
    if values["scout_min_gainer_volume_usd"] < _PAPER_GAINER_VOLUME:
        msg = "scout_min_gainer_volume_usd cannot be below the paper-desk default"
        raise ValueError(msg)
    if values["quote_max_age_seconds"] > 20 or values["quote_max_age_seconds"] < 1:
        msg = "quote_max_age_seconds cannot exceed 20"
        raise ValueError(msg)
    if (
        values["security_max_age_seconds"] > 120
        or values["security_max_age_seconds"] < 1
    ):
        msg = "security_max_age_seconds cannot exceed 120"
        raise ValueError(msg)
    if values["min_liquidity_usd"] < Decimal("1000"):
        msg = "min_liquidity_usd cannot be below 1000"
        raise ValueError(msg)
    if values["poll_seconds"] < 1 or values["enrich_per_cycle"] < 0:
        msg = "poll and enrich must be non-negative, and poll at least 1"
        raise ValueError(msg)
    if values["vote_ttl_seconds"] < 1 or values["vote_ttl_seconds"] > 90:
        msg = "vote_ttl_seconds cannot exceed 90"
        raise ValueError(msg)
