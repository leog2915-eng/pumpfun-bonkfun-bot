"""Paper-desk risk, quorum, accounting, restart, and fail-closed checks."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from paper_desk.agents import scout_agent  # noqa: E402
from paper_desk.book import Book  # noqa: E402
from paper_desk.cli import main  # noqa: E402
from paper_desk.config import Settings, default_settings, load_settings  # noqa: E402
from paper_desk.demo import ManualClock, run_scripted_scenario  # noqa: E402
from paper_desk.errors import MarketError  # noqa: E402
from paper_desk.execution import fee_fraction, plan_buy, plan_sell, resolve_gas_usd  # noqa: E402
from paper_desk.launch_bridge import _offer  # noqa: E402
from paper_desk.ledger import Ledger  # noqa: E402
from paper_desk.report import build_report  # noqa: E402
from paper_desk.runtime import run_demo_session  # noqa: E402
from paper_desk.security import (  # noqa: E402
    CURVE_GRADUATED,
    CURVE_UNGRADUATED,
    Quote,
    SecurityView,
    build_security_view,
    shield_vetoes,
    sniper_blocks,
)
from paper_desk.signals import cluster_ready, scout_hits, whale_buys  # noqa: E402
from paper_desk.timeutil import UTC, format_et, format_utc, parse_duration  # noqa: E402

WIN = "11111111111111111111111111111111"
LOSS = "11111111111111111111111111111112"


def _settings(**overrides: object) -> Settings:
    base = default_settings(mode="synthetic", cash=Decimal("500"))
    values = {
        "slippage": Decimal("0"),
        "default_fee_bps": 0,
        "min_liquidity_usd": Decimal("1"),
    }
    values.update(overrides)
    return replace(base, **values)


def _open(
    directory: Path, cash: Decimal = Decimal("500")
) -> tuple[Book, Ledger, ManualClock, Settings]:
    settings = _settings()
    ledger = Ledger(directory / "desk.sqlite")
    clock = ManualClock()
    ledger.create_session(
        session_id="s",
        mode="synthetic",
        started_at=clock(),
        ends_at=clock() + timedelta(hours=24),
        cash=cash,
        config=settings.to_public_dict(),
        coverage={"exhaustive": False},
    )
    return Book(ledger, settings, "s", clock), ledger, clock, settings


def _quote(clock: ManualClock, contract: str, price: str, chain: str = "sol") -> Quote:
    return Quote(
        chain=chain,
        contract=contract,
        price_usd=Decimal(price),
        liquidity_usd=Decimal("1000000000"),
        fee_ratio_raw=None,
        fetched_at=clock(),
        source="synthetic",
        origin="synthetic",
    )


def _view(
    clock: ManualClock, contract: str, chain: str = "sol", **overrides: object
) -> SecurityView:
    view = SecurityView(
        chain=chain,
        contract=contract,
        fetched_at=clock(),
        origin="synthetic",
        lock_percent=None,
        dev_team_hold_rate=None,
        bundler_rate=None,
        honeypot="empty" if chain == "sol" else "no",
        curve=CURVE_UNGRADUATED,
        curve_evidence="test",
        fetch_error=None,
        field_sources={},
    )
    if overrides:
        return replace(view, **overrides)
    return view


def _vote(book: Book, agent: str, contract: str, chain: str = "sol") -> str:
    return book.record_signal(
        agent=agent,
        chain=chain,
        contract=contract,
        price=Decimal("1"),
        reason="volume_spike" if agent == "scout" else "smart_money_convergence",
        symbol="T",
        evidence={"origin": "synthetic"},
    )


class QuorumTests(unittest.TestCase):
    def test_one_vote_does_not_buy_and_shield_is_not_a_vote(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            book, ledger, clock, _settings_obj = _open(Path(raw))
            self.assertEqual(_vote(book, "scout", WIN), "waiting")
            self.assertEqual(_vote(book, "scout", WIN), "waiting")
            self.assertEqual(_vote(book, "shield", WIN), "rejected")
            book.try_entry(
                chain="sol",
                contract=WIN,
                symbol="T",
                quote=_quote(clock, WIN, "1"),
                view=_view(clock, WIN),
                gas_usd=Decimal(0),
                gas_source="synthetic_zero",
            )
            self.assertEqual(ledger.count_open("s"), 0)
            self.assertEqual(ledger.cash("s"), Decimal("500"))

    def test_two_distinct_votes_buy_and_a_duplicate_does_not(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            book, ledger, clock, _settings_obj = _open(Path(raw))
            _vote(book, "scout", WIN)
            self.assertEqual(_vote(book, "whale", WIN), "quorum")
            book.try_entry(
                chain="sol",
                contract=WIN,
                symbol="T",
                quote=_quote(clock, WIN, "1"),
                view=_view(clock, WIN),
                gas_usd=Decimal(0),
                gas_source="synthetic_zero",
            )
            self.assertEqual(ledger.count_open("s"), 1)
            self.assertEqual(ledger.cash("s"), Decimal("450"))
            book.try_entry(
                chain="sol",
                contract=WIN,
                symbol="T",
                quote=_quote(clock, WIN, "1"),
                view=_view(clock, WIN),
                gas_usd=Decimal(0),
                gas_source="synthetic_zero",
            )
            self.assertEqual(ledger.count_open("s"), 1)
            self.assertEqual(ledger.cash("s"), Decimal("450"))

    def test_expired_vote_drops_out_of_quorum(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            book, _ledger, clock, _settings_obj = _open(Path(raw))
            _vote(book, "scout", WIN)
            clock.advance(100)
            self.assertEqual(_vote(book, "whale", WIN), "waiting")

    def test_paper_mode_refuses_synthetic_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            settings = replace(
                default_settings(mode="paper"), slippage=Decimal(0), default_fee_bps=0
            )
            ledger = Ledger(Path(raw) / "desk.sqlite")
            clock = ManualClock()
            ledger.create_session(
                session_id="s",
                mode="paper",
                started_at=clock(),
                ends_at=clock() + timedelta(hours=1),
                cash=Decimal("500"),
                config={},
                coverage={},
            )
            book = Book(ledger, settings, "s", clock)
            self.assertEqual(_vote(book, "scout", WIN), "rejected")
            book.record_signal(
                agent="scout",
                chain="sol",
                contract=WIN,
                price=Decimal("1"),
                reason="volume_spike",
                symbol="T",
                evidence={"origin": "market"},
            )
            book.record_signal(
                agent="whale",
                chain="sol",
                contract=WIN,
                price=Decimal("1"),
                reason="smart_money_convergence",
                symbol="T",
                evidence={"origin": "market"},
            )
            book.try_entry(
                chain="sol",
                contract=WIN,
                symbol="T",
                quote=_quote(clock, WIN, "1"),
                view=_view(clock, WIN),
                gas_usd=Decimal(0),
                gas_source="synthetic_zero",
            )
            self.assertEqual(ledger.count_open("s"), 0)


class RiskTests(unittest.TestCase):
    def test_cash_cap_and_eleventh_position(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            book, ledger, clock, _settings_obj = _open(Path(raw), cash=Decimal("40"))
            _vote(book, "scout", WIN)
            _vote(book, "whale", WIN)
            book.try_entry(
                chain="sol",
                contract=WIN,
                symbol="T",
                quote=_quote(clock, WIN, "1"),
                view=_view(clock, WIN),
                gas_usd=Decimal(0),
                gas_source="synthetic_zero",
            )
            self.assertEqual(ledger.count_open("s"), 0)
            self.assertGreaterEqual(ledger.cash("s"), 0)

        with tempfile.TemporaryDirectory() as raw:
            book, ledger, clock, _settings_obj = _open(Path(raw), cash=Decimal("500"))
            for index in range(10):
                contract = f"{index + 1:032d}".replace("0", "1")
                # 32 chars of base58. Digits 1-9 are valid; build from the index.
                contract = (
                    "1" * 31
                ) + "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"[index]
                _vote(book, "scout", contract)
                _vote(book, "whale", contract)
                book.try_entry(
                    chain="sol",
                    contract=contract,
                    symbol="T",
                    quote=_quote(clock, contract, "1"),
                    view=_view(clock, contract),
                    gas_usd=Decimal(0),
                    gas_source="synthetic_zero",
                )
            self.assertEqual(ledger.count_open("s"), 10)
            self.assertEqual(ledger.cash("s"), Decimal("0"))
            extra = "1" * 31 + "z"
            _vote(book, "scout", extra)
            _vote(book, "whale", extra)
            book.try_entry(
                chain="sol",
                contract=extra,
                symbol="T",
                quote=_quote(clock, extra, "1"),
                view=_view(clock, extra),
                gas_usd=Decimal(0),
                gas_source="synthetic_zero",
            )
            self.assertEqual(ledger.count_open("s"), 10)
            self.assertEqual(ledger.cash("s"), Decimal("0"))

    def test_concurrent_buys_cannot_overspend(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            book, ledger, clock, _settings_obj = _open(Path(raw), cash=Decimal("50"))
            first = "1" * 32
            second = "2" * 32
            _vote(book, "scout", first)
            _vote(book, "whale", first)
            _vote(book, "scout", second)
            _vote(book, "whale", second)
            barrier = threading.Barrier(2)

            def buy(contract: str) -> None:
                barrier.wait()
                book.try_entry(
                    chain="sol",
                    contract=contract,
                    symbol="T",
                    quote=_quote(clock, contract, "1"),
                    view=_view(clock, contract),
                    gas_usd=Decimal(0),
                    gas_source="synthetic_zero",
                )

            threads = [
                threading.Thread(target=buy, args=(first,)),
                threading.Thread(target=buy, args=(second,)),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(ledger.count_open("s"), 1)
            self.assertEqual(ledger.cash("s"), Decimal("0"))

    def test_unknown_stale_and_honeypot_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            book, ledger, clock, settings = _open(Path(raw))
            now = clock()
            unknown = _view(clock, WIN, fetch_error="down")
            self.assertIn(
                "security_fetch_failed",
                shield_vetoes(unknown, _quote(clock, WIN, "1"), settings, now),
            )
            stale = _quote(clock, WIN, "1")
            stale = replace(stale, fetched_at=now - timedelta(hours=1))
            self.assertIn(
                "quote_stale", shield_vetoes(_view(clock, WIN), stale, settings, now)
            )
            thin = replace(_quote(clock, WIN, "1"), liquidity_usd=None)
            self.assertIn(
                "liquidity_unknown_or_thin",
                shield_vetoes(_view(clock, WIN), thin, settings, now),
            )
            honey = _view(clock, "0x" + "ab" * 20, chain="base", honeypot="yes")
            self.assertIn(
                "honeypot",
                shield_vetoes(
                    honey, _quote(clock, honey.contract, "1", "base"), settings, now
                ),
            )
            missing = _view(clock, honey.contract, chain="base", honeypot="missing")
            self.assertIn(
                "honeypot_unknown",
                shield_vetoes(
                    missing, _quote(clock, honey.contract, "1", "base"), settings, now
                ),
            )
            _vote(book, "scout", WIN)
            _vote(book, "whale", WIN)
            book.try_entry(
                chain="sol",
                contract=WIN,
                symbol="T",
                quote=stale,
                view=_view(clock, WIN),
                gas_usd=Decimal(0),
                gas_source="synthetic_zero",
            )
            self.assertEqual(ledger.count_open("s"), 0)


class SniperTests(unittest.TestCase):
    def test_lock_dev_and_bundle_filters_do_not_relax(self) -> None:
        settings = _settings()
        clean = SecurityView(
            chain="sol",
            contract=WIN,
            fetched_at=datetime(2026, 11, 8, tzinfo=UTC),
            origin="market",
            lock_percent=Decimal("1"),
            dev_team_hold_rate=Decimal("0.049"),
            bundler_rate=Decimal(0),
            honeypot="empty",
            curve=CURVE_GRADUATED,
            curve_evidence="migrated",
            fetch_error=None,
            field_sources={},
        )
        self.assertEqual(sniper_blocks(clean, settings), [])
        self.assertIn(
            "curve_ungraduated",
            sniper_blocks(replace(clean, curve=CURVE_UNGRADUATED), settings),
        )
        self.assertIn(
            "liquidity_not_locked",
            sniper_blocks(replace(clean, lock_percent=Decimal("0.99")), settings),
        )
        self.assertIn(
            "liquidity_lock_unknown",
            sniper_blocks(replace(clean, lock_percent=None), settings),
        )
        self.assertIn(
            "dev_holdings_not_under_5pct",
            sniper_blocks(replace(clean, dev_team_hold_rate=Decimal("0.05")), settings),
        )
        self.assertIn(
            "dev_holdings_unknown",
            sniper_blocks(replace(clean, dev_team_hold_rate=None), settings),
        )
        self.assertIn(
            "bundled_supply_not_zero",
            sniper_blocks(replace(clean, bundler_rate=Decimal("0.01")), settings),
        )
        locked_curve = replace(
            clean, curve=CURVE_UNGRADUATED, lock_percent=Decimal("1")
        )
        self.assertIn("curve_ungraduated", sniper_blocks(locked_curve, settings))

    def test_volume_bundle_field_is_not_supply(self) -> None:
        view = build_security_view(
            chain="sol",
            contract=WIN,
            fetched_at=datetime(2026, 11, 8, tzinfo=UTC),
            origin="market",
            curve=CURVE_GRADUATED,
            curve_evidence="test",
            documents=[
                (
                    "token_security",
                    {"bundler_trader_amount_rate": "0", "dev_team_hold_rate": "0.01"},
                )
            ],
        )
        self.assertIsNone(view.bundler_rate)
        self.assertEqual(view.dev_team_hold_rate, Decimal("0.01"))


class ExecutionTests(unittest.TestCase):
    def test_partials_in_one_update_trail_stop_gap_and_identity(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            book, ledger, clock, settings = _open(Path(raw))
            run_scripted_scenario(book, clock)
            fills = ledger.fills("s")
            reasons = [row["reason"] for row in fills]
            self.assertEqual(
                reasons,
                [
                    "quorum_entry",
                    "tp1_partial",
                    "tp2_partial",
                    "trail_trigger_gap",
                    "quorum_entry",
                    "stop_trigger_gap",
                ],
            )
            self.assertEqual(Decimal(fills[1]["mid_price"]), Decimal("4"))
            self.assertEqual(Decimal(fills[2]["mid_price"]), Decimal("4"))
            stop = fills[5]
            self.assertEqual(Decimal(stop["mid_price"]), Decimal("0.80"))
            self.assertLess(Decimal(stop["exec_price"]), Decimal(stop["mid_price"]))
            self.assertLess(Decimal(stop["exec_price"]), Decimal("0.94"))
            self.assertEqual(json.loads(stop["votes_json"]), [])
            report = build_report(ledger, "s", settings)
            realized = Decimal(report["realized_pnl_usd"])
            self.assertEqual(ledger.cash("s"), Decimal("500") + realized)
            self.assertEqual(Decimal(report["winrate"]), Decimal("0.5"))
            self.assertEqual(report["best_trade"]["symbol"], "WIN")
            self.assertEqual(report["worst_trade"]["symbol"], "LOSS")
            self.assertEqual(report["unrealized_pnl_usd"], "0.00000000")
            self.assertIsNotNone(report["definitions"]["winrate"])
            self.assertEqual(ledger.count_open("s"), 0)
            for row in fills:
                self.assertGreaterEqual(Decimal(row["cash_after"]), 0)

    def test_restart_keeps_cash_position_and_vote(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw)
            book, ledger, clock, _settings_obj = _open(path)
            _vote(book, "scout", WIN)
            _vote(book, "whale", WIN)
            book.try_entry(
                chain="sol",
                contract=WIN,
                symbol="T",
                quote=_quote(clock, WIN, "1"),
                view=_view(clock, WIN),
                gas_usd=Decimal(0),
                gas_source="synthetic_zero",
            )
            ledger.close()
            again = Ledger(path / "desk.sqlite")
            self.assertEqual(again.cash("s"), Decimal("450"))
            self.assertEqual(again.count_open("s"), 1)
            votes = again.fresh_votes("s", "sol", WIN, clock(), 90)
            self.assertEqual({vote.agent for vote in votes}, {"scout", "whale"})
            again.close()

    def test_fee_ratio_and_gas_and_unfilled_exit(self) -> None:
        fraction, source = fee_fraction("0.1", 30)
        self.assertEqual(fraction, Decimal("0.001"))
        self.assertEqual(source, "gmgn_pool_fee_ratio_percent_points")
        self.assertEqual(fee_fraction(None, 30)[1], "configured_default_bps")
        self.assertIsNone(fee_fraction("nope", 30)[0])
        gas, gas_source = resolve_gas_usd(
            "sol",
            {"average": "5000", "native_token_usd_price": "100"},
            fallback_usd=Decimal("0.02"),
            evm_gas_units=300_000,
            max_plausible_usd=Decimal("5"),
        )
        self.assertEqual(gas, Decimal("0.00050000"))
        self.assertEqual(gas_source, "gmgn_gas_price.average_lamports")
        refused = plan_buy(
            mid=Decimal("1"),
            liquidity=Decimal("100"),
            budget=Decimal("50"),
            fee_raw=None,
            gas_usd=Decimal(0),
            gas_source="test",
            slippage=Decimal(0),
            max_impact=Decimal("0.10"),
            default_fee_bps=0,
        )
        self.assertFalse(refused.ok)
        self.assertEqual(refused.reason, "impact_above_cap")
        with tempfile.TemporaryDirectory() as raw:
            book, ledger, clock, _settings_obj = _open(Path(raw))
            _vote(book, "scout", WIN)
            _vote(book, "whale", WIN)
            book.try_entry(
                chain="sol",
                contract=WIN,
                symbol="T",
                quote=_quote(clock, WIN, "1"),
                view=_view(clock, WIN),
                gas_usd=Decimal(0),
                gas_source="synthetic_zero",
            )
            with ledger.transaction() as conn:
                conn.execute("UPDATE cash SET cash_usd = '0' WHERE session_id = 's'")
            book.on_mark(
                replace(
                    _quote(clock, WIN, "0.01"), liquidity_usd=Decimal("1000000000")
                ),
                Decimal("100"),
                "synthetic_huge_gas",
            )
            self.assertEqual(ledger.count_open("s"), 1)
            self.assertEqual(ledger.cash("s"), Decimal("0"))


class SignalTests(unittest.TestCase):
    def test_scout_and_whale_thresholds(self) -> None:
        settings = _settings()
        rows = [
            {
                "address": WIN,
                "price": "1",
                "volume": "25000",
                "price_change_percent": "15",
                "symbol": "A",
            },
            {
                "address": LOSS,
                "price": "1",
                "volume": "25000",
                "price_change_percent": "14",
                "symbol": "B",
            },
            {
                "address": "1" * 31 + "3",
                "price": None,
                "volume": "999999",
                "price_change_percent": "80",
            },
        ]
        hits = scout_hits(rows, chain="sol", role="spike", settings=settings)
        self.assertEqual([hit["contract"] for hit in hits], [WIN])
        now = datetime(2026, 11, 8, 17, 0, tzinfo=UTC)
        trades = [
            {
                "side": "buy",
                "transaction_hash": "a",
                "maker": "Wallet11111111111111111111111111111111",
                "base_address": WIN,
                "amount_usd": "150",
                "price_usd": "1",
                "timestamp": int(now.timestamp()) - 10,
            },
            {
                "side": "buy",
                "transaction_hash": "b",
                "maker": "Wallet22222222222222222222222222222222",
                "base_address": WIN,
                "amount_usd": "150",
                "price_usd": "1",
                "timestamp": int(now.timestamp()) - 20,
            },
            {"side": "buy", "base_address": WIN, "maker": "no-hash"},
        ]
        buys = whale_buys(trades, chain="sol", now=now, settings=settings)
        self.assertTrue(cluster_ready(buys, settings))
        self.assertEqual(len(buys), 2)


class SessionTests(unittest.TestCase):
    def test_duration_display_and_demo_is_labeled(self) -> None:
        self.assertEqual(parse_duration("24h"), 86400)
        with self.assertRaises(ValueError):
            parse_duration("24")
        stamp = datetime(2026, 11, 8, 17, 0, tzinfo=UTC)
        self.assertEqual(format_utc(stamp), "2026-11-08T17:00:00+00:00")
        self.assertEqual(format_et(stamp), "2026-11-08 12:00:00 ET")
        with tempfile.TemporaryDirectory() as raw:
            folder = run_demo_session(_settings(), Path(raw))
            manifest = json.loads(
                (folder / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["mode"], "synthetic")
            self.assertEqual(manifest["status"], "completed")
            self.assertFalse(manifest["live_trading"])
            text = (folder / "report.md").read_text(encoding="utf-8")
            self.assertIn("completed_position", text)
            self.assertIn("simulated", text.lower())

    def test_live_and_missing_key_do_not_start(self) -> None:
        self.assertEqual(main(["live"]), 2)
        self.assertEqual(main(["--mode=live", "demo"]), 2)
        with tempfile.TemporaryDirectory() as raw:
            previous = os.environ.pop("GMGN_API_KEY", None)
            try:
                code = main(
                    [
                        "--data-dir",
                        raw,
                        "--config",
                        str(ROOT / "missing-paper-config.yaml"),
                        "paper",
                        "--duration",
                        "24h",
                    ]
                )
            finally:
                if previous is not None:
                    os.environ["GMGN_API_KEY"] = previous
            self.assertEqual(code, 3)
            self.assertFalse((Path(raw) / "CURRENT").exists())
            blocked = json.loads(
                (Path(raw) / "not_started.json").read_text(encoding="utf-8")
            )
            self.assertEqual(blocked["twenty_four_hour_session"], "not_started")

    def test_hard_caps_cannot_be_loosened(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "paper.yaml"
            path.write_text("max_position_usd: 100\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_settings(path, mode="paper")

    def test_outage_does_not_invent_a_vote(self) -> None:
        async def run() -> int:
            with tempfile.TemporaryDirectory() as raw:
                book, ledger, _clock, settings = _open(Path(raw))
                stop = asyncio.Event()

                class Down:
                    origin = "market"

                    async def rank(self, *_args: object) -> list[object]:
                        stop.set()
                        raise MarketError("down")

                    async def smartmoney(self, *_args: object) -> list[object]:
                        raise MarketError("down")

                    async def trenches(self) -> dict[str, list[object]]:
                        raise MarketError("down")

                    async def bundle(self, *_args: object) -> tuple[object, ...]:
                        raise MarketError("down")

                    async def mark(self, *_args: object) -> tuple[object, ...]:
                        raise MarketError("down")

                await scout_agent(book, Down(), settings, stop, asyncio.Event(), {})
                return len(ledger.fills("s"))

        self.assertEqual(asyncio.run(run()), 0)

    def test_launch_queue_drops_when_full(self) -> None:
        async def run() -> int:
            queue: asyncio.Queue[dict[str, str]] = asyncio.Queue(maxsize=1)
            item = {"stage": "create", "contract": WIN}
            await _offer(queue, item)
            await _offer(queue, {"stage": "create", "contract": LOSS})
            return queue.qsize()

        self.assertEqual(asyncio.run(run()), 1)

    def test_sources_do_not_call_signing_paths(self) -> None:
        banned = (
            "from trading",
            "import trading",
            "build_and_send",
            "Keypair.from",
            "solders.keypair",
        )
        for path in (SRC / "paper_desk").glob("*.py"):
            text = path.read_text(encoding="utf-8")
            for token in banned:
                self.assertNotIn(token, text, f"{path.name} contains {token}")
        client = (SRC / "paper_desk" / "gmgn.py").read_text(encoding="utf-8")
        self.assertNotIn("/v1/trade/swap", client)
        self.assertNotIn("/v1/trade/quote", client)


class PlanSellTests(unittest.TestCase):
    def test_close_allocates_remaining_cost(self) -> None:
        plan = plan_sell(
            mid=Decimal("2"),
            liquidity=Decimal("1000000000"),
            qty=Decimal("1"),
            fee_raw=None,
            gas_usd=Decimal(0),
            gas_source="test",
            slippage=Decimal(0),
            max_impact=Decimal("0.1"),
            default_fee_bps=0,
            remaining_qty=Decimal("1"),
            remaining_cost=Decimal("1.25"),
            close_all=True,
        )
        self.assertTrue(plan.ok)
        self.assertEqual(plan.allocated_cost, Decimal("1.25"))
        self.assertEqual(plan.realized_pnl, Decimal("0.75000000"))


if __name__ == "__main__":
    unittest.main()
