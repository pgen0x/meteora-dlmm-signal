# Webhook Signal Schema

The daemon POSTs one envelope per poll cycle. Its `payload` is an **array** of
every pool that newly qualified that cycle for a single mode — all elements
share the same `mode`. The agent compares the set, picks the single strongest
pool, and deploys it (via `dlmm_pipeline.py --from-signal`) — or rejects.

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

To deploy, the agent passes the chosen element's **full JSON record** to
`dlmm_pipeline.py --from-signal '<record>'`, which skips re-screening (the
gates below already ran) and runs only the final live gates before deploy.

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
