"""Command line. `live` refuses. `paper --duration 24h` is the market session."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from decimal import Decimal
from pathlib import Path

from paper_desk.config import Settings, default_settings, load_settings
from paper_desk.errors import LiveTradingRefusedError, PrerequisitesMissingError
from paper_desk.ledger import Ledger
from paper_desk.report import build_report, render_markdown
from paper_desk.runtime import preflight, run_demo_session, run_market_session
from paper_desk.timeutil import parse_duration

REFUSAL = (
    "Live trading is refused. This desk does not sign, broadcast, fund, or "
    "hand work to the upstream trader, even if SOLANA_PRIVATE_KEY or "
    "GMGN_PRIVATE_KEY is set. There is no automatic switch to live mode."
)

_LIVE_TOKENS = frozenset({"live", "--live", "--mode=live", "mode=live"})


def main(argv: list[str] | None = None) -> int:
    """Run one paper-desk command. Returns a process status."""
    args = list(sys.argv[1:] if argv is None else argv)
    if os.environ.get("PAPER_DESK_MODE", "").lower() in {
        "live",
        "real",
    } or _live_requested(args):
        print(REFUSAL)
        return 2
    parser = argparse.ArgumentParser(prog="paper_desk")
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--config", default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("preflight", help="Check read-only GMGN and listener credentials")
    sub.add_parser("demo", help="Run the labeled synthetic scenario and write a recap")
    sub.add_parser("live", help="Refused")
    sub.add_parser("status", help="Show the current session manifest and health")
    recap = sub.add_parser(
        "recap", help="Print realized, unrealized, fees, and win rate"
    )
    recap.add_argument("--session", default=None)

    paper = sub.add_parser(
        "paper", help="Real-market paper session. Does not broadcast."
    )
    paper.add_argument(
        "--duration", default=None, help="For example 24h. Ignored with --resume."
    )
    paper.add_argument("--cash", default="500")
    paper.add_argument("--resume", default=None)

    sub.add_parser(
        "observe-preflight",
        help="Keyless public-API smoke. Does not start a 24h session.",
    )
    observe = sub.add_parser(
        "observe",
        help="Keyless observation. Records vetoes and does not trade.",
    )
    observe.add_argument("--duration", default=None, help="For example 24h.")
    observe.add_argument("--resume", default=None)
    sub.add_parser("observe-status", help="Observation heartbeat and manifest")
    observe_recap = sub.add_parser(
        "observe-recap", help="Observation recap. PnL is N/A."
    )
    observe_recap.add_argument("--session", default=None)

    parsed = parser.parse_args(args)
    if parsed.command == "live":
        print(REFUSAL)
        return 2
    try:
        if parsed.command == "preflight":
            return _preflight(parsed)
        if parsed.command == "demo":
            return _demo(parsed)
        if parsed.command == "paper":
            return _paper(parsed)
        if parsed.command == "status":
            return _status(parsed)
        if parsed.command == "recap":
            return _recap(parsed)
        if parsed.command == "observe-preflight":
            return _observe_preflight(parsed)
        if parsed.command == "observe":
            return _observe(parsed)
        if parsed.command == "observe-status":
            return _observe_status(parsed)
        if parsed.command == "observe-recap":
            return _observe_recap(parsed)
    except LiveTradingRefusedError:
        print(REFUSAL)
        return 2
    except (ValueError, TypeError, FileNotFoundError, RuntimeError) as exc:
        print(exc)
        return 2
    except PrerequisitesMissingError as exc:
        _write_blocked(_paper_dir(parsed), exc.missing)
        print("Real-market paper mode was not started.")
        print("Missing read-only credential: " + ", ".join(exc.missing))
        print(
            "Optional listener credentials: SOLANA_NODE_WSS_ENDPOINT, or GEYSER_ENDPOINT plus GEYSER_API_TOKEN."
        )
        print("Wallet keys are not required and are ignored.")
        return 3
    print(REFUSAL)
    return 2


def _live_requested(args: list[str]) -> bool:
    for token in args:
        if token.lower() in _LIVE_TOKENS or token.lower().startswith("--mode=live"):
            return True
    return False


def _settings(
    parsed: argparse.Namespace, mode: str, cash: Decimal | None = None
) -> Settings:
    path = _path(parsed.config, "config/paper_desk.yaml")
    plan = os.environ.get("GMGN_PLAN")
    if path.exists():
        return load_settings(path, mode=mode, cash=cash, plan=plan)
    return load_settings(None, mode=mode, cash=cash, plan=plan)


def _path(given: str | None, fallback: str) -> Path:
    return Path(given) if given else Path(fallback)


def _paper_dir(parsed: argparse.Namespace) -> Path:
    return _path(parsed.data_dir, "data/paper_desk")


def _observe_dir(parsed: argparse.Namespace) -> Path:
    return _path(parsed.data_dir, "data/observe")


def _preflight(parsed: argparse.Namespace) -> int:
    plan = os.environ.get("GMGN_PLAN", "free")
    report = asyncio.run(preflight(plan))
    folder = _paper_dir(parsed)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "preflight.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    print("24h paper session: not started.")
    return 0


def _demo(parsed: argparse.Namespace) -> int:
    settings = _settings(parsed, "synthetic", Decimal("500"))
    folder = run_demo_session(settings, _paper_dir(parsed))
    print("SYNTHETIC DEMO. Not market data and not a paper-trading result.")
    print((folder / "report.md").read_text(encoding="utf-8"))
    return 0


def _paper(parsed: argparse.Namespace) -> int:
    cash = Decimal(parsed.cash)
    settings = _settings(parsed, "paper", cash)
    if parsed.resume:
        folder = asyncio.run(
            run_market_session(settings, _paper_dir(parsed), 0, resume_id=parsed.resume)
        )
    else:
        if not parsed.duration:
            print("paper requires --duration, for example --duration 24h")
            return 2
        seconds = parse_duration(parsed.duration)
        folder = asyncio.run(run_market_session(settings, _paper_dir(parsed), seconds))
    print(f"Session folder: {folder}")
    report = folder / "report.md"
    if report.exists():
        print(report.read_text(encoding="utf-8"))
    return 0


def _status(parsed: argparse.Namespace) -> int:
    folder = _current_folder(_paper_dir(parsed))
    if folder is None:
        print("No session has been started.")
        return 0
    for name in ("manifest.json", "status.json", "health.json"):
        path = folder / name
        print(f"## {name}")
        if path.exists():
            print(path.read_text(encoding="utf-8"))
        else:
            print("(missing)")
    return 0


def _recap(parsed: argparse.Namespace) -> int:
    data = _paper_dir(parsed)
    session_id = parsed.session
    if session_id is None:
        folder = _current_folder(data)
        if folder is None:
            print("No session has been started.")
            return 0
        session_id = folder.name
    else:
        folder = data / session_id
    ledger = Ledger(folder / "desk.sqlite")
    try:
        settings = _settings_from_manifest(folder)
        report = build_report(ledger, session_id, settings)
    finally:
        ledger.close()
    print(render_markdown(report))
    return 0


def _settings_from_manifest(folder: Path) -> Settings:
    payload = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    mode = str(payload.get("mode") or "paper")
    cash = Decimal(str(payload.get("initial_cash_usd") or "500"))
    return default_settings(
        mode=mode if mode in {"paper", "synthetic"} else "paper", cash=cash
    )


def _current_folder(data: Path) -> Path | None:
    pointer = data / "CURRENT"
    if not pointer.exists():
        return None
    session_id = pointer.read_text(encoding="utf-8").strip()
    folder = data / session_id
    if not folder.exists():
        return None
    return folder


def _observe_preflight(parsed: argparse.Namespace) -> int:
    from paper_desk.observe.preflight import observe_preflight

    report = asyncio.run(observe_preflight())
    folder = _observe_dir(parsed)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "preflight.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    print("24h observation session: not started.")
    return 0


def _observe(parsed: argparse.Namespace) -> int:
    from paper_desk.observe.config import load_observe_settings
    from paper_desk.observe.runner import run_observation

    settings = load_observe_settings(_path(parsed.config, "config/observe.yaml"))
    if parsed.resume:
        folder = asyncio.run(
            run_observation(settings, _observe_dir(parsed), 0, resume_id=parsed.resume)
        )
    else:
        if not parsed.duration:
            print("observe requires --duration, for example --duration 24h")
            return 2
        seconds = parse_duration(parsed.duration)
        folder = asyncio.run(run_observation(settings, _observe_dir(parsed), seconds))
    print(f"Observation folder: {folder}")
    report = folder / "report.md"
    if report.exists():
        print(report.read_text(encoding="utf-8"))
    return 0


def _observe_status(parsed: argparse.Namespace) -> int:
    folder = _current_folder(_observe_dir(parsed))
    if folder is None:
        print("No observation session has been started.")
        return 0
    for name in ("manifest.json", "status.json", "health.json", "checkpoint.json"):
        path = folder / name
        print(f"## {name}")
        print(path.read_text(encoding="utf-8") if path.exists() else "(missing)")
    return 0


def _observe_recap(parsed: argparse.Namespace) -> int:
    from paper_desk.observe.report import build_observe_report, render_observe_markdown
    from paper_desk.observe.store import ObserveStore

    data = _observe_dir(parsed)
    if parsed.session:
        folder = data / parsed.session
    else:
        folder = _current_folder(data)
    if folder is None or not (folder / "observe.sqlite").exists():
        print("No observation session has been started.")
        return 0
    store = ObserveStore(folder / "observe.sqlite")
    try:
        print(render_observe_markdown(build_observe_report(store)))
    finally:
        store.close()
    return 0


def _write_blocked(data: Path, missing: list[str]) -> None:
    data.mkdir(parents=True, exist_ok=True)
    payload = {
        "ready_for_paper": False,
        "twenty_four_hour_session": "not_started",
        "missing": missing,
        "live_trading": "refused",
    }
    (data / "not_started.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
