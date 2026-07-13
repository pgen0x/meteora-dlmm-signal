# Webhook Signal Schema

The daemon POSTs one envelope per poll cycle. Its `payload` is an **array** of
every pool that newly qualified that cycle for a single mode — all elements
share the same `mode`. The agent compares the set, picks the single strongest
pool, and deploys it (via `dlmm_pipeline.py --from-signal`) — or rejects.

In **direct-deploy mode** (`DEPLOY_CMD` set) the webhook is skipped entirely:
the daemon hands the same payload array to
`dlmm_pipeline.py --from-batch '<payload>' --mode <mode>`, which re-ranks the
batch deterministically and deploys the strongest survivor itself. The payload
shape is identical in both flows.

## Transport

- **Method:** `POST`
- **URL:** `HERMES_WEBHOOK_URL` (default `http://127.0.0.1:8646/webhooks/dlmm-signal`)
- **Header:** `X-Webhook-Signature: <hex(HMAC-SHA256(secret, body))>`
  - `secret` = `HERMES_WEBHOOK_SECRET`, and must equal the `secret` in your
    Hermes `webhook_subscriptions.json` entry.
- **Content-Type:** `application/json`

## Envelope

`payload` is an array. One signal carries the whole cycle's batch (1..N pools),
so the agent selects across the set instead of racing first-come per-pool sends.

```json
{
  "type": "alert",
  "timestamp": 1782873031,
  "source": "meteora_pool_discovery",
  "payload": [
    {
      "mode": "casual",
      "timeframe": "30m",
      "pool": "sz2UJhf8KWxa115KmwcDuJYnUZx1fxDBetcAxXSboKi",
      "name": "CATWIF-SOL",
      "base_mint": "5pYB12kEhfhSFXJjZ7JtyqDpt6uUqhsF6iu6Ee9spump",
      "base_symbol": "CATWIF",
      "sol_is_x": false,
      "tvl": 105596.0,
      "fee_tvl_ratio": 27.43,
      "fee_active_tvl_ratio": 41.2,
      "fee_tvl_ratio_change_pct": 12.0,
      "daily_fee_usd": 289.0,
      "volatility": 3.4,
      "bin_step": 100,
      "organic_score": 82.0,
      "mcap": 540000.0,
      "holders": 1240,
      "top_holders_pct": 38.0,
      "dev_balance_pct": 2.0,
      "score": 91.3
    },
    {
      "mode": "casual",
      "timeframe": "30m",
      "pool": "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin",
      "name": "ZERO-SOL",
      "base_mint": "EmcxFTNVDqyLHp11NvwvLZ4D7LKGbG9i7B8RF7dwpump",
      "base_symbol": "ZERO",
      "sol_is_x": false,
      "tvl": 87820.0,
      "fee_tvl_ratio": 3.55,
      "fee_active_tvl_ratio": 3.63,
      "fee_tvl_ratio_change_pct": 97.7,
      "daily_fee_usd": 62.0,
      "volatility": 2.82,
      "bin_step": 100,
      "organic_score": 76.0,
      "mcap": 2342037.0,
      "holders": 4879,
      "top_holders_pct": 41.0,
      "dev_balance_pct": 3.0,
      "score": 45.0
    }
  ]
}
```

A single-pool cycle still ships as a one-element array, never a bare object.

## Payload element fields

Every element of `payload` is one candidate pool with these fields:

| Field | Meaning |
|-------|---------|
| `mode` | `casual` (30m plays), `multiday` (24h+ holds) or `turnover` (30m fee-capture on small high-fee pools) — same for every element; drives which budget/params the agent uses |
| `timeframe` | discovery timeframe the pool trended on |
| `pool` | Meteora DLMM pool address |
| `name` | pair name (e.g. `CATWIF-SOL`) |
| `base_mint` | non-SOL token mint — use for audit / momentum re-checks |
| `base_symbol` | token symbol (display) |
| `sol_is_x` | true if SOL is token_x (deploy orientation) |
| `tvl` / `fee_tvl_ratio` / `fee_active_tvl_ratio` | liquidity + yield metrics |
| `fee_tvl_ratio_change_pct` | fee/TVL trend (already gated ≥ −40%) |
| `daily_fee_usd` | absolute fees/day (already past the mode floor) |
| `volatility` | 0–15 band (IL risk); >15 already rejected |
| `bin_step` | pool bin step |
| `organic_score` / `mcap` / `holders` | base-token quality |
| `top_holders_pct` / `dev_balance_pct` | supply concentration (already gated) |
| `fee_pct` | pool base fee % (turnover mode gates ≥ 1%; other modes report it ungated) |
| `volume_tvl_ratio` | window volume / TVL turnover (turnover mode gates ≥ 3) |
| `swap_count` / `unique_traders` | window activity — wash-trade guards (turnover mode gates ≥ 20 / ≥ 15) |
| `score` | conviction score 0–100 (Degen Score: geometric mean of trading / LP-activity / fee / liquidity efficiency sub-scores, normalized to a 30m window — a high score requires balance, no single metric can fake it) |
| `active_tvl` / `volume_active_tvl_ratio` / `unique_lps` / `positions_created` | the Degen Score inputs, exposed so the agent sees *why* a score is high or low |
| `bot_holders_pct` / `global_fees_sol` | Jupiter audit enrichment (audit gate). **May be absent** — absent means the audit fetch failed (fail-open); treat as unknown, never as zero |
| `prior_closes` / `prior_net_pnl_sol` | pool memory summary from the monitor's close journal (`sol:dlmm:history:pool:<pool>`, last 10 closes / 30d). **May be absent** — absent means no history (or non-Redis dedup backend), not a clean record. Negative net PnL = this pool cost us before |
| `is_pvp` + `pvp_rival_name` / `pvp_rival_mint` / `pvp_rival_pool` / `pvp_rival_tvl` / `pvp_rival_holders` / `pvp_rival_fees_sol` | same-symbol rival detection: an established token (≥500 holders, ≥30 SOL fees) sharing this ticker has its own live DLMM pool (≥$5k TVL) — a ticker war. Advisory flag, never a daemon reject. **Absent** = no rival found or check failed (fail-open) |
| `gmgn_smart_wallets` / `gmgn_kol_wallets` | GMGN holder quality (GMGN gate): count of smart-money wallets (proven profitable traders) and KOL/fund wallets currently holding. Higher = stronger conviction; 0 = nobody notable in yet. **May be absent** — fetch failed or gate disabled (fail-open); treat as unknown, never as zero |
| `gmgn_sniper_wallets` / `gmgn_bundler_wallets` | count of launch-sniper and bot-bundled-buy wallets holding — bot-farmed supply. High counts relative to `holders` = manufactured demand |
| `gmgn_rat_volume_pct` / `gmgn_bundler_volume_pct` | share of trade volume from insider ("rat") and bundler wallets, percent. High insider volume = exit liquidity risk. **Values above the daemon's caps never reach the payload** — candidates exceeding `GMGN_MAX_RAT_PCT` / `GMGN_MAX_BUNDLER_PCT` (default 40 each) are hard-rejected before emit; absent values still pass (fail-open) |
| `gmgn_top10_pct` | GMGN's top-10 holder supply share, percent (independent recheck of `top_holders_pct`) |
| `gmgn_dev_status` / `gmgn_dev_tokens_created` | dev wallet state (`creator_hold` = still holding, `creator_sell` = exited) and how many tokens this creator has launched before — a serial deployer (dozens+) is a rug-factory signal |

To deploy, the agent passes the chosen element's **full JSON record** to
`dlmm_pipeline.py --from-signal '<record>'`, which skips re-screening (the
gates below already ran) and runs only the final live gates before deploy.
In direct-deploy mode the daemon passes the **whole payload array** to
`--from-batch` instead and the pipeline does the picking.

## Screening already applied (agent can trust these)

Only pools passing **all** of these are emitted:

- SOL-paired; TVL ≥ mode floor; fee/TVL ≥ mode floor; daily fee ≥ mode floor
- `0 < volatility ≤ 15`; organic ≥ mode floor (60 casual/multiday, 50 turnover); mcap ≥ mode floor; holders ≥ mode floor
- turnover mode only: TVL ≤ $300k; base fee ≥ 1%; volume/TVL ≥ 3; swaps ≥ 20; unique traders ≥ 15 (30m window)
- fee/TVL change ≥ −40%; top-10 ≤ 60%; dev ≤ 20%
- no freeze/mint authority; `is_verified` not false; no critical/warning flags
- (if enabled) not dumping: 5m > −5%, 1h > −15%, 6h > −12%, 24h > −25%
- (if enabled) Jupiter audit: bot holders ≤ 30% (fail-open when the audit is unavailable)
- lone-candidate conviction gate: a cycle producing exactly one fresh pool only
  emits it when `score ≥ LONE_MIN_SCORE` (default 50) — a weak solo candidate is
  held back (and un-deduped) so it can compete inside a future, richer batch

The agent still does final live checks (audit, portfolio slots, balance,
cooldowns — including the pool-level repeat-deploy cooldown — and learned
signal weights) before deploying.

## Robinhood Chain venue (`robinhood_pool_discovery`)

Enabled by `ROBINHOOD_ENABLED` (Phase 1: observe-only — batches journal to the
daemon log; `ROBINHOOD_WEBHOOK=true` forwards them with `source:
"robinhood_pool_discovery"` and the same envelope/HMAC transport). Payload is
an array of screened Uniswap v3 pools on Robinhood Chain (chain ID 4663).
Never routed to `DEPLOY_CMD` — the deploy pipeline is Solana-only until the
EVM executor lands (docs/ROBINHOOD_CHAIN_PLAN.md Phase 2).

```json
{
  "chain": "robinhood",
  "mode": "rh-fresh",
  "pool": "0xc187feb911997c06bc94903def113b677e6577c9",
  "dex": "uniswap-v3",
  "name": "CALLIE / WETH 1%",
  "created_at": "2026-07-13T02:08:17Z",
  "age_minutes": 124.5,
  "base_address": "0x21028be78e8f521214d24328715c1a8aadbac5a8",
  "base_symbol": "CALLIE",
  "base_decimals": 18,
  "quote_address": "0x0bd7d308f8e1639fab988df18a8011f41eacad73",
  "quote_symbol": "WETH",
  "fee_pct": 1.0,
  "reserve_usd": 23794.0,
  "fdv_usd": 297000.0,
  "mcap_usd": 0,
  "volume_h1_usd": 7539.0,
  "volume_h24_usd": 7539.0,
  "fee_tvl_day_pct": 7.6,
  "tx_h1": 65,
  "buyers_h1": 20,
  "sellers_h1": 12,
  "change_m5_pct": 1.2,
  "change_h1_pct": 231.0,
  "score": 62.4,
  "holders": 160,
  "gmgn_sell_tax_pct": 0,
  "gmgn_buy_tax_pct": 0,
  "gmgn_open_source": false,
  "gmgn_launchpad": "noxa",
  "gmgn_smart_wallets": 0,
  "gmgn_bundler_wallets": 85,
  "gmgn_rat_volume_pct": 0,
  "gmgn_bundler_volume_pct": 27.98,
  "gmgn_top10_pct": 17.3,
  "gmgn_dev_status": "creator_close"
}
```

Field notes:

- `fee_tvl_day_pct` — projected daily fee/TVL %, computed as
  `volume_h1 x 24 x fee_pct / reserve` (GeckoTerminal exposes no fee field;
  v3 fees are deterministic).
- `holders` + all `gmgn_*` — enrichment, absent on fetch failure (fail-open);
  treat missing as unknown, never zero.
- Screening already applied: WETH-quoted Uniswap v3 only; age 10m–24h;
  reserve $8k–$500k; fee tier ≥ 0.25%; fee pace ≥ 5%/day; ≥30 txns and ≥12
  buyers in h1; a "many buys, zero sells" pool is rejected (honeypot shape);
  FDV $20k–$50M; momentum gates (same thresholds as the Solana venue);
  Blockscout holders ≥ `ROBINHOOD_MIN_HOLDERS`; GMGN rat/bundler caps; GMGN
  contract security **hard-rejects on positive** honeypot/blacklist/sell-tax
  detection (unknown/null passes — the venue's one fail-closed divergence).
