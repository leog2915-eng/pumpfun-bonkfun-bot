"""Market sources. Paper mode refuses anything labeled synthetic."""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol

from paper_desk.config import Settings
from paper_desk.errors import MarketError
from paper_desk.execution import resolve_gas_usd
from paper_desk.gmgn import GmgnClient
from paper_desk.numbers import dec
from paper_desk.security import CURVE_UNKNOWN, Quote, SecurityView, build_security_view
from paper_desk.timeutil import utc_now


class MarketSource(Protocol):
    """Reads the desk is allowed to trade on. Implementations do not sign."""

    origin: str

    async def rank(self, chain: str, interval: str, order_by: str) -> list[object]:
        """Rank sample for one chain."""

    async def smartmoney(self, chain: str) -> list[object]:
        """Recent smart-money trades for one chain."""

    async def trenches(self) -> dict[str, list[object]]:
        """Solana launch sample, or an empty mapping when this source has none."""

    async def bundle(
        self,
        chain: str,
        contract: str,
        context: dict[str, object] | None,
    ) -> tuple[Quote, SecurityView, Decimal, str]:
        """Fresh quote, security view, and gas for an entry."""

    async def mark(self, chain: str, contract: str) -> tuple[Quote, Decimal, str]:
        """Fresh quote and gas for an exit. No security read."""


class GmgnSource:
    """Live read-only GMGN adapter. `origin` is `market`."""

    origin = "market"

    def __init__(self, client: GmgnClient, settings: Settings) -> None:
        self._client = client
        self._settings = settings

    async def rank(self, chain: str, interval: str, order_by: str) -> list[object]:
        """Documented rank route."""
        return await self._client.rank(
            chain, interval, order_by, self._settings.rank_limit
        )

    async def smartmoney(self, chain: str) -> list[object]:
        """Documented smartmoney route, one page."""
        return await self._client.smartmoney(chain, 100)

    async def trenches(self) -> dict[str, list[object]]:
        """Solana pump.fun and letsbonk sample."""
        return await self._client.trenches(self._settings.trenches_limit)

    async def bundle(
        self,
        chain: str,
        contract: str,
        context: dict[str, object] | None,
    ) -> tuple[Quote, SecurityView, Decimal, str]:
        """Token info, security, pool, and gas. A failed call raises."""
        info = await self._client.token_info(chain, contract)
        security = await self._client.token_security(chain, contract)
        pool = await self._client.token_pool(chain, contract)
        gas_payload = await self._client.gas_price(chain)
        quote = quote_from_documents(
            chain=chain,
            contract=contract,
            info=info,
            pool=pool,
            origin=self.origin,
        )
        documents: list[tuple[str, dict[str, object]]] = [
            ("token_security", security),
            ("token_info", info),
            ("token_pool", pool),
        ]
        if context:
            documents.append(("context", context))
        view = build_security_view(
            chain=chain,
            contract=contract,
            fetched_at=quote.fetched_at,
            origin=self.origin,
            curve=CURVE_UNKNOWN,
            curve_evidence="not a launch classification",
            documents=documents,
        )
        gas_usd, gas_source = _gas(chain, gas_payload, self._settings)
        return quote, view, gas_usd, gas_source

    async def mark(self, chain: str, contract: str) -> tuple[Quote, Decimal, str]:
        """Price and pool only."""
        info = await self._client.token_info(chain, contract)
        pool = await self._client.token_pool(chain, contract)
        gas_payload = await self._client.gas_price(chain)
        quote = quote_from_documents(
            chain=chain,
            contract=contract,
            info=info,
            pool=pool,
            origin=self.origin,
        )
        gas_usd, gas_source = _gas(chain, gas_payload, self._settings)
        return quote, gas_usd, gas_source


def quote_from_documents(
    *,
    chain: str,
    contract: str,
    info: dict[str, object],
    pool: dict[str, object],
    origin: str,
) -> Quote:
    """Build a quote from documented price and liquidity fields.

    Missing price becomes 0 so clearance fails closed instead of guessing.
    """
    price: Decimal | None = None
    source = "missing"
    raw_price = info.get("price")
    if isinstance(raw_price, dict):
        price = dec(raw_price.get("price"))
        if price is not None:
            source = "gmgn_token_info.price.price"
    liquidity: Decimal | None = None
    fee_raw: str | None = None
    nested = info.get("pool")
    if isinstance(nested, dict):
        liquidity = dec(nested.get("liquidity"))
        if nested.get("fee_ratio") is not None:
            fee_raw = str(nested.get("fee_ratio"))
    if liquidity is None:
        liquidity = dec(info.get("liquidity"))
    if price is None:
        price = dec(pool.get("price"))
        if price is not None:
            source = "gmgn_token_pool.price"
    if liquidity is None:
        liquidity = dec(pool.get("liquidity"))
    if price is None:
        price = Decimal(0)
    return Quote(
        chain=chain,
        contract=contract,
        price_usd=price,
        liquidity_usd=liquidity,
        fee_ratio_raw=fee_raw,
        fetched_at=utc_now(),
        source=source,
        origin=origin,
    )


def _gas(
    chain: str, payload: dict[str, object], settings: Settings
) -> tuple[Decimal, str]:
    if chain not in settings.gas_fallback_usd:
        raise MarketError(f"no gas fallback for {chain}")
    return resolve_gas_usd(
        chain,
        payload,
        fallback_usd=settings.gas_fallback_usd[chain],
        evm_gas_units=settings.evm_gas_units,
        max_plausible_usd=settings.max_plausible_gas_usd,
    )
