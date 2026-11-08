"""Session recap. Definitions are printed with the numbers."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from paper_desk.config import Settings
from paper_desk.ledger import Ledger, Position
from paper_desk.numbers import as_text, money
from paper_desk.security import Quote
from paper_desk.timeutil import ensure_utc, format_et, format_utc, utc_now

DEFINITIONS = {
    "completed_position": (
        "A lot whose status is closed and whose quantity is zero. "
        "Open lots are not completed, even after a partial sell."
    ),
    "win": "A completed position whose realized PnL is greater than zero.",
    "loss": "A completed position whose realized PnL is less than zero.",
    "scratch": "A completed position whose realized PnL is exactly zero. Not a win.",
    "winrate": (
        "Wins divided by completed positions. Null when none have completed. "
        "Open lots are excluded."
    ),
    "realized_pnl": (
        "Sum of sell proceeds after fees and gas, minus the cost allocated to "
        "those sells. Buy fees and gas are part of cost, so they are not subtracted again."
    ),
    "unrealized_pnl": (
        "For each open lot, fresh mark times quantity minus remaining cost. "
        "Null for the session when any open lot has no fresh mark. Not counted as zero."
    ),
    "fees": (
        "Sum of simulated fee and gas on every fill. Informational: already inside "
        "realized PnL and remaining cost."
    ),
    "net_pnl": "Realized plus unrealized when unrealized is known. Otherwise null.",
    "stop_fill": (
        "The -6% level is a trigger against the average execution price. "
        "The simulated fill uses the observed mark after slippage and impact, "
        "which can be worse than -6% when the quote gaps."
    ),
}


def build_report(
    ledger: Ledger,
    session_id: str,
    settings: Settings,
    marks: dict[tuple[str, str], Quote] | None = None,
) -> dict[str, Any]:
    """Recap one session from the ledger and any fresh marks still in memory."""
    row = ledger.session_row(session_id)
    if row is None:
        msg = "session not found"
        raise KeyError(msg)
    now = utc_now()
    closed = ledger.closed_positions(session_id)
    opened = ledger.open_positions(session_id)
    wins = [pos for pos in closed if pos.realized_pnl_usd > 0]
    losses = [pos for pos in closed if pos.realized_pnl_usd < 0]
    scratches = [pos for pos in closed if pos.realized_pnl_usd == 0]
    realized = money(sum((pos.realized_pnl_usd for pos in closed), Decimal(0)))
    realized += money(sum((pos.realized_pnl_usd for pos in opened), Decimal(0)))
    unrealized, unrealized_ok = _unrealized(opened, marks or {}, settings, now)
    fees = _fees(ledger, session_id)
    net = (
        money(realized + unrealized)
        if unrealized_ok and unrealized is not None
        else None
    )
    best = _extreme(closed, best=True)
    worst = _extreme(closed, best=False)
    cash = ledger.cash(session_id)
    return {
        "session_id": session_id,
        "mode": row["mode"],
        "status": row["status"],
        "started_at_utc": row["started_at"],
        "ends_at_utc": row["ends_at"],
        "ended_at_utc": row["ended_at"],
        "reported_at_utc": format_utc(now),
        "reported_at_et": format_et(now),
        "initial_cash_usd": row["initial_cash_usd"],
        "cash_usd": as_text(cash),
        "completed_positions": len(closed),
        "open_positions": len(opened),
        "wins": len(wins),
        "losses": len(losses),
        "scratches": len(scratches),
        "winrate": _ratio(len(wins), len(closed)),
        "realized_pnl_usd": as_text(realized),
        "unrealized_pnl_usd": as_text(unrealized) if unrealized_ok else None,
        "fees_usd": as_text(fees),
        "net_pnl_usd": as_text(net) if net is not None else None,
        "best_trade": best,
        "worst_trade": worst,
        "open": [_open_line(pos, marks or {}, settings, now) for pos in opened],
        "definitions": DEFINITIONS,
        "live_trading": False,
        "note": (
            "Paper simulation only. No live session is started or completed by "
            "reading this report. Fills are not on-chain trades."
        ),
    }


def render_markdown(report: dict[str, Any]) -> str:
    """Recap a human can read. Definitions come before the numbers."""
    lines = [
        f"# Paper desk recap {report['session_id']}",
        "",
        f"Mode: `{report['mode']}`. Status: `{report['status']}`.",
        f"Reported {report['reported_at_et']} (stored {report['reported_at_utc']}).",
        "",
        report["note"],
        "",
        "## Definitions",
        "",
    ]
    definitions = report["definitions"]
    if isinstance(definitions, dict):
        for key, text in definitions.items():
            lines.append(f"- **{key}**: {text}")
    lines.extend(
        [
            "",
            "## Results",
            "",
            f"- Cash: {report['cash_usd']} USD (started {report['initial_cash_usd']})",
            f"- Realized PnL: {report['realized_pnl_usd']}",
            f"- Unrealized PnL: {report['unrealized_pnl_usd']}",
            f"- Fees (included in PnL): {report['fees_usd']}",
            f"- Net PnL: {report['net_pnl_usd']}",
            f"- Completed positions: {report['completed_positions']}",
            f"- Open positions: {report['open_positions']}",
            f"- Wins / losses / scratches: {report['wins']} / {report['losses']} / {report['scratches']}",
            f"- Win rate: {report['winrate']}",
            f"- Best completed trade: {_fmt_trade(report['best_trade'])}",
            f"- Worst completed trade: {_fmt_trade(report['worst_trade'])}",
            "",
            "These numbers are simulated. They are not a claim about live trading.",
            "",
        ]
    )
    return "\n".join(lines)


def write_report(path_json: Any, path_md: Any, report: dict[str, Any]) -> None:
    """Write the JSON and markdown recap."""
    path_json.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    path_md.write_text(render_markdown(report), encoding="utf-8")


def _fees(ledger: Ledger, session_id: str) -> Decimal:
    total = Decimal(0)
    for row in ledger.fills(session_id):
        total += Decimal(row["fee_usd"]) + Decimal(row["gas_usd"])
    return money(total)


def _unrealized(
    positions: list[Position],
    marks: dict[tuple[str, str], Quote],
    settings: Settings,
    now: Any,
) -> tuple[Decimal | None, bool]:
    if not positions:
        return Decimal(0), True
    total = Decimal(0)
    for pos in positions:
        quote = marks.get((pos.chain, pos.contract))
        value = _mark_pnl(pos, quote, settings, now)
        if value is None:
            return None, False
        total += value
    return money(total), True


def _mark_pnl(
    pos: Position, quote: Quote | None, settings: Settings, now: Any
) -> Decimal | None:
    if quote is None or quote.price_usd <= 0:
        return None
    age = (ensure_utc(now) - ensure_utc(quote.fetched_at)).total_seconds()
    if age > settings.quote_max_age_seconds or age < -5:
        return None
    return money(quote.price_usd * pos.qty - pos.remaining_cost_usd)


def _open_line(
    pos: Position,
    marks: dict[tuple[str, str], Quote],
    settings: Settings,
    now: Any,
) -> dict[str, Any]:
    quote = marks.get((pos.chain, pos.contract))
    unrealized = _mark_pnl(pos, quote, settings, now)
    return {
        "chain": pos.chain,
        "contract": pos.contract,
        "symbol": pos.symbol,
        "qty": as_text(pos.qty),
        "remaining_cost_usd": as_text(pos.remaining_cost_usd),
        "realized_pnl_usd": as_text(pos.realized_pnl_usd),
        "unrealized_pnl_usd": as_text(unrealized) if unrealized is not None else None,
    }


def _extreme(positions: list[Position], *, best: bool) -> dict[str, Any] | None:
    if not positions:
        return None
    chosen = max(positions, key=lambda pos: pos.realized_pnl_usd)
    if not best:
        chosen = min(positions, key=lambda pos: pos.realized_pnl_usd)
    return {
        "chain": chosen.chain,
        "contract": chosen.contract,
        "symbol": chosen.symbol,
        "realized_pnl_usd": as_text(chosen.realized_pnl_usd),
    }


def _ratio(wins: int, completed: int) -> str | None:
    if completed == 0:
        return None
    return format(money(Decimal(wins) / Decimal(completed)), "f")


def _fmt_trade(trade: object) -> str:
    if not isinstance(trade, dict):
        return "none"
    return (
        f"{trade.get('symbol') or trade.get('contract')} on {trade.get('chain')} "
        f"realized {trade.get('realized_pnl_usd')}"
    )
