"""What this desk actually watches. Not the whole market."""

from __future__ import annotations

from typing import Any

from paper_desk.config import Settings

GMGN_HOST = "https://openapi.gmgn.ai"
GMGN_COMMIT = "6a04e1f00b5463cfc965e41003ca0c7c2db5b69f"
GMGN_VERSION = "1.7.1"
UPSTREAM_COMMIT = "a22893ba0a17117fd27977284379f168ce118c69"
UPSTREAM_REPO = "https://github.com/chainstacklabs/pumpfun-bonkfun-bot"


def coverage_document(settings: Settings, listener: dict[str, str]) -> dict[str, Any]:
    """Static description of the supported universe and the known gaps."""
    return {
        "exhaustive": False,
        "chains": list(settings.chains),
        "excluded_chains": ["robinhood"],
        "gmgn": {
            "repo": "https://github.com/GMGNAI/gmgn-skills",
            "commit": GMGN_COMMIT,
            "version": GMGN_VERSION,
            "host": GMGN_HOST,
            "auth": "X-APIKEY exist-auth on read-only routes",
            "plan_bucket": settings.gmgn_plan,
            "routes": [
                "GET /v1/user/info",
                "GET /v1/market/rank",
                "GET /v1/token/info",
                "GET /v1/token/security",
                "GET /v1/token/pool_info",
                "GET /v1/user/smartmoney",
                "POST /v1/trenches",
                "GET /v1/trade/gas_price",
            ],
            "not_called": [
                "/v1/trade/swap",
                "/v1/trade/multi_swap",
                "/v1/trade/quote",
                "/v1/trade/strategy/create",
                "/v1/cooking/create_token",
                "/v1/trade/follow_wallet",
            ],
        },
        "scout": {
            "universe": (
                f"GMGN rank top {settings.rank_limit} per chain for "
                f"{settings.scout_spike_interval} (volume) and "
                f"{settings.scout_gainer_interval} (change). Not every token."
            ),
            "chains": list(settings.chains),
        },
        "whale": {
            "universe": (
                "GMGN smartmoney feed, limit 100 recent trades per chain. "
                "Platform-tagged wallets only, not every wallet."
            ),
            "window_seconds": settings.whale_window_seconds,
            "min_distinct_wallets": settings.whale_min_wallets,
        },
        "sniper": {
            "launchpads": ["pump.fun", "letsbonk.fun"],
            "chains": ["sol"],
            "base_bsc": (
                "No pump.fun or letsbonk adapter. Scout and whale still read "
                "GMGN rank and smartmoney on base and bsc. Sniper does not."
            ),
            "listener": listener,
            "trenches": (
                "POST /v1/trenches chain=sol types new_creation, "
                "near_completion, completed platforms Pump.fun and letsbonk, "
                f"limit {settings.trenches_limit} per type. A sample, not every launch."
            ),
            "bonding_curve": (
                "Ungraduated creates and trenches new_creation/near_completion "
                "are blocked. Locked liquidity is not inferred from virtual reserves."
            ),
            "shreds": "Refused. Shreds are pre-execution and are not entries.",
        },
        "upstream_listeners": {
            "repo": UPSTREAM_REPO,
            "commit": UPSTREAM_COMMIT,
            "license": "Apache-2.0",
            "reused": [
                "monitoring.listener_factory",
                "logs listener for pump.fun",
                "blocks listener for letsbonk.fun",
                "geyser listener for pump.fun and letsbonk.fun",
            ],
            "not_used": [
                "trading/",
                "bot_runner live orders",
                "shreds listener",
                "signing and broadcast",
            ],
        },
    }
