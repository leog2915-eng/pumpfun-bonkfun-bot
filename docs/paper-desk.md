# Paper desk

Paper simulation only. No signing, no broadcast, no funding, and no path into `src/trading`. `paper_desk live` refuses even when wallet keys are present. A finished paper session does not become a live session.

## Agents

| Agent | Entry vote | What it reads |
|---|---|---|
| scout | yes | `GET /v1/market/rank` on sol, base, and bsc. Top `rank_limit` (default 50) for `5m` ordered by volume and `1h` ordered by change. A spike needs `price_change_percent` >= 15 and volume >= $25,000. A gainer needs change >= 25 and volume >= $10,000. The change field is percent points. |
| whale | yes | `GET /v1/user/smartmoney`, 100 recent rows per chain. A vote needs at least 2 distinct makers buying the same token inside 30 minutes, each buy >= $100, with hash, maker, timestamp, and price present. |
| sniper | yes | Solana pump.fun and letsbonk.fun only. Upstream logs (pump.fun), blocks (letsbonk), or geyser (both) when those endpoints exist, plus `POST /v1/trenches` platforms `Pump.fun` and `letsbonk`, limit 80 per section. |
| shield | no | Fresh security and quote, honeypot fail-closed on base and bsc, liquidity at least $1,000, cash, the $50 cap, and 10 open positions. |
| exit | no | Marks open lots. Stop, 2x, 3x, and the trail do not wait for votes. |

Quorum is two distinct fresh votes among scout, whale, and sniper. Vote TTL is 90 seconds. The same agent voting twice is one vote. A second buy of a token already traded in the session is refused.

## Sniper filters, fail closed

A sniper vote requires all of:

- The curve is graduated. Listener creates, pre-execution migrations, and trenches `new_creation` / `near_completion` are blocked. GMGN's two docs disagree on `launchpad_status` numbers, so that field is not used as a curve oracle.
- `lock_percent` is present and >= 1. That field is documented on rank rows as a 0-1 liquidity-lock ratio (`locked` on EVM means >= 0.5, which is not this check). `locked_ratio` is supply locked and is not accepted. `burn_status == burn` is not treated as a lock.
- `dev_team_hold_rate` is present and strictly < 0.05.
- `bundler_rate` is present and exactly 0. `bundler_trader_amount_rate` is documented as volume and is not accepted as bundled supply.

Missing, stale (security 120s, quote 20s), unparsable, or unsupported data vetoes. Base and BSC honeypot must be an explicit no. Solana's empty honeypot is the documented "not applicable" value. A honeypot yes vetoes on every chain.

## Fills

The mid is worsened by the larger of configured slippage (default 1%) and linear impact `notional / liquidity`. They are not added. Impact above 10% refuses the fill. Pool `fee_ratio` uses the GMGN note that 0.1 means 0.1%. If the field is absent, 30 bps is used and labeled `configured_default_bps`. If the field is present but unusable, the fill is refused.

Gas prefers `GET /v1/trade/gas_price`: SOL `average` as lamports, EVM `average` as wei per gas times 300,000 configured units. An implausible or missing gas quote uses the labeled fallback ($0.02 sol, $0.02 base, $0.05 bsc) and says so on the fill.

Buy cash out is the $50 budget including fee and gas. Cost basis is that cash out. A partial sell allocates cost pro-rata; the closing slice takes whatever cost remains. Realized PnL is net proceeds minus allocated cost. Fees are inside those numbers and are also reported as a total. Unrealized is mark times quantity minus remaining cost, and is null when any open lot lacks a fresh mark. Cash cannot go negative. A sell that would make it negative does not fill.

The stop compares the fresh mid to the average execution price times 0.94. The fill price is the worsened mid. A gap through the stop fills at that observed price.

Default take-profits, both configurable in `config/paper_desk.yaml`: sell 50% of the original quantity at 2x and 25% at 3x. If one quote crosses both, both slices fill at that quote's worsened price, in that order. The trail arms after the 2x slice and sells the remainder after a 15% drop from the peak mid since it armed. The original -6% stop still applies to whatever is left.

## Coverage

Not every token and not every launch.

- Scout is a rank sample, max 100, default 50, two intervals, three chains.
- Whale is the latest smart-money page, not every wallet.
- Sniper trenches are three sections of at most 80. Listeners only see the programs they subscribe to, and only when WSS or Geyser credentials exist. With neither, sniper is the trenches sample alone.
- Base and BSC have scout and whale only.
- Shreds are refused because they are pre-execution.
- Robinhood is not a chain here.
- Rate limits follow the GMGN plan bucket. A 429 pauses the client until `X-RateLimit-Reset`. Outages back off from 1s to 60s. The launch queue holds 256 events and drops the rest with a log line. No outage is filled with a made-up price.

Routes the client calls: `GET /v1/user/info`, `GET /v1/market/rank`, `GET /v1/token/info`, `GET /v1/token/security`, `GET /v1/token/pool_info`, `GET /v1/user/smartmoney`, `POST /v1/trenches`, `GET /v1/trade/gas_price`.

Routes it does not call: swap, multi-swap, quote, strategy, cooking, follow-wallet.

Importing a listener also imports the upstream instruction builders, because that is how the factory is built. The desk never calls them and never builds a keypair.

## Session files

`data/paper_desk/<session>/` holds `desk.sqlite` (WAL), `manifest.json`, `status.json`, `health.json`, `desk.log`, and at the end `report.json` and `report.md`. Timestamps in the database are UTC. Log lines also show US Eastern. `data/paper_desk/CURRENT` points at the latest session.

## Recap definitions

`paper_desk recap` prints these with the numbers:

- Completed position: status closed and quantity zero. Open lots are excluded, including after a partial sell.
- Win: completed and realized PnL > 0. A zero is a scratch, not a win.
- Win rate: wins / completed positions, null when none have completed.
- Realized PnL: sell net proceeds minus allocated cost, summed across open partials and completed lots.
- Unrealized PnL: fresh mark times quantity minus remaining cost. Null for the session if any open lot has no fresh mark.
- Fees: sum of simulated fee and gas. Already inside realized PnL and remaining cost.
- Net PnL: realized plus unrealized when unrealized is known, otherwise null.

## 24h command

```bash
paper_desk preflight
paper_desk paper --duration 24h --cash 500
paper_desk status
paper_desk recap
```

The process runs until the deadline, then writes the report and marks the manifest completed. It does not flatten open lots at the deadline, so unrealized can remain. It does not start a live trader afterward.

## Observation mode

`paper_desk observe --duration 24h` is a separate database under `data/observe/`. Manifest mode is `OBSERVATION_ONLY`. It does not call GMGN, an RPC, a signer, or `execute_trade`.

Public reads, capped at 10 requests per minute each:

- GeckoTerminal `trending_pools` and `new_pools` for solana, base, and bsc. One list per minute, 20 rows. That is the sample, not every token and not every mint.
- RugCheck `GET /v1/tokens/{mint}/report` for Solana enrichment. `lpLockedPct` is a 0-100 percent. A value of 100 is ratio 1, but it is not a verified lock unless `lockers` names a non-burn locker and `lockerScanStatus` is not `none`, with a permanent flag or a future unlock time. Empty lockers leave expiry unknown.
- GoPlus token security for Base (`8453`) and BSC (`56`). `is_honeypot` of `0` is no and `1` is yes.

`creatorBalance`, `creator_percent`, and `owner_percent` are not dev-team holdings. `graphInsidersDetected` is not bundled supply. Those fields are stored as ignored evidence. Missing dev-team rate or bundled-supply ratio stays unknown and blocks. `pump-fun` pools are ungraduated bonding curves and are blocked. The checked AMM ids are `uniswap-v4-base`, `pancakeswap_v2`, `meteora`, `meteora-damm-v2`, `pumpswap`, and `raydium`. Any other dex id stays unknown.

Whale is unavailable and does not vote. Exit is idle because there are no positions. Quorum is still two distinct votes among scout and sniper. Shield does not vote. A pair that would pass is recorded as `filters_passed_not_traded`.

The recap uses `N/A` for PnL, win rate, best trade, and worst trade. It does not print those as zero. Heartbeat files are `health.json`, `status.json`, and `checkpoint.json`.
