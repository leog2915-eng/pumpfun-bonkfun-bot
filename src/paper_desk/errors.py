"""Errors that mean a read failed. Callers must not invent a quote."""


class MarketError(Exception):
    """A market or security read failed, was rate-limited, or was the wrong shape."""


class LiveTradingRefusedError(Exception):
    """Live mode is not implemented and must not fall through to the live bot."""


class PrerequisitesMissingError(Exception):
    """Real-market paper mode has no read-only GMGN key."""

    def __init__(self, missing: list[str]) -> None:
        self.missing = missing
        super().__init__("missing " + ", ".join(missing))
