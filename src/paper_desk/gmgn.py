"""Read-only GMGN OpenAPI client.

Mirrors the exist-auth calls in gmgn-skills 1.7.1 (`OpenApiClient.authExistRequest`).
Signed routes (swap, strategy, follow-wallet, cooking) are not implemented.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any
from urllib.parse import urlencode

import aiohttp

from paper_desk.errors import MarketError

HOST = "https://openapi.gmgn.ai"
_RETRYABLE = frozenset({"RATE_LIMIT_EXCEEDED", "RATE_LIMIT_BANNED"})


class RateBucket:
    """Plan leaky bucket. Free 5/5, Plus 20/20, Pro 50/50, per GMGN's skill docs."""

    def __init__(self, plan: str) -> None:
        rate = {"free": 5.0, "plus": 20.0, "pro": 50.0}[plan]
        self.rate = rate
        self.capacity = rate
        self.tokens = rate
        self.updated = time.monotonic()
        self.ban_until = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self, weight: float) -> None:
        """Wait until `weight` tokens are available and this client is not banned."""
        while True:
            async with self._lock:
                now = time.monotonic()
                if now < self.ban_until:
                    wait = self.ban_until - now
                else:
                    elapsed = now - self.updated
                    self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
                    self.updated = now
                    if self.tokens >= weight:
                        self.tokens -= weight
                        return
                    wait = (weight - self.tokens) / self.rate
            await asyncio.sleep(min(max(wait, 0.01), 5))

    def ban_for(self, seconds: float) -> None:
        """Pause every caller until `seconds` from now."""
        self.ban_until = max(self.ban_until, time.monotonic() + max(seconds, 0))


class GmgnClient:
    """HTTP client. The API key is a header and is never put in an error string."""

    def __init__(self, api_key: str, bucket: RateBucket, host: str = HOST) -> None:
        if not api_key:
            raise MarketError("GMGN_API_KEY is not set")
        self._api_key = api_key
        self._bucket = bucket
        self._host = host.rstrip("/")
        self._session: aiohttp.ClientSession | None = None
        self._gas_cache: dict[str, tuple[float, dict[str, Any]]] = {}

    async def __aenter__(self) -> GmgnClient:
        timeout = aiohttp.ClientTimeout(total=20)
        self._session = aiohttp.ClientSession(timeout=timeout)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def user_info(self) -> dict[str, Any]:
        """GET /v1/user/info. Used as a credential check."""
        data = await self._call("GET", "/v1/user/info", {}, None, 1)
        if not isinstance(data, dict):
            raise MarketError("user info payload was not an object")
        return data

    async def rank(
        self, chain: str, interval: str, order_by: str, limit: int
    ) -> list[object]:
        """GET /v1/market/rank. The documented list is `data.rank`."""
        data = await self._call(
            "GET",
            "/v1/market/rank",
            {
                "chain": chain,
                "interval": interval,
                "limit": limit,
                "order_by": order_by,
                "direction": "desc",
            },
            None,
            3,
        )
        if not isinstance(data, dict) or not isinstance(data.get("rank"), list):
            raise MarketError("market rank payload has no rank array")
        rows = data["rank"]
        if not isinstance(rows, list):
            raise MarketError("market rank payload has no rank array")
        return rows

    async def smartmoney(self, chain: str, limit: int = 100) -> list[object]:
        """GET /v1/user/smartmoney. `side` is filtered by the caller, as the CLI does."""
        data = await self._call(
            "GET",
            "/v1/user/smartmoney",
            {"chain": chain, "limit": limit},
            None,
            1,
        )
        if not isinstance(data, dict) or not isinstance(data.get("list"), list):
            raise MarketError("smartmoney payload has no list array")
        rows = data["list"]
        if not isinstance(rows, list):
            raise MarketError("smartmoney payload has no list array")
        return rows

    async def trenches(self, limit: int) -> dict[str, list[object]]:
        """POST /v1/trenches for pump.fun and letsbonk on Solana."""
        section: dict[str, Any] = {
            "filters": ["offchain", "onchain"],
            "launchpad_platform_v2": True,
            "limit": limit,
            "launchpad_platform": ["Pump.fun", "letsbonk"],
        }
        body = {
            "version": "v2",
            "new_creation": dict(section),
            "near_completion": dict(section),
            "completed": dict(section),
        }
        data = await self._call("POST", "/v1/trenches", {"chain": "sol"}, body, 2)
        if not isinstance(data, dict):
            raise MarketError("trenches payload was not an object")
        out: dict[str, list[object]] = {}
        for name in ("new_creation", "near_completion", "completed"):
            rows = data.get(name)
            if not isinstance(rows, list):
                raise MarketError(f"trenches section {name} was not an array")
            out[name] = rows
        return out

    async def token_info(self, chain: str, address: str) -> dict[str, Any]:
        """GET /v1/token/info."""
        return await self._object(
            "GET", "/v1/token/info", {"chain": chain, "address": address}, 1
        )

    async def token_security(self, chain: str, address: str) -> dict[str, Any]:
        """GET /v1/token/security."""
        return await self._object(
            "GET", "/v1/token/security", {"chain": chain, "address": address}, 1
        )

    async def token_pool(self, chain: str, address: str) -> dict[str, Any]:
        """GET /v1/token/pool_info."""
        return await self._object(
            "GET", "/v1/token/pool_info", {"chain": chain, "address": address}, 1
        )

    async def gas_price(self, chain: str) -> dict[str, Any]:
        """GET /v1/trade/gas_price, cached for 30s so marks do not restamp it."""
        cached = self._gas_cache.get(chain)
        if cached is not None and time.monotonic() - cached[0] < 30:
            return cached[1]
        data = await self._object("GET", "/v1/trade/gas_price", {"chain": chain}, 1)
        self._gas_cache[chain] = (time.monotonic(), data)
        return data

    async def _object(
        self,
        method: str,
        path: str,
        query: dict[str, Any],
        weight: int,
    ) -> dict[str, Any]:
        data = await self._call(method, path, query, None, weight)
        if not isinstance(data, dict):
            raise MarketError(f"{path} payload was not an object")
        return data

    async def _call(
        self,
        method: str,
        path: str,
        query: dict[str, Any],
        body: dict[str, Any] | None,
        weight: int,
    ) -> Any:
        if self._session is None:
            raise MarketError("GMGN client is not open")
        last: MarketError | None = None
        for attempt in (1, 2):
            await self._bucket.acquire(weight)
            stamp = int(time.time())
            params = {**query, "timestamp": stamp, "client_id": str(uuid.uuid4())}
            url = f"{self._host}{path}?{urlencode(params)}"
            headers = {
                "X-APIKEY": self._api_key,
                "Content-Type": "application/json",
                "User-Agent": "paper-desk/1.0",
            }
            payload = json.dumps(body) if body is not None else None
            try:
                async with self._session.request(
                    method, url, headers=headers, data=payload
                ) as resp:
                    text = await resp.text()
                    reset = _reset_seconds(resp.headers.get("x-ratelimit-reset"))
                    status = resp.status
            except (aiohttp.ClientError, TimeoutError) as exc:
                raise MarketError(
                    f"{path} request failed: {type(exc).__name__}"
                ) from exc
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as exc:
                raise MarketError(f"{path} returned non-JSON HTTP {status}") from exc
            if not isinstance(parsed, dict):
                raise MarketError(f"{path} returned a non-object HTTP {status}")
            code = parsed.get("code")
            if code in {0, "0"}:
                return parsed.get("data")
            error = str(parsed.get("error") or "")
            last = MarketError(f"{path} failed HTTP {status} code={code} error={error}")
            if error in _RETRYABLE and reset is not None:
                self._bucket.ban_for(reset)
            if error == "RATE_LIMIT_BANNED_MAX" and reset is not None:
                self._bucket.ban_for(reset)
                raise last
            if (
                attempt == 1
                and error in _RETRYABLE
                and reset is not None
                and reset <= 5
            ):
                await asyncio.sleep(reset)
                continue
            raise last
        raise last or MarketError(f"{path} failed")


def _reset_seconds(raw: str | None) -> float | None:
    if raw is None or not raw.strip():
        return None
    try:
        stamp = int(raw)
    except ValueError:
        return None
    if stamp <= 0:
        return None
    return max(stamp - time.time(), 0) + 1
