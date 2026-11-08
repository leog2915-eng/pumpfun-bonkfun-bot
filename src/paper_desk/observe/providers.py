"""Keyless public reads. No API key is added to a request."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Protocol

import aiohttp

from paper_desk.observe.limits import MinuteBudget

GECKO = "https://api.geckoterminal.com/api/v2"
RUGCHECK = "https://api.rugcheck.xyz/v1/tokens"
GOPLUS = "https://api.gopluslabs.io/api/v1"
_GOPLUS_CHAIN = {"bsc": "56", "base": "8453"}


class ReadError(Exception):
    """A public read failed, was skipped for budget, or had the wrong shape."""

    def __init__(
        self, provider: str, endpoint: str, detail: str, status: int | None = None
    ) -> None:
        self.provider = provider
        self.endpoint = endpoint
        self.detail = detail
        self.status = status
        self.recorded = False
        super().__init__(f"{provider} {endpoint} {detail}")


class Feeds(Protocol):
    """List and risk reads. Implementations do not trade."""

    async def trending(self, network: str) -> list[object]:
        """Trending pool rows for one GeckoTerminal network."""

    async def new_pools(self, network: str) -> list[object]:
        """Newest pool rows for one GeckoTerminal network."""

    async def rugcheck(self, mint: str) -> dict[str, Any]:
        """Public Solana report."""

    async def goplus(self, chain: str, address: str) -> dict[str, Any]:
        """Public EVM token-security row."""


class LiveFeeds:
    """GeckoTerminal, RugCheck, and GoPlus over HTTP."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        gecko_budget: MinuteBudget,
        rugcheck_budget: MinuteBudget,
        goplus_budget: MinuteBudget,
    ) -> None:
        self._session = session
        self._budgets = {
            "geckoterminal": gecko_budget,
            "rugcheck": rugcheck_budget,
            "goplus": goplus_budget,
        }
        self.calls: list[dict[str, Any]] = []

    async def trending(self, network: str) -> list[object]:
        """GET trending_pools page 1. The documented list is `data`."""
        url = f"{GECKO}/networks/{network}/trending_pools?page=1"
        payload = await self._get("geckoterminal", "trending_pools", url)
        return _data_list(payload, "geckoterminal", "trending_pools")

    async def new_pools(self, network: str) -> list[object]:
        """GET new_pools page 1."""
        url = f"{GECKO}/networks/{network}/new_pools?page=1"
        payload = await self._get("geckoterminal", "new_pools", url)
        return _data_list(payload, "geckoterminal", "new_pools")

    async def rugcheck(self, mint: str) -> dict[str, Any]:
        """GET /v1/tokens/{mint}/report. `refresh` is not sent."""
        url = f"{RUGCHECK}/{mint}/report"
        payload = await self._get("rugcheck", "token_report", url)
        if not isinstance(payload, dict):
            raise ReadError("rugcheck", "token_report", "report_not_object")
        return payload

    async def goplus(self, chain: str, address: str) -> dict[str, Any]:
        """GET token_security for Base 8453 or BSC 56."""
        chain_id = _GOPLUS_CHAIN.get(chain)
        if chain_id is None:
            raise ReadError("goplus", "token_security", "chain_not_supported")
        url = f"{GOPLUS}/token_security/{chain_id}?contract_addresses={address}"
        payload = await self._get("goplus", "token_security", url)
        if not isinstance(payload, dict) or payload.get("code") not in {1, "1"}:
            raise ReadError("goplus", "token_security", "code_not_ok")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise ReadError("goplus", "token_security", "result_not_object")
        for key, row in result.items():
            if str(key).lower() == address.lower() and isinstance(row, dict):
                return row
        raise ReadError("goplus", "token_security", "address_missing")

    async def _get(self, provider: str, endpoint: str, url: str) -> object:
        budget = self._budgets[provider]
        if not budget.allow(time.monotonic()):
            self._record(
                provider,
                endpoint,
                ok=False,
                status=None,
                error="rate_budget",
                rows=None,
                elapsed_ms=0,
            )
            error = ReadError(provider, endpoint, "rate_budget")
            error.recorded = True
            raise error
        last: ReadError | None = None
        for attempt in (1, 2):
            started = time.perf_counter()
            try:
                timeout = aiohttp.ClientTimeout(total=15)
                async with self._session.get(
                    url,
                    timeout=timeout,
                    headers={
                        "Accept": "application/json",
                        "User-Agent": "paper-desk-observe/1",
                    },
                ) as response:
                    status = response.status
                    payload = await response.json(content_type=None)
            except (aiohttp.ClientError, TimeoutError) as exc:
                last = ReadError(provider, endpoint, type(exc).__name__)
                if attempt == 1:
                    await asyncio.sleep(1)
                    continue
                elapsed = int((time.perf_counter() - started) * 1000)
                self._record(
                    provider,
                    endpoint,
                    ok=False,
                    status=None,
                    error=last.detail,
                    rows=None,
                    elapsed_ms=elapsed,
                )
                last.recorded = True
                raise last from exc
            elapsed = int((time.perf_counter() - started) * 1000)
            if status == 429 or status >= 500:
                last = ReadError(provider, endpoint, f"http_{status}", status)
                if attempt == 1:
                    await asyncio.sleep(1)
                    continue
                self._record(
                    provider,
                    endpoint,
                    ok=False,
                    status=status,
                    error=last.detail,
                    rows=None,
                    elapsed_ms=elapsed,
                )
                last.recorded = True
                raise last
            if status != 200:
                self._record(
                    provider,
                    endpoint,
                    ok=False,
                    status=status,
                    error=f"http_{status}",
                    rows=None,
                    elapsed_ms=elapsed,
                )
                error = ReadError(provider, endpoint, f"http_{status}", status)
                error.recorded = True
                raise error
            rows = (
                len(payload["data"])
                if isinstance(payload, dict) and isinstance(payload.get("data"), list)
                else None
            )
            self._record(
                provider,
                endpoint,
                ok=True,
                status=status,
                error=None,
                rows=rows,
                elapsed_ms=elapsed,
            )
            return payload
        raise last or ReadError(provider, endpoint, "failed")

    def _record(
        self,
        provider: str,
        endpoint: str,
        *,
        ok: bool,
        status: int | None,
        error: str | None,
        rows: int | None,
        elapsed_ms: int,
    ) -> None:
        self.calls.append(
            {
                "provider": provider,
                "endpoint": endpoint,
                "ok": ok,
                "http_status": status,
                "error": error,
                "rows": rows,
                "elapsed_ms": elapsed_ms,
            }
        )


def _data_list(payload: object, provider: str, endpoint: str) -> list[object]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ReadError(provider, endpoint, "data_not_a_list")
    rows = payload["data"]
    if not isinstance(rows, list):
        raise ReadError(provider, endpoint, "data_not_a_list")
    return rows
