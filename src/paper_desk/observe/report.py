"""Observation recap. Performance figures are N/A, not zeros."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from paper_desk.observe.store import ObserveStore
from paper_desk.timeutil import format_et, parse_utc

NA = "N/A"


def build_observe_report(store: ObserveStore) -> dict[str, Any]:
    """Summarize one observation session from its own database."""
    started = store.get_meta("started_at")
    ended = store.get_meta("ended_at")
    elapsed = None
    if started and ended:
        elapsed = int((parse_utc(ended) - parse_utc(started)).total_seconds())
    decisions = store.decisions()
    coverage = store.coverage_rows()
    calls = store.call_rows()
    side_counts = Counter(row["side"] for row in decisions)
    reason_counts = Counter(row["reason"] for row in decisions)
    veto_hits = sum(row["seen_count"] for row in decisions if row["side"] == "veto")
    signal_hits = sum(row["seen_count"] for row in decisions if row["side"] == "signal")
    unknown_hits = sum(
        row["seen_count"]
        for row in decisions
        if "unknown" in row["reason"] or row["reason"].endswith("_not_fetched")
    )
    errors = [row for row in calls if not row["ok"]]
    returned = sum(row["returned_rows"] for row in coverage)
    return {
        "mode": "OBSERVATION_ONLY",
        "session_id": store.get_meta("session_id"),
        "status": store.get_meta("status"),
        "started_at_utc": started,
        "ended_at_utc": ended,
        "started_at_et": format_et(parse_utc(started)) if started else None,
        "ended_at_et": format_et(parse_utc(ended)) if ended else None,
        "elapsed_seconds": elapsed,
        "heartbeat_utc": store.get_meta("heartbeat_at"),
        "pools_returned": returned,
        "list_reads": len(coverage),
        "provider_calls": len(calls),
        "provider_errors": len(errors),
        "unique_decisions": len(decisions),
        "signal_observations": signal_hits,
        "veto_observations": veto_hits,
        "security_unknown_observations": unknown_hits,
        "decisions_by_side": dict(side_counts),
        "decisions_by_reason": dict(reason_counts),
        "unavailable_agents": ["whale", "exit"],
        "positions": 0,
        "trades": 0,
        "virtual_balance": "unused_not_deployed",
        "pnl": NA,
        "realized_pnl": NA,
        "unrealized_pnl": NA,
        "winrate": NA,
        "best_trade": NA,
        "worst_trade": NA,
        "fees": NA,
        "coverage_note": (
            "Lists are samples. new_pools is not every mint. "
            "trending_pools is not every token. Whale has no free "
            "performance-ranked feed and did not vote. Exit stayed idle "
            "because this mode opens no positions."
        ),
        "settings_to_evaluate": json.loads(store.get_meta("settings") or "{}"),
        "live_trading": False,
        "orders_sent": 0,
    }


def render_observe_markdown(report: dict[str, Any]) -> str:
    """Human recap. PnL lines say N/A."""
    lines = [
        f"# Observation recap {report.get('session_id')}",
        "",
        "Mode: `OBSERVATION_ONLY`. No orders were sent and no positions were opened.",
        f"Status: `{report.get('status')}`.",
        f"Period: {report.get('started_at_et')} to {report.get('ended_at_et')} "
        f"({report.get('elapsed_seconds')} seconds).",
        "",
        report.get("coverage_note") or "",
        "",
        "## Counts",
        "",
        f"- Pool rows returned: {report.get('pools_returned')}",
        f"- List reads: {report.get('list_reads')}",
        f"- Provider calls / errors: {report.get('provider_calls')} / {report.get('provider_errors')}",
        f"- Unique decisions: {report.get('unique_decisions')}",
        f"- Signal observations: {report.get('signal_observations')}",
        f"- Veto observations: {report.get('veto_observations')}",
        f"- Security-unknown observations: {report.get('security_unknown_observations')}",
        f"- Unavailable agents: {', '.join(report.get('unavailable_agents') or [])}",
        f"- Positions: {report.get('positions')}",
        "",
        "## Performance",
        "",
        "These are not zeros and not a hypothetical result. Nothing was traded.",
        "",
        f"- PnL: {report.get('pnl')}",
        f"- Win rate: {report.get('winrate')}",
        f"- Best trade: {report.get('best_trade')}",
        f"- Worst trade: {report.get('worst_trade')}",
        f"- Fees: {report.get('fees')}",
        f"- Virtual balance: {report.get('virtual_balance')}",
        "",
        "## Reasons",
        "",
    ]
    reasons = report.get("decisions_by_reason")
    if isinstance(reasons, dict) and reasons:
        for name, count in sorted(reasons.items()):
            lines.append(f"- {name}: {count}")
    else:
        lines.append("- none")
    lines.append("")
    return "\n".join(lines)


def write_observe_report(folder: Path, report: dict[str, Any]) -> None:
    """Write JSON and markdown next to the session database."""
    folder.joinpath("report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    folder.joinpath("report.md").write_text(
        render_observe_markdown(report), encoding="utf-8"
    )
