"""Observation mode: no orders, unchanged veto rules, separate database."""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from paper_desk.cli import main  # noqa: E402
from paper_desk.observe.assess import (  # noqa: E402
    parse_gecko_pool,
    parse_lp_locked_pct,
    risk_from_goplus,
    risk_from_rugcheck,
    scout_reason,
    vetoes,
)
from paper_desk.observe.config import load_observe_settings  # noqa: E402
from paper_desk.observe.limits import MinuteBudget  # noqa: E402
from paper_desk.observe.providers import ReadError  # noqa: E402
from paper_desk.observe.runner import _cycle, execute_trade, run_observation  # noqa: E402
from paper_desk.observe.store import ObserveStore  # noqa: E402
from paper_desk.timeutil import UTC, utc_now  # noqa: E402

BASE = "0x" + "ab" * 20


def _gecko(
    address: str = BASE, dex: str = "pancakeswap_v2", prefix: str = "base"
) -> dict:
    return {
        "relationships": {
            "base_token": {"data": {"id": f"{prefix}_{address}", "type": "token"}},
            "dex": {"data": {"id": dex, "type": "dex"}},
        },
        "attributes": {
            "address": "pool-1",
            "name": "TOK / USD",
            "base_token_price_usd": "0.007620977150651470206239023534",
            "reserve_in_usd": "5000",
            "volume_usd": {"m5": "30000", "h1": "40000"},
            "price_change_percentage": {"m5": "20", "h1": "30"},
        },
    }


def _passing_goplus() -> dict:
    return {
        "is_honeypot": "0",
        "creator_percent": "0.01",
        "dev_team_hold_rate": "0.04",
        "bundler_rate": "0",
        "lpLockedPct": 100,
        "lockerScanStatus": "ok",
        "lockers": {"a": {"type": "locker", "permanent": True}},
    }


class Scripted:
    """In-memory lists. `fail` raises instead of returning a fabricated row."""

    def __init__(
        self, rows: list[object], goplus: dict | None = None, rug: dict | None = None
    ) -> None:
        self.rows = rows
        self.goplus_row = goplus or {}
        self.rug = rug or {}
        self.fail = False

    async def trending(self, _network: str) -> list[object]:
        if self.fail:
            raise ReadError("geckoterminal", "trending_pools", "down")
        return self.rows

    async def new_pools(self, _network: str) -> list[object]:
        if self.fail:
            raise ReadError("geckoterminal", "new_pools", "down")
        return self.rows

    async def rugcheck(self, _mint: str) -> dict:
        return self.rug

    async def goplus(self, _chain: str, _address: str) -> dict:
        return self.goplus_row


class AssessTests(unittest.TestCase):
    def test_percent_units_and_ignored_fields(self) -> None:
        self.assertEqual(parse_lp_locked_pct(100), Decimal("1"))
        self.assertEqual(parse_lp_locked_pct("99.9"), Decimal("99.9") / Decimal(100))
        self.assertIsNone(parse_lp_locked_pct(None))
        self.assertIsNone(parse_lp_locked_pct(150))
        now = datetime(2026, 11, 8, 17, tzinfo=UTC)
        rug = risk_from_rugcheck(
            {
                "creatorBalance": 0,
                "graphInsidersDetected": 0,
                "lockerScanStatus": "none",
                "lockers": {},
                "markets": [{"pubkey": "pool-1", "lp": {"lpLockedPct": 100}}],
            },
            "pool-1",
            now,
        )
        self.assertEqual(rug.lock_ratio, Decimal("1"))
        self.assertIsNone(rug.dev_team_hold_rate)
        self.assertIsNone(rug.bundler_rate)
        self.assertEqual(rug.insider_count_ignored, 0)
        self.assertEqual(rug.lock_kind, "pct_only")
        burned = risk_from_rugcheck(
            {
                "lockerScanStatus": "ok",
                "lockers": {"a": {"type": "burn"}},
                "markets": [{"pubkey": "pool-1", "lp": {"lpLockedPct": 100}}],
            },
            "pool-1",
            now,
        )
        self.assertEqual(burned.lock_kind, "burn")
        settings = load_observe_settings(None)
        pool, reason = parse_gecko_pool(
            "base", _gecko(), fetched_at=now, list_name="trending"
        )
        self.assertEqual(reason, "")
        self.assertIsNotNone(pool)
        self.assertEqual(pool.price_usd, Decimal("0.007620977150651470206239023534"))
        self.assertEqual(scout_reason(pool, settings), "volume_spike")
        self.assertIn(
            "liquidity_lock_pct_without_locker", vetoes(pool, rug, settings, now)
        )
        self.assertIn("dev_holdings_unknown", vetoes(pool, rug, settings, now))
        self.assertIn("bundled_supply_unknown", vetoes(pool, rug, settings, now))
        creator_only = risk_from_goplus(
            {"is_honeypot": "0", "creator_percent": "0.01"}, now
        )
        self.assertIsNone(creator_only.dev_team_hold_rate)
        self.assertIn("dev_holdings_unknown", vetoes(pool, creator_only, settings, now))
        curve_pool, _reason = parse_gecko_pool(
            "solana",
            _gecko(address="1" * 32, dex="pump-fun", prefix="solana"),
            fetched_at=now,
            list_name="new",
        )
        self.assertIsNotNone(curve_pool)
        self.assertIn("curve_ungraduated", vetoes(curve_pool, None, settings, now))
        stale = vetoes(pool, rug, settings, now + timedelta(hours=1))
        self.assertIn("quote_stale", stale)
        missing_change = parse_gecko_pool(
            "base",
            {
                "relationships": _gecko()["relationships"],
                "attributes": {
                    "address": "pool-1",
                    "name": "TOK / USD",
                    "base_token_price_usd": "1",
                    "reserve_in_usd": "5000",
                    "volume_usd": {},
                    "price_change_percentage": {},
                },
            },
            fetched_at=now,
            list_name="trending",
        )[0]
        self.assertIsNotNone(missing_change)
        self.assertIsNone(scout_reason(missing_change, settings))

    def test_config_cannot_weaken_safety(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "observe.yaml"
            path.write_text("max_dev_hold_rate: 0.2\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_observe_settings(path)
            path.write_text("gecko_per_minute: 30\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_observe_settings(path)
            path.write_text("scout_min_spike_volume_usd: 1\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_observe_settings(path)

    def test_rate_budget(self) -> None:
        budget = MinuteBudget(2)
        self.assertTrue(budget.allow(0))
        self.assertTrue(budget.allow(1))
        self.assertFalse(budget.allow(2))
        self.assertTrue(budget.allow(61))


class LoopTests(unittest.TestCase):
    def test_passing_filters_do_not_trade_and_recap_is_na(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                feeds = Scripted([_gecko()], goplus=_passing_goplus())
                clock = [datetime(2026, 11, 8, 18, tzinfo=UTC)]

                async def sleep(_seconds: float) -> None:
                    clock[0] = clock[0] + timedelta(seconds=1)

                folder = await run_observation(
                    load_observe_settings(ROOT / "config" / "observe.yaml"),
                    root,
                    2,
                    feeds=feeds,
                    now_fn=lambda: clock[0],
                    sleep_fn=sleep,
                )
                store = ObserveStore(folder / "observe.sqlite")
                try:
                    self.assertNotIn("positions", store.table_names())
                    self.assertNotIn("fills", store.table_names())
                    # First cycles are other chains; drive base trending then base new.
                    store.set_meta("rotation", "1")
                    await _cycle(store, load_observe_settings(None), feeds, clock[0])
                    store.set_meta("rotation", "4")
                    await _cycle(store, load_observe_settings(None), feeds, clock[0])
                    reasons = [row["reason"] for row in store.decisions()]
                    self.assertIn("filters_passed_not_traded", reasons)
                    self.assertIn("whale_unavailable_no_free_smart_money_feed", reasons)
                    self.assertIn("exit_idle_no_positions", reasons)
                    with self.assertRaises(RuntimeError):
                        execute_trade({"order": "buy"})
                    report_text = (folder / "report.md").read_text(encoding="utf-8")
                finally:
                    store.close()
                self.assertIn("N/A", report_text)
                self.assertIn("unused_not_deployed", report_text)
                self.assertNotIn("Win rate: 0", report_text)
                manifest = (folder / "manifest.json").read_text(encoding="utf-8")
                self.assertIn("OBSERVATION_ONLY", manifest)
                self.assertFalse((root / "desk.sqlite").exists())

        asyncio.run(run())

    def test_high_signal_without_evidence_does_not_clear(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as raw:
                folder = Path(raw) / "s"
                folder.mkdir()
                store = ObserveStore(folder / "observe.sqlite")
                store.set_meta("rotation", "0")
                feeds = Scripted(
                    [_gecko(address="1" * 32, dex="pumpswap", prefix="solana")],
                    rug={
                        "creatorBalance": 0,
                        "graphInsidersDetected": 0,
                        "lockerScanStatus": "none",
                        "lockers": {},
                        "markets": [{"pubkey": "pool-1", "lp": {"lpLockedPct": 100}}],
                    },
                )
                now = utc_now()
                await _cycle(store, load_observe_settings(None), feeds, now)
                sides = {row["side"] for row in store.decisions()}
                self.assertNotIn("would_enter_not_traded", sides)
                reasons = [row["reason"] for row in store.decisions()]
                self.assertTrue(
                    any(
                        item in reasons
                        for item in (
                            "dev_holdings_unknown",
                            "bundled_supply_unknown",
                            "honeypot_unknown",
                            "liquidity_lock_pct_without_locker",
                        )
                    )
                )
                store.set_meta("rotation", "0")
                await _cycle(store, load_observe_settings(None), feeds, now)
                signal = [
                    row for row in store.decisions() if row["reason"] == "volume_spike"
                ]
                self.assertEqual(len(signal), 1)
                self.assertGreater(signal[0]["seen_count"], 1)
                with self.assertRaises(ValueError):
                    store.upsert_vote("sol", "1" * 32, "whale", now, "nope")
                store.close()

        asyncio.run(run())

    def test_outage_resume_and_cli(self) -> None:
        async def run() -> Path:
            with tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                feeds = Scripted([], goplus={})
                feeds.fail = True
                clock = [datetime(2026, 11, 8, 19, tzinfo=UTC)]

                async def sleep(_seconds: float) -> None:
                    clock[0] = clock[0] + timedelta(hours=24)

                folder = await run_observation(
                    load_observe_settings(None),
                    root,
                    3600,
                    feeds=feeds,
                    now_fn=lambda: clock[0],
                    sleep_fn=sleep,
                )
                session = folder.name
                clock[0] = datetime(2026, 11, 8, 19, tzinfo=UTC)
                await run_observation(
                    load_observe_settings(None),
                    root,
                    0,
                    resume_id=session,
                    feeds=feeds,
                    now_fn=lambda: clock[0],
                    sleep_fn=sleep,
                )
                self.assertTrue((folder / "health.json").exists())
                self.assertTrue((folder / "report.json").exists())
                text = (folder / "report.md").read_text(encoding="utf-8")
                self.assertIn("PnL: N/A", text)
                self.assertIn("Positions: 0", text)
                return root

        asyncio.run(run())
        with tempfile.TemporaryDirectory() as raw:
            code = main(
                [
                    "--data-dir",
                    raw,
                    "--config",
                    str(ROOT / "config" / "observe.yaml"),
                    "observe",
                ]
            )
            self.assertEqual(code, 2)
            self.assertFalse((Path(raw) / "CURRENT").exists())


if __name__ == "__main__":
    unittest.main()
