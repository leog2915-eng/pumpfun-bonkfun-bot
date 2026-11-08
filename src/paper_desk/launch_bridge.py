"""Read-only bridge onto the upstream launch listeners.

Importing a listener also imports platform instruction builders, because that
is how the upstream factory is built. This module never calls those builders,
never loads a keypair, and never sends a transaction.
"""

from __future__ import annotations

import asyncio
from typing import Any

from utils.logger import get_logger

logger = get_logger(__name__)


async def stream_launches(
    mode: str,
    queue: asyncio.Queue[dict[str, str]],
    stop: asyncio.Event,
) -> None:
    """Push create and migration events until `stop` is set.

    `mode` is `logs_and_blocks` or `geyser`. Endpoints come from the environment
    and are not logged.
    """
    import os

    from interfaces.core import Platform
    from monitoring.listener_factory import ListenerFactory

    async def on_token(info: Any) -> None:
        await _offer(
            queue,
            {
                "chain": "sol",
                "contract": str(info.mint),
                "symbol": str(info.symbol or ""),
                "platform": str(info.platform.value),
                "stage": "create",
                "pre_execution": "0",
                "source": mode,
            },
        )

    async def on_pool(pool: Any) -> None:
        stage = "pre_execution" if pool.pre_execution else "migrated"
        await _offer(
            queue,
            {
                "chain": "sol",
                "contract": str(pool.mint),
                "symbol": "",
                "platform": str(pool.platform.value),
                "stage": stage,
                "pre_execution": "1" if pool.pre_execution else "0",
                "source": mode,
            },
        )

    tasks: list[asyncio.Task[None]] = []
    if mode == "logs_and_blocks":
        wss = os.environ.get("SOLANA_NODE_WSS_ENDPOINT", "")
        logs = ListenerFactory.create_listener(
            "logs", wss_endpoint=wss, platforms=[Platform.PUMP_FUN]
        )
        blocks = ListenerFactory.create_listener(
            "blocks", wss_endpoint=wss, platforms=[Platform.LETS_BONK]
        )
        tasks.append(
            asyncio.create_task(logs.listen_for_tokens(on_token, pool_callback=on_pool))
        )
        tasks.append(
            asyncio.create_task(
                blocks.listen_for_tokens(on_token, pool_callback=on_pool)
            )
        )
    elif mode == "geyser":
        geyser = ListenerFactory.create_listener(
            "geyser",
            geyser_endpoint=os.environ.get("GEYSER_ENDPOINT", ""),
            geyser_api_token=os.environ.get("GEYSER_API_TOKEN", ""),
            geyser_auth_type=os.environ.get("GEYSER_AUTH_TYPE", "x-token"),
            platforms=[Platform.PUMP_FUN, Platform.LETS_BONK],
        )
        tasks.append(
            asyncio.create_task(
                geyser.listen_for_tokens(on_token, pool_callback=on_pool)
            )
        )
    else:
        return
    logger.info("Upstream launch listeners started in read-only mode (%s)", mode)
    await stop.wait()
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def _offer(queue: asyncio.Queue[dict[str, str]], item: dict[str, str]) -> None:
    try:
        queue.put_nowait(item)
    except asyncio.QueueFull:
        logger.warning(
            "Launch queue full; dropped %s %s", item.get("stage"), item.get("contract")
        )


def resolve_listener(choice: str) -> dict[str, str]:
    """Pick a listener from settings and which endpoints exist. Never prints them."""
    import os

    wss = bool(os.environ.get("SOLANA_NODE_WSS_ENDPOINT"))
    geyser = bool(
        os.environ.get("GEYSER_ENDPOINT") and os.environ.get("GEYSER_API_TOKEN")
    )
    if choice == "off":
        return {"mode": "off", "reason": "disabled"}
    if choice == "geyser":
        if not geyser:
            return {"mode": "off", "reason": "geyser_credentials_missing"}
        return {"mode": "geyser", "reason": "configured"}
    if choice == "logs_and_blocks":
        if not wss:
            return {"mode": "off", "reason": "wss_missing"}
        return {"mode": "logs_and_blocks", "reason": "configured"}
    if choice == "auto":
        if geyser:
            return {"mode": "geyser", "reason": "auto_geyser"}
        if wss:
            return {"mode": "logs_and_blocks", "reason": "auto_wss"}
        return {"mode": "off", "reason": "no_listener_credentials"}
    return {"mode": "off", "reason": "unsupported_listener"}
