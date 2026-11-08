"""Session runner. A 24h paper session ends itself and writes a recap.

This module does not start a live trader, even when a key is present.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from paper_desk.agents import (
    exit_agent,
    scout_agent,
    shield_agent,
    sniper_agent,
    whale_agent,
)
from paper_desk.book import Book
from paper_desk.config import Settings
from paper_desk.coverage import coverage_document
from paper_desk.demo import ManualClock, run_scripted_scenario
from paper_desk.errors import MarketError, PrerequisitesMissingError
from paper_desk.gmgn import HOST, GmgnClient, RateBucket
from paper_desk.launch_bridge import resolve_listener, stream_launches
from paper_desk.ledger import Ledger
from paper_desk.logging_setup import configure_logging
from paper_desk.report import build_report, write_report
from paper_desk.source import GmgnSource
from paper_desk.timeutil import add_seconds, format_et, format_utc, parse_utc, utc_now

_IGNORED = (
    "SOLANA_PRIVATE_KEY",
    "GMGN_PRIVATE_KEY",
    "KEYSTORE_PASSWORD",
    "PUMPFUN_PASSWORD",
)


def session_dir(data_dir: Path, session_id: str) -> Path:
    """Directory for one session's ledger, manifest, and report."""
    return data_dir / session_id


def new_session_id(now: datetime) -> str:
    """Time-prefixed id. Unique enough for a single operator."""
    return now.strftime("%Y%m%dT%H%M%SZ") + "-" + os.urandom(3).hex()


async def run_market_session(
    settings: Settings,
    data_dir: Path,
    duration_seconds: int,
    *,
    resume_id: str | None = None,
) -> Path:
    """Run read-only paper mode until the session deadline, then write the recap.

    Raises:
        PrerequisitesMissingError: When `GMGN_API_KEY` is unset. No session is marked running.
    """
    if settings.mode != "paper":
        msg = "market session mode must be paper"
        raise ValueError(msg)
    if not os.environ.get("GMGN_API_KEY"):
        raise PrerequisitesMissingError(["GMGN_API_KEY"])
    data_dir.mkdir(parents=True, exist_ok=True)
    if resume_id:
        return await _resume(settings, data_dir, resume_id)
    started = utc_now()
    session_id = new_session_id(started)
    ends = add_seconds(started, duration_seconds)
    folder = session_dir(data_dir, session_id)
    folder.mkdir(parents=True, exist_ok=True)
    listener = resolve_listener(settings.listener)
    coverage = coverage_document(settings, listener)
    ledger = Ledger(folder / "desk.sqlite")
    ledger.create_session(
        session_id=session_id,
        mode="paper",
        started_at=started,
        ends_at=ends,
        cash=settings.initial_cash_usd,
        config=settings.to_public_dict(),
        coverage=coverage,
    )
    _write_manifest(
        folder,
        session_id=session_id,
        mode="paper",
        status="running",
        started=started,
        ends=ends,
        settings=settings,
        coverage=coverage,
        listener=listener,
    )
    (data_dir / "CURRENT").write_text(session_id + "\n", encoding="utf-8")
    try:
        await _run_tasks(ledger, settings, folder, session_id, ends, listener)
    finally:
        ledger.close()
    return folder


def run_demo_session(settings: Settings, data_dir: Path) -> Path:
    """Write a synthetic session. It is never labeled as a market session."""
    if settings.mode != "synthetic":
        msg = "demo session mode must be synthetic"
        raise ValueError(msg)
    started = utc_now()
    session_id = new_session_id(started)
    ends = add_seconds(started, 60)
    folder = session_dir(data_dir, session_id)
    folder.mkdir(parents=True, exist_ok=True)
    configure_logging(folder / "desk.log")
    listener = {"mode": "off", "reason": "synthetic_demo_has_no_market_feed"}
    coverage = coverage_document(settings, listener)
    coverage["data_origin"] = "synthetic"
    ledger = Ledger(folder / "desk.sqlite")
    clock = ManualClock(started)
    ledger.create_session(
        session_id=session_id,
        mode="synthetic",
        started_at=clock(),
        ends_at=ends,
        cash=settings.initial_cash_usd,
        config=settings.to_public_dict(),
        coverage=coverage,
    )
    book = Book(ledger, settings, session_id, clock)
    run_scripted_scenario(book, clock)
    ledger.set_status(session_id, "completed", clock())
    report = build_report(ledger, session_id, settings)
    write_report(folder / "report.json", folder / "report.md", report)
    _write_manifest(
        folder,
        session_id=session_id,
        mode="synthetic",
        status="completed",
        started=started,
        ends=ends,
        settings=settings,
        coverage=coverage,
        listener=listener,
    )
    (data_dir / "CURRENT").write_text(session_id + "\n", encoding="utf-8")
    ledger.close()
    return folder


async def preflight(plan: str) -> dict[str, Any]:
    """Check read-only credentials. Does not open a trading session."""
    key = os.environ.get("GMGN_API_KEY", "")
    report: dict[str, Any] = {
        "gmgn_api_key_configured": bool(key),
        "gmgn_plan": plan,
        "gmgn_host": HOST,
        "wss_configured": bool(os.environ.get("SOLANA_NODE_WSS_ENDPOINT")),
        "rpc_configured": bool(os.environ.get("SOLANA_NODE_RPC_ENDPOINT")),
        "geyser_configured": bool(
            os.environ.get("GEYSER_ENDPOINT") and os.environ.get("GEYSER_API_TOKEN")
        ),
        "signing_material_present_and_ignored": [
            name for name in _IGNORED if os.environ.get(name)
        ],
        "ready_for_paper": False,
        "twenty_four_hour_session": "not_started",
        "live_trading": "refused",
        "minimum_for_real_paper": ["GMGN_API_KEY"],
        "optional_for_solana_launch_listeners": [
            "SOLANA_NODE_WSS_ENDPOINT (pump.fun logs and letsbonk blocks)",
            "GEYSER_ENDPOINT and GEYSER_API_TOKEN (geyser; shreds are not used)",
        ],
        "not_required": [
            "SOLANA_PRIVATE_KEY",
            "GMGN_PRIVATE_KEY",
            "SOLANA_NODE_RPC_ENDPOINT (paper desk does not send RPC trades)",
        ],
    }
    report["host_probe"] = await _probe_host()
    if report["wss_configured"]:
        report["wss_probe"] = await _probe_wss()
    if not key:
        report["smoke"] = "not_attempted_missing_GMGN_API_KEY"
        return report
    try:
        async with GmgnClient(
            key, RateBucket(plan if plan in {"free", "plus", "pro"} else "free")
        ) as client:
            await client.user_info()
            rows = await client.rank("sol", "1h", "volume", 1)
        sample: dict[str, Any] = {
            "user_info": "ok",
            "rank_rows": len(rows),
            "chain": "sol",
        }
        if rows and isinstance(rows[0], dict):
            sample["sample_address"] = rows[0].get("address")
            sample["sample_has_price"] = rows[0].get("price") is not None
        report["smoke"] = sample
        report["ready_for_paper"] = True
    except MarketError as exc:
        report["smoke"] = {"error": str(exc)}
        report["ready_for_paper"] = False
    return report


async def _resume(settings: Settings, data_dir: Path, session_id: str) -> Path:
    folder = session_dir(data_dir, session_id)
    manifest_path = folder / "manifest.json"
    if not manifest_path.exists():
        msg = f"no manifest for {session_id}"
        raise FileNotFoundError(msg)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("mode") != "paper":
        msg = "refusing to resume a session that is not paper mode"
        raise ValueError(msg)
    ends = parse_utc(str(manifest["ends_at_utc"]))
    if manifest.get("status") == "completed" or utc_now() >= ends:
        _finalize_files(folder, session_id, settings, "completed")
        return folder
    pid = int(manifest.get("pid") or 0)
    if pid and pid != os.getpid() and _pid_alive(pid):
        msg = f"session {session_id} is already running as pid {pid}"
        raise RuntimeError(msg)
    listener = manifest.get("listener")
    if not isinstance(listener, dict):
        listener = resolve_listener(settings.listener)
    ledger = Ledger(folder / "desk.sqlite")
    try:
        await _run_tasks(ledger, settings, folder, session_id, ends, listener)
    finally:
        ledger.close()
    return folder


async def _run_tasks(
    ledger: Ledger,
    settings: Settings,
    folder: Path,
    session_id: str,
    ends: datetime,
    listener: dict[str, Any],
) -> None:
    configure_logging(folder / "desk.log")
    book = Book(ledger, settings, session_id, utc_now)
    _log_ignored(book)
    book.note(
        "coverage",
        "Paper session started. Live trading is refused. Universe is the documented sample, not every token.",
        {"ends_at_utc": format_utc(ends), "ends_at_et": format_et(ends)},
    )
    stop = asyncio.Event()
    wake = asyncio.Event()
    health: dict[str, Any] = {}
    marks: dict[tuple[str, str], Any] = {}
    launches: asyncio.Queue[dict[str, str]] = asyncio.Queue(maxsize=settings.queue_size)
    key = os.environ["GMGN_API_KEY"]
    plan = settings.gmgn_plan
    async with GmgnClient(key, RateBucket(plan)) as client:
        source = GmgnSource(client, settings)
        tasks = [
            asyncio.create_task(
                scout_agent(book, source, settings, stop, wake, health)
            ),
            asyncio.create_task(
                whale_agent(book, source, settings, stop, wake, health)
            ),
            asyncio.create_task(
                sniper_agent(book, source, settings, stop, wake, launches, health)
            ),
            asyncio.create_task(
                shield_agent(book, source, settings, stop, wake, health)
            ),
            asyncio.create_task(
                exit_agent(book, source, settings, stop, health, marks)
            ),
            asyncio.create_task(_health_loop(folder, session_id, health, stop, book)),
            asyncio.create_task(_deadline(ends, stop)),
        ]
        mode = str(listener.get("mode") or "off")
        if mode in {"logs_and_blocks", "geyser"}:
            tasks.append(asyncio.create_task(stream_launches(mode, launches, stop)))
        else:
            book.note(
                "coverage",
                "Solana launch listeners are off. Sniper uses the GMGN trenches sample only.",
                {"listener": listener},
            )
        await stop.wait()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    ledger.set_status(session_id, "completed", utc_now())
    report = build_report(ledger, session_id, settings, marks)
    write_report(folder / "report.json", folder / "report.md", report)
    _mark_manifest(folder, "completed")
    _write_status(folder, session_id, "completed", health)


async def _deadline(ends: datetime, stop: asyncio.Event) -> None:
    while not stop.is_set():
        remaining = (ends - utc_now()).total_seconds()
        if remaining <= 0:
            stop.set()
            return
        try:
            await asyncio.wait_for(stop.wait(), timeout=min(remaining, 1))
        except TimeoutError:
            continue


async def _health_loop(
    folder: Path,
    session_id: str,
    health: dict[str, Any],
    stop: asyncio.Event,
    book: Book,
) -> None:
    while not stop.is_set():
        _write_status(folder, session_id, "running", health)
        payload = {
            "session_id": session_id,
            "beat_utc": format_utc(book.now()),
            "beat_et": format_et(book.now()),
            "cash_usd": format(book.ledger.cash(session_id), "f"),
            "open_positions": book.ledger.count_open(session_id),
            "agents": health,
        }
        (folder / "health.json").write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        try:
            await asyncio.wait_for(stop.wait(), timeout=5)
        except TimeoutError:
            continue


def _finalize_files(
    folder: Path, session_id: str, settings: Settings, status: str
) -> None:
    ledger = Ledger(folder / "desk.sqlite")
    try:
        row = ledger.session_row(session_id)
        if row and row["status"] != "completed":
            ledger.set_status(session_id, status, utc_now())
        if not (folder / "report.json").exists():
            report = build_report(ledger, session_id, settings)
            write_report(folder / "report.json", folder / "report.md", report)
    finally:
        ledger.close()
    _mark_manifest(folder, status)


def _write_manifest(
    folder: Path,
    *,
    session_id: str,
    mode: str,
    status: str,
    started: datetime,
    ends: datetime,
    settings: Settings,
    coverage: dict[str, Any],
    listener: dict[str, Any],
) -> None:
    payload = {
        "session_id": session_id,
        "mode": mode,
        "status": status,
        "started_at_utc": format_utc(started),
        "started_at_et": format_et(started),
        "ends_at_utc": format_utc(ends),
        "ends_at_et": format_et(ends),
        "initial_cash_usd": format(settings.initial_cash_usd, "f"),
        "pid": os.getpid(),
        "live_trading": False,
        "signing": False,
        "listener": listener,
        "config": settings.to_public_dict(),
        "coverage": coverage,
    }
    (folder / "manifest.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    _write_status(folder, session_id, status, {})


def _mark_manifest(folder: Path, status: str) -> None:
    path = folder / "manifest.json"
    if not path.exists():
        return
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["status"] = status
    payload["ended_at_utc"] = format_utc(utc_now())
    payload["ended_at_et"] = format_et(utc_now())
    payload["pid"] = os.getpid()
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _write_status(
    folder: Path, session_id: str, status: str, health: dict[str, Any]
) -> None:
    payload = {
        "session_id": session_id,
        "status": status,
        "beat_utc": format_utc(utc_now()),
        "beat_et": format_et(utc_now()),
        "agents": health,
        "live_trading": False,
    }
    (folder / "status.json").write_text(
        json.dumps(payload, indent=2, default=str, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _log_ignored(book: Book) -> None:
    present = [name for name in _IGNORED if os.environ.get(name)]
    if present:
        book.note(
            "coverage",
            "Signing material is present and ignored. Paper mode cannot broadcast.",
            {"names": present},
        )


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


async def _probe_host() -> dict[str, Any]:
    import aiohttp

    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"{HOST}/v1/user/info") as resp:
                return {"http_status": resp.status, "authenticated": False}
    except (aiohttp.ClientError, TimeoutError) as exc:
        return {"error": type(exc).__name__}


async def _probe_wss() -> str:
    url = os.environ.get("SOLANA_NODE_WSS_ENDPOINT", "")
    try:
        import websockets
    except ImportError:
        return "ImportError"
    try:
        async with websockets.connect(url, open_timeout=5, close_timeout=2):
            return "connected"
    except (OSError, TimeoutError, websockets.exceptions.WebSocketException) as exc:
        return type(exc).__name__
