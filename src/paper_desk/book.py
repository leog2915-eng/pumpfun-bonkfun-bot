"""Quorum, shield vetoes, and exit rules over the ledger.

Scout, whale, and sniper are the only entry votes. Shield vetoes and the
exit agent never count toward quorum. Exits do not wait for a vote.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from typing import Any

from paper_desk.config import Settings
from paper_desk.execution import FillPlan, plan_buy, plan_sell
from paper_desk.ledger import Ledger, Position, StoredVote, insert_decision_row
from paper_desk.numbers import ENTRY_AGENTS, as_text, money
from paper_desk.security import Quote, SecurityView, shield_vetoes
from paper_desk.timeutil import ensure_utc, format_et, format_utc
from utils.logger import get_logger

logger = get_logger(__name__)


class Book:
    """Single-writer trading rules. The ledger transaction is the atomic section."""

    def __init__(
        self,
        ledger: Ledger,
        settings: Settings,
        session_id: str,
        now_fn: Any,
    ) -> None:
        self.ledger = ledger
        self.settings = settings
        self.session_id = session_id
        self._now_fn = now_fn

    def now(self) -> datetime:
        """Clock supplied by the caller, always UTC."""
        return ensure_utc(self._now_fn())

    def note(
        self, kind: str, message: str, detail: dict[str, object] | None = None
    ) -> None:
        """Coverage, outage, or rate-limit line. Not a trade."""
        stamp = self.now()
        payload = detail or {}
        self.ledger.add_event(self.session_id, stamp, kind, message, payload)
        logger.info(
            "%s utc=%s kind=%s %s",
            format_et(stamp),
            format_utc(stamp),
            kind,
            message,
        )

    def record_signal(
        self,
        *,
        agent: str,
        chain: str,
        contract: str,
        price: Decimal,
        reason: str,
        symbol: str,
        evidence: dict[str, object],
    ) -> str:
        """Store one entry vote. Returns `quorum` or `waiting` or `rejected`."""
        origin = str(evidence.get("origin", "market"))
        if agent not in ENTRY_AGENTS:
            self._emit(
                chain=chain,
                contract=contract,
                side="veto",
                reason="agent_is_not_an_entry_vote",
                votes=[],
                vetoes=[agent],
                price=price,
                detail={"agent": agent},
            )
            return "rejected"
        if self.settings.mode != "synthetic" and origin == "synthetic":
            self._emit(
                chain=chain,
                contract=contract,
                side="veto",
                reason="synthetic_data_refused",
                votes=[],
                vetoes=["synthetic_data_refused"],
                price=price,
                detail={"agent": agent},
            )
            return "rejected"
        stamp = self.now()
        self.ledger.upsert_vote(
            self.session_id,
            StoredVote(chain, contract, agent, stamp, price, reason),
            {**evidence, "symbol": symbol, "origin": origin},
        )
        names = self._fresh_names(chain, contract, stamp)
        self._emit(
            chain=chain,
            contract=contract,
            side="signal",
            reason=reason,
            votes=names,
            vetoes=[],
            price=price,
            detail={"agent": agent, "symbol": symbol, "origin": origin},
        )
        if len(names) < 2:
            self._emit(
                chain=chain,
                contract=contract,
                side="skip",
                reason="quorum_not_met",
                votes=names,
                vetoes=[],
                price=price,
                detail={"need": 2, "symbol": symbol},
            )
            return "waiting"
        return "quorum"

    def entry_unavailable(self, chain: str, contract: str, reason: str) -> None:
        """Quorum may be met, but the clearance read failed. No fill."""
        names = self._fresh_names(chain, contract, self.now())
        self._emit(
            chain=chain,
            contract=contract,
            side="veto",
            reason=reason,
            votes=names,
            vetoes=[reason],
            price=None,
            detail={},
        )

    def mark_unavailable(self, chain: str, contract: str, reason: str) -> None:
        """Exit could not read a quote. The position stays open."""
        self._emit(
            chain=chain,
            contract=contract,
            side="hold",
            reason=reason,
            votes=[],
            vetoes=[reason],
            price=None,
            detail={},
        )

    def block_once(
        self,
        *,
        key: str,
        chain: str,
        contract: str,
        reason: str,
        vetoes: list[str],
        detail: dict[str, object],
    ) -> None:
        """Log a sniper block once per session key."""
        if not self.ledger.claim_once(self.session_id, key):
            return
        self._emit(
            chain=chain,
            contract=contract,
            side="veto",
            reason=reason,
            votes=[],
            vetoes=vetoes,
            price=None,
            detail=detail,
        )

    def try_entry(
        self,
        *,
        chain: str,
        contract: str,
        symbol: str,
        quote: Quote,
        view: SecurityView,
        gas_usd: Decimal,
        gas_source: str,
    ) -> None:
        """Buy only with quorum, fresh clearance, and room under the caps."""
        stamp = self.now()
        names = self._fresh_names(chain, contract, stamp)
        if len(names) < 2:
            self._emit(
                chain=chain,
                contract=contract,
                side="skip",
                reason="quorum_not_met",
                votes=names,
                vetoes=[],
                price=quote.price_usd,
                detail={"symbol": symbol},
            )
            return
        vetoes = shield_vetoes(view, quote, self.settings, stamp)
        existing = self.ledger.get_position(self.session_id, chain, contract)
        if existing is not None:
            vetoes.append(
                "duplicate_position" if existing.status == "open" else "already_traded"
            )
        if self.ledger.count_open(self.session_id) >= self.settings.max_open_positions:
            vetoes.append("max_open_positions")
        if vetoes:
            self._emit(
                chain=chain,
                contract=contract,
                side="veto",
                reason=vetoes[0],
                votes=names,
                vetoes=vetoes,
                price=quote.price_usd,
                detail={"symbol": symbol, "field_sources": view.field_sources},
            )
            return
        budget = min(self.settings.buy_usd, self.settings.max_position_usd)
        plan = plan_buy(
            mid=quote.price_usd,
            liquidity=quote.liquidity_usd,
            budget=budget,
            fee_raw=quote.fee_ratio_raw,
            gas_usd=gas_usd,
            gas_source=gas_source,
            slippage=self.settings.slippage,
            max_impact=self.settings.max_impact,
            default_fee_bps=self.settings.default_fee_bps,
        )
        if not plan.ok:
            self._emit(
                chain=chain,
                contract=contract,
                side="veto",
                reason=plan.reason,
                votes=names,
                vetoes=[plan.reason],
                price=quote.price_usd,
                detail={"symbol": symbol, "gas_source": gas_source},
            )
            return
        if plan.allocated_cost > self.settings.max_position_usd:
            self._emit(
                chain=chain,
                contract=contract,
                side="veto",
                reason="max_position_usd",
                votes=names,
                vetoes=["max_position_usd"],
                price=quote.price_usd,
                detail={"deployed": as_text(plan.allocated_cost)},
            )
            return
        self._apply_buy(chain, contract, symbol, quote, plan, names)

    def on_mark(self, quote: Quote, gas_usd: Decimal, gas_source: str) -> None:
        """Exit path. Never consults quorum. A stale quote does not fill."""
        pos = self.ledger.get_position(self.session_id, quote.chain, quote.contract)
        if pos is None or pos.status != "open" or pos.qty <= 0:
            return
        stamp = self.now()
        blocked = self._mark_blocked(quote, stamp)
        if blocked:
            self._emit(
                chain=pos.chain,
                contract=pos.contract,
                side="hold",
                reason=blocked,
                votes=[],
                vetoes=[blocked],
                price=quote.price_usd,
                detail={"symbol": pos.symbol, "qty": as_text(pos.qty)},
            )
            return
        trigger = pos.avg_exec_price * (Decimal(1) - self.settings.stop_trigger)
        if quote.price_usd <= trigger:
            reason = "stop_trigger_gap" if quote.price_usd < trigger else "stop_trigger"
            self._sell(
                pos,
                quote,
                pos.qty,
                reason,
                gas_usd,
                gas_source,
                close_all=True,
                extra={
                    "trigger_price": as_text(trigger),
                    "observed_mid": as_text(quote.price_usd),
                },
            )
            return
        current = pos
        if (
            not current.tp1_done
            and quote.price_usd >= current.avg_exec_price * self.settings.tp1_multiple
        ):
            qty = min(current.original_qty * self.settings.tp1_fraction, current.qty)
            filled = self._sell(
                current,
                quote,
                qty,
                "tp1_partial",
                gas_usd,
                gas_source,
                close_all=False,
                extra={"multiple": as_text(self.settings.tp1_multiple)},
            )
            if not filled:
                return
            current = self._reload(current)
            if current is None:
                return
        if (
            current.status == "open"
            and not current.tp2_done
            and quote.price_usd >= current.avg_exec_price * self.settings.tp2_multiple
        ):
            qty = min(current.original_qty * self.settings.tp2_fraction, current.qty)
            filled = self._sell(
                current,
                quote,
                qty,
                "tp2_partial",
                gas_usd,
                gas_source,
                close_all=False,
                extra={"multiple": as_text(self.settings.tp2_multiple)},
            )
            if not filled:
                return
            current = self._reload(current)
            if current is None:
                return
        if current.status != "open":
            return
        if current.trail_armed:
            peak = (
                current.trail_peak
                if current.trail_peak is not None
                else quote.price_usd
            )
            if quote.price_usd > peak:
                peak = quote.price_usd
                self._set_peak(current, peak)
                current = self._reload(current)
                if current is None:
                    return
            floor = peak * (Decimal(1) - self.settings.trail_drawdown)
            if quote.price_usd <= floor:
                reason = (
                    "trail_trigger_gap" if quote.price_usd < floor else "trail_trigger"
                )
                self._sell(
                    current,
                    quote,
                    current.qty,
                    reason,
                    gas_usd,
                    gas_source,
                    close_all=True,
                    extra={"trail_floor": as_text(floor), "trail_peak": as_text(peak)},
                )
                return
        self._emit(
            chain=current.chain,
            contract=current.contract,
            side="hold",
            reason="no_exit_trigger",
            votes=[],
            vetoes=[],
            price=quote.price_usd,
            detail={
                "symbol": current.symbol,
                "qty": as_text(current.qty),
                "stop_trigger_price": as_text(trigger),
                "trail_armed": current.trail_armed,
            },
        )

    def _reload(self, pos: Position) -> Position | None:
        nxt = self.ledger.get_position(self.session_id, pos.chain, pos.contract)
        if nxt is None or nxt.status != "open" or nxt.qty <= 0:
            return None
        return nxt

    def _mark_blocked(self, quote: Quote, now: datetime) -> str | None:
        if self.settings.mode != "synthetic" and quote.origin == "synthetic":
            return "synthetic_data_refused"
        age = (now - ensure_utc(quote.fetched_at)).total_seconds()
        if age > self.settings.quote_max_age_seconds or age < -5:
            return "exit_blocked_stale_quote"
        if quote.price_usd <= 0:
            return "exit_blocked_invalid_quote"
        if quote.liquidity_usd is None or quote.liquidity_usd <= 0:
            return "exit_blocked_unknown_liquidity"
        return None

    def _fresh_names(self, chain: str, contract: str, now: datetime) -> list[str]:
        votes = self.ledger.fresh_votes(
            self.session_id, chain, contract, now, self.settings.vote_ttl_seconds
        )
        return sorted({vote.agent for vote in votes if vote.agent in ENTRY_AGENTS})

    def _apply_buy(
        self,
        chain: str,
        contract: str,
        symbol: str,
        quote: Quote,
        plan: FillPlan,
        votes: list[str],
    ) -> None:
        needed = -plan.cash_delta
        with self.ledger.transaction() as conn:
            existing = self.ledger.get_position(self.session_id, chain, contract)
            open_count = self.ledger.count_open(self.session_id)
            cash = self.ledger.cash(self.session_id)
            veto = _buy_veto(
                existing, open_count, cash, needed, self.settings.max_open_positions
            )
            if veto:
                self._emit(
                    chain=chain,
                    contract=contract,
                    side="veto",
                    reason=veto,
                    votes=votes,
                    vetoes=[veto],
                    price=quote.price_usd,
                    detail={"symbol": symbol, "cash": as_text(cash)},
                    conn=conn,
                )
                return
            cash_after = money(cash - needed)
            avg = plan.gross_usd / plan.qty
            stamp = self.now()
            conn.execute(
                """
                INSERT INTO positions (
                    session_id, chain, contract, symbol, qty, remaining_cost_usd,
                    avg_exec_price, original_qty, opened_at, tp1_done, tp2_done,
                    trail_armed, trail_peak, status, realized_pnl_usd, fees_usd
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, NULL, 'open', '0', ?)
                """,
                (
                    self.session_id,
                    chain,
                    contract,
                    symbol,
                    as_text(plan.qty),
                    as_text(plan.allocated_cost),
                    as_text(avg),
                    as_text(plan.qty),
                    format_utc(stamp),
                    as_text(plan.fee_usd + plan.gas_usd),
                ),
            )
            conn.execute(
                "UPDATE cash SET cash_usd = ? WHERE session_id = ?",
                (as_text(cash_after), self.session_id),
            )
            self._insert_fill(
                conn,
                stamp,
                chain,
                contract,
                "buy",
                "quorum_entry",
                plan,
                cash_after,
                votes,
                [],
                {
                    "symbol": symbol,
                    "origin": quote.origin,
                    "quote_source": quote.source,
                },
            )
            self._emit(
                chain=chain,
                contract=contract,
                side="buy",
                reason="quorum_entry",
                votes=votes,
                vetoes=[],
                price=plan.exec_price,
                fee_usd=plan.fee_usd,
                gas_usd=plan.gas_usd,
                pnl_usd=Decimal(0),
                detail={
                    "symbol": symbol,
                    "qty": as_text(plan.qty),
                    "mid": as_text(plan.mid),
                    "cash_after": as_text(cash_after),
                    "fee_source": plan.fee_source,
                    "gas_source": plan.gas_source,
                    "origin": quote.origin,
                    "deployed_usd": as_text(plan.allocated_cost),
                },
                conn=conn,
            )

    def _sell(
        self,
        pos: Position,
        quote: Quote,
        qty: Decimal,
        reason: str,
        gas_usd: Decimal,
        gas_source: str,
        *,
        close_all: bool,
        extra: dict[str, object],
    ) -> bool:
        plan = plan_sell(
            mid=quote.price_usd,
            liquidity=quote.liquidity_usd,
            qty=qty,
            fee_raw=quote.fee_ratio_raw,
            gas_usd=gas_usd,
            gas_source=gas_source,
            slippage=self.settings.slippage,
            max_impact=self.settings.max_impact,
            default_fee_bps=self.settings.default_fee_bps,
            remaining_qty=pos.qty,
            remaining_cost=pos.remaining_cost_usd,
            close_all=close_all or qty == pos.qty,
        )
        if not plan.ok:
            self._emit(
                chain=pos.chain,
                contract=pos.contract,
                side="hold",
                reason="exit_trigger_unfilled",
                votes=[],
                vetoes=[plan.reason],
                price=quote.price_usd,
                detail={"trigger": reason, "symbol": pos.symbol, **extra},
            )
            return False
        with self.ledger.transaction() as conn:
            current = self.ledger.get_position(self.session_id, pos.chain, pos.contract)
            cash = self.ledger.cash(self.session_id)
            if current is None or current.status != "open" or current.qty < plan.qty:
                self._emit(
                    chain=pos.chain,
                    contract=pos.contract,
                    side="hold",
                    reason="exit_trigger_unfilled",
                    votes=[],
                    vetoes=["position_changed"],
                    price=quote.price_usd,
                    detail={"trigger": reason},
                    conn=conn,
                )
                return False
            cash_after = money(cash + plan.cash_delta)
            if cash_after < 0:
                self._emit(
                    chain=pos.chain,
                    contract=pos.contract,
                    side="hold",
                    reason="exit_trigger_unfilled",
                    votes=[],
                    vetoes=["sell_would_make_cash_negative"],
                    price=quote.price_usd,
                    detail={"trigger": reason, "net": as_text(plan.cash_delta)},
                    conn=conn,
                )
                return False
            closing = close_all or plan.qty == current.qty
            new_qty = Decimal(0) if closing else current.qty - plan.qty
            new_cost = (
                Decimal(0)
                if closing
                else money(current.remaining_cost_usd - plan.allocated_cost)
            )
            if new_cost < 0:
                new_cost = Decimal(0)
            tp1 = 1 if current.tp1_done or reason == "tp1_partial" else 0
            tp2 = 1 if current.tp2_done or reason == "tp2_partial" else 0
            arm = bool(current.trail_armed or (reason == "tp1_partial" and new_qty > 0))
            peak = current.trail_peak
            if reason == "tp1_partial" and new_qty > 0:
                peak = quote.price_usd
            status = "closed" if new_qty <= 0 else "open"
            realized = money(current.realized_pnl_usd + plan.realized_pnl)
            fees = money(current.fees_usd + plan.fee_usd + plan.gas_usd)
            stamp = self.now()
            conn.execute(
                """
                UPDATE positions SET
                    qty = ?, remaining_cost_usd = ?, tp1_done = ?, tp2_done = ?,
                    trail_armed = ?, trail_peak = ?, status = ?,
                    realized_pnl_usd = ?, fees_usd = ?
                WHERE session_id = ? AND chain = ? AND contract = ?
                """,
                (
                    as_text(new_qty),
                    as_text(new_cost),
                    tp1,
                    tp2,
                    1 if arm else 0,
                    as_text(peak) if peak is not None else None,
                    status,
                    as_text(realized),
                    as_text(fees),
                    self.session_id,
                    pos.chain,
                    pos.contract,
                ),
            )
            conn.execute(
                "UPDATE cash SET cash_usd = ? WHERE session_id = ?",
                (as_text(cash_after), self.session_id),
            )
            self._insert_fill(
                conn,
                stamp,
                pos.chain,
                pos.contract,
                "sell",
                reason,
                plan,
                cash_after,
                [],
                [],
                {"symbol": pos.symbol, "origin": quote.origin, **extra},
            )
            self._emit(
                chain=pos.chain,
                contract=pos.contract,
                side="sell",
                reason=reason,
                votes=[],
                vetoes=[],
                price=plan.exec_price,
                fee_usd=plan.fee_usd,
                gas_usd=plan.gas_usd,
                pnl_usd=plan.realized_pnl,
                detail={
                    "symbol": pos.symbol,
                    "qty": as_text(plan.qty),
                    "mid": as_text(plan.mid),
                    "cash_after": as_text(cash_after),
                    "fee_source": plan.fee_source,
                    "gas_source": plan.gas_source,
                    "position_realized_pnl": as_text(realized),
                    "status": status,
                    **extra,
                },
                conn=conn,
            )
        return True

    def _set_peak(self, pos: Position, peak: Decimal) -> None:
        with self.ledger.transaction() as conn:
            conn.execute(
                """
                UPDATE positions SET trail_peak = ?
                WHERE session_id = ? AND chain = ? AND contract = ?
                """,
                (as_text(peak), self.session_id, pos.chain, pos.contract),
            )

    def _insert_fill(
        self,
        conn: Any,
        stamp: datetime,
        chain: str,
        contract: str,
        side: str,
        reason: str,
        plan: FillPlan,
        cash_after: Decimal,
        votes: list[str],
        vetoes: list[str],
        detail: dict[str, object],
    ) -> None:
        conn.execute(
            """
            INSERT INTO fills (
                session_id, ts, chain, contract, side, reason, qty, exec_price,
                mid_price, gross_usd, fee_usd, gas_usd, cash_delta, cash_after,
                realized_pnl_usd, fee_source, gas_source, votes_json, vetoes_json,
                detail_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self.session_id,
                format_utc(stamp),
                chain,
                contract,
                side,
                reason,
                as_text(plan.qty),
                as_text(plan.exec_price),
                as_text(plan.mid),
                as_text(plan.gross_usd),
                as_text(plan.fee_usd),
                as_text(plan.gas_usd),
                as_text(plan.cash_delta),
                as_text(cash_after),
                as_text(plan.realized_pnl),
                plan.fee_source,
                plan.gas_source,
                json.dumps(votes),
                json.dumps(vetoes),
                json.dumps(detail, default=str, sort_keys=True),
            ),
        )

    def _emit(
        self,
        *,
        chain: str | None,
        contract: str | None,
        side: str,
        reason: str,
        votes: list[str],
        vetoes: list[str],
        price: Decimal | None,
        detail: dict[str, object],
        fee_usd: Decimal | None = None,
        gas_usd: Decimal | None = None,
        pnl_usd: Decimal | None = None,
        conn: Any | None = None,
    ) -> None:
        stamp = self.now()
        payload = {
            **detail,
            "stored_utc": format_utc(stamp),
            "display_et": format_et(stamp),
            "mode": self.settings.mode,
        }
        if conn is None:
            self.ledger.add_decision(
                self.session_id,
                stamp,
                chain=chain,
                contract=contract,
                side=side,
                reason=reason,
                votes=votes,
                vetoes=vetoes,
                price=price,
                fee_usd=fee_usd,
                gas_usd=gas_usd,
                pnl_usd=pnl_usd,
                detail=payload,
            )
        else:
            insert_decision_row(
                conn,
                self.session_id,
                stamp,
                chain=chain,
                contract=contract,
                side=side,
                reason=reason,
                votes=votes,
                vetoes=vetoes,
                price=price,
                fee_usd=fee_usd,
                gas_usd=gas_usd,
                pnl_usd=pnl_usd,
                detail=payload,
            )
        logger.info(
            "%s utc=%s chain=%s token=%s side=%s reason=%s votes=%s vetoes=%s "
            "price=%s fee=%s gas=%s pnl=%s",
            format_et(stamp),
            format_utc(stamp),
            chain or "-",
            contract or "-",
            side,
            reason,
            ",".join(votes) if votes else "-",
            ",".join(vetoes) if vetoes else "-",
            as_text(price) if price is not None else "-",
            as_text(fee_usd) if fee_usd is not None else "-",
            as_text(gas_usd) if gas_usd is not None else "-",
            as_text(pnl_usd) if pnl_usd is not None else "-",
        )


def _buy_veto(
    existing: Position | None,
    open_count: int,
    cash: Decimal,
    needed: Decimal,
    max_open: int,
) -> str | None:
    if existing is not None:
        return "duplicate_position" if existing.status == "open" else "already_traded"
    if open_count >= max_open:
        return "max_open_positions"
    if cash < needed or cash - needed < 0:
        return "insufficient_cash"
    return None
