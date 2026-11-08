"""Per-minute budgets. A refused call is skipped, not replaced with a guess."""

from __future__ import annotations


class MinuteBudget:
    """Allow at most N events in any rolling 60 seconds."""

    def __init__(self, per_minute: int) -> None:
        if per_minute < 1:
            msg = "per_minute must be positive"
            raise ValueError(msg)
        self.per_minute = per_minute
        self._stamps: list[float] = []

    def allow(self, now: float) -> bool:
        """True when this call may proceed. The stamp is consumed on success."""
        self._stamps = [stamp for stamp in self._stamps if now - stamp < 60]
        if len(self._stamps) >= self.per_minute:
            return False
        self._stamps.append(now)
        return True
