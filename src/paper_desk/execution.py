"""Simulated fills. A trigger is not a fill price.

Buy and sell prices worsen the observed mid by the larger of the configured
slippage and a linear impact of notional / liquidity. They are not added.
Pool `fee_ratio` follows the GMGN token-info note that 0.1 means 0.1%.
When that field is absent, the configured fee is used and labeled. When it
is present but unusable, the fill is refused.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from paper_desk.numbers import money

_HUNDRED = Decimal(100)
_BPS = Decimal(10000)
_LAMPORTS = Decimal(1_000_000_000)
_WEI = Decimal(10) ** 18


@dataclass(frozen=True)
class FillPlan:
    """A fill that either names its cash legs or the reason it cannot."""

    ok: bool
    reason: str
    qty: Decimal
    exec_price: Decimal
    mid: Decimal
    gross_usd: Decimal
    fee_usd: Decimal
    gas_usd: Decimal
    cash_delta: Decimal
    impact: Decimal
    slippage: Decimal
    fee_source: str
    gas_source: str
    allocated_cost: Decimal
    realized_pnl: Decimal


def _fail(reason: str, *, gas_source: str = "", fee_source: str = "") -> FillPlan:
    zero = Decimal(0)
    return FillPlan(
        ok=False,
        reason=reason,
        qty=zero,
        exec_price=zero,
        mid=zero,
        gross_usd=zero,
        fee_usd=zero,
        gas_usd=zero,
        cash_delta=zero,
        impact=zero,
        slippage=zero,
        fee_source=fee_source,
        gas_source=gas_source,
        allocated_cost=zero,
        realized_pnl=zero,
    )


def fee_fraction(raw: str | None, default_bps: int) -> tuple[Decimal | None, str]:
    """Return the fee as a fraction of notional, or None when the field is bad."""
    if raw is None or raw.strip() == "":
        return Decimal(default_bps) / _BPS, "configured_default_bps"
    try:
        points = Decimal(raw.strip())
    except InvalidOperation:
        return None, "fee_ratio_unparseable"
    if not points.is_finite() or points < 0 or points > _HUNDRED:
        return None, "fee_ratio_out_of_range"
    return points / _HUNDRED, "gmgn_pool_fee_ratio_percent_points"


def resolve_gas_usd(
    chain: str,
    payload: dict[str, object] | None,
    *,
    fallback_usd: Decimal,
    evm_gas_units: int,
    max_plausible_usd: Decimal,
) -> tuple[Decimal, str]:
    """USD gas from GMGN `gas-price`, else a labeled configured fallback.

    SOL `average` is treated as lamports. EVM `average` is wei per gas times
    the configured unit count. An implausible result is not used as a quote.
    """
    fallback = money(fallback_usd)
    labeled = "configured_fallback"
    if not payload:
        return fallback, labeled + "_missing_quote"
    native = payload.get("native_token_usd_price")
    average = payload.get("average")
    if native is None or average is None or average == "":
        return fallback, labeled + "_incomplete_quote"
    try:
        native_usd = Decimal(str(native))
        avg = Decimal(str(average))
    except InvalidOperation:
        return fallback, labeled + "_unparseable_quote"
    if not native_usd.is_finite() or not avg.is_finite():
        return fallback, labeled + "_unparseable_quote"
    if native_usd <= 0 or avg < 0:
        return fallback, labeled + "_nonpositive_quote"
    if chain == "sol":
        cost = (avg / _LAMPORTS) * native_usd
        source = "gmgn_gas_price.average_lamports"
    else:
        cost = (avg * Decimal(evm_gas_units) / _WEI) * native_usd
        source = "gmgn_gas_price.average_wei_times_configured_units"
    if cost > max_plausible_usd:
        return fallback, labeled + "_implausible_quote"
    return money(cost), source


def plan_buy(
    *,
    mid: Decimal,
    liquidity: Decimal | None,
    budget: Decimal,
    fee_raw: str | None,
    gas_usd: Decimal,
    gas_source: str,
    slippage: Decimal,
    max_impact: Decimal,
    default_fee_bps: int,
) -> FillPlan:
    """Spend at most `budget` cash, fees and gas included."""
    if mid <= 0:
        return _fail("quote_price_invalid", gas_source=gas_source)
    if liquidity is None or liquidity <= 0:
        return _fail("liquidity_unknown", gas_source=gas_source)
    fraction, fee_source = fee_fraction(fee_raw, default_fee_bps)
    if fraction is None:
        return _fail(fee_source, gas_source=gas_source, fee_source=fee_source)
    gas = money(gas_usd)
    if gas < 0:
        return _fail("gas_unknown", gas_source=gas_source)
    if budget <= gas:
        return _fail("budget_does_not_cover_gas", gas_source=gas_source)
    gross = (budget - gas) / (Decimal(1) + fraction)
    impact = gross / liquidity
    if impact > max_impact:
        return _fail("impact_above_cap", gas_source=gas_source, fee_source=fee_source)
    worsen = max(slippage, impact)
    exec_price = mid * (Decimal(1) + worsen)
    gross_q = money(gross)
    fee_q = money(gross_q * fraction)
    total = gross_q + fee_q + gas
    if total > budget:
        gross_q = money(gross_q - (total - budget))
        fee_q = money(gross_q * fraction)
        total = gross_q + fee_q + gas
        if total > budget:
            fee_q = money(fee_q - (total - budget))
            total = gross_q + fee_q + gas
    if gross_q <= 0 or total <= 0 or total > budget:
        return _fail("budget_unusable", gas_source=gas_source, fee_source=fee_source)
    qty = gross_q / exec_price
    if qty <= 0:
        return _fail("qty_nonpositive", gas_source=gas_source, fee_source=fee_source)
    return FillPlan(
        ok=True,
        reason="buy",
        qty=qty,
        exec_price=exec_price,
        mid=mid,
        gross_usd=gross_q,
        fee_usd=fee_q,
        gas_usd=gas,
        cash_delta=-total,
        impact=impact,
        slippage=slippage,
        fee_source=fee_source,
        gas_source=gas_source,
        allocated_cost=total,
        realized_pnl=Decimal(0),
    )


def plan_sell(
    *,
    mid: Decimal,
    liquidity: Decimal | None,
    qty: Decimal,
    fee_raw: str | None,
    gas_usd: Decimal,
    gas_source: str,
    slippage: Decimal,
    max_impact: Decimal,
    default_fee_bps: int,
    remaining_qty: Decimal,
    remaining_cost: Decimal,
    close_all: bool,
) -> FillPlan:
    """Sell `qty` at the worsened mid. Cost leaves the lot pro-rata.

    The closing slice takes whatever cost is left so rounding cannot strand
    a dust basis. Realized PnL is net proceeds minus that allocated cost.
    Fees already sit inside both numbers and are not subtracted again.
    """
    if mid <= 0:
        return _fail("quote_price_invalid", gas_source=gas_source)
    if liquidity is None or liquidity <= 0:
        return _fail("liquidity_unknown", gas_source=gas_source)
    if qty <= 0 or qty > remaining_qty:
        return _fail("sell_qty_invalid", gas_source=gas_source)
    fraction, fee_source = fee_fraction(fee_raw, default_fee_bps)
    if fraction is None:
        return _fail(fee_source, gas_source=gas_source, fee_source=fee_source)
    gas = money(gas_usd)
    if gas < 0:
        return _fail("gas_unknown", gas_source=gas_source)
    estimate = qty * mid
    impact = estimate / liquidity
    if impact > max_impact:
        return _fail("impact_above_cap", gas_source=gas_source, fee_source=fee_source)
    worsen = max(slippage, impact)
    exec_price = mid * (Decimal(1) - worsen)
    if exec_price <= 0:
        return _fail(
            "sell_price_nonpositive", gas_source=gas_source, fee_source=fee_source
        )
    gross = money(qty * exec_price)
    fee = money(gross * fraction)
    net = gross - fee - gas
    if close_all or qty == remaining_qty:
        allocated = remaining_cost
    else:
        allocated = money(remaining_cost * qty / remaining_qty)
    realized = money(net - allocated)
    return FillPlan(
        ok=True,
        reason="sell",
        qty=qty,
        exec_price=exec_price,
        mid=mid,
        gross_usd=gross,
        fee_usd=fee,
        gas_usd=gas,
        cash_delta=net,
        impact=impact,
        slippage=slippage,
        fee_source=fee_source,
        gas_source=gas_source,
        allocated_cost=allocated,
        realized_pnl=realized,
    )
