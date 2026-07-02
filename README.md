# meteora-dlmm-signal

A standalone signal daemon for **Meteora DLMM** liquidity provision on Solana.

It continuously watches Meteora's pool-discovery API, screens pools with the
same gates a battle-tested DLMM pipeline uses, and forwards each cycle's *batch*
of newly-qualifying pools — as one signal — to a
[Hermes](https://github.com/NousResearch/hermes) agent webhook. Your agent then
compares the set, picks the single strongest pool and a strategy, and deploys it
(via `dlmm_pipeline.py --from-signal`, which skips re-screening) — or rejects.

It runs entirely on Meteora's **public pool-discovery API** — no third-party
accounts, API keys, or scraping required to source signals.

```
┌─────────────────────────┐   HMAC-signed   ┌─────────────────────┐
│ meteora-dlmm-signal     │ POST /webhooks/ │ Hermes agent        │
│ (this Go daemon)        ├────────────────▶│ ranks the batch,    │
│                         │   dlmm-signal   │ picks 1 + strategy  │
│ poll -> screen -> dedup │  (batch array)  │ -> dlmm_pipeline.py │
└─────────────────────────┘                 └─────────────────────┘
             │                                        │
   Meteora discovery API              Meteora on-chain (deploy/monitor)
```

## Why signal-driven?

Blind time-based screening (a cron that scans every 30m) deploys into whatever
happens to trend at that minute — weak selection. This daemon watches
*continuously* and, each cycle, emits every pool that crosses all quality gates
as one batch. Sending the whole set (instead of first-come per-pool) lets the
agent compare candidates and pick the strongest — no mediocre early pool grabs
a slot the best pool wanted — off fresh data instead of stale cron snapshots.

## What gets screened

Two isolated modes, each with its own budget in the agent:

| Mode | Timeframe | Min TVL | Min fee/TVL | Min mcap | Min holders | Min fees/day |
|------|-----------|---------|-------------|----------|-------------|--------------|
| `casual`   | 30m | $5k  | 0.3% | $250k | 500  | $20  |
| `multiday` | 24h | $50k | 1.0% | $1M   | 1000 | $150 |

Shared gates (both modes): SOL-paired · `0 < volatility ≤ 15` · organic ≥ 60 ·
fee/TVL change ≥ −40% · top-10 ≤ 60% · dev ≤ 20% · no freeze/mint authority ·
`is_verified` not false · no critical warnings · (optional) not dumping
(5m > −5%, 1h > −15%, 6h > −12%, 24h > −25%).

See [`docs/SIGNAL_SCHEMA.md`](docs/SIGNAL_SCHEMA.md) for the exact webhook payload.

## Quick start

```bash
git clone <this-repo> && cd meteora-dlmm-signal

# 1. Install skill + webhook subscription into a Hermes profile, build daemon
./install.sh ~/.hermes/profiles/dlmm

# 2. Configure the daemon
cp .env.example .env        # set HERMES_WEBHOOK_SECRET to match the subscription
set -a && . ./.env && set +a

# 3. Run
./mds
```

The daemon is stateless except for its dedup set (in-memory by default; point
`REDIS_ADDR` at a Redis to persist "seen" pools across restarts).

## Configuration

All via environment (see `.env.example`): `METEORA_DISCOVER_URL`,
`POLL_INTERVAL`, `HERMES_WEBHOOK_URL`, `HERMES_WEBHOOK_SECRET`, `REDIS_ADDR`,
`SEEN_TTL`, `ENABLE_CASUAL`, `ENABLE_MULTIDAY`, `ENABLE_MOMENTUM_GATE`.

## Repo layout

```
main.go                     daemon entrypoint
internal/config             env config
internal/meteora            discovery client, screening gates, momentum
internal/scanner            poll ▸ screen ▸ dedup ▸ forward loop
internal/webhook            HMAC-signed forwarder
internal/store              seen-pool dedup (Redis or in-memory)
assets/skill                solana-dlmm skill (pipeline/monitor/executor) + safety scripts
assets/hermes               dlmm-signal webhook subscription (agent decision prompt)
docs/SIGNAL_SCHEMA.md       webhook contract
install.sh                  wires assets into a Hermes profile + builds daemon
```

`install.sh` symlinks `assets/skill/scripts/` into `<profile>/skills/solana-dlmm/scripts`
instead of copying it — edits to the pipeline/monitor/executor here take effect in every
installed profile immediately, no reinstall needed. The scripts resolve their own profile
dir at runtime (relative to their own file location), so no `__PROFILE__` path rewriting
happens at install time anymore.

## Position management

The daemon only handles **entry signals**. Exits are owned by the
`dlmm_monitor.py` cron (installed into your profile) — it applies the Close
GUARD and is the single authorized closer. Run it every 5m via Hermes cron.

## Security notes

- Wallet keys never live in this repo. The skill reads `SOLANA_PUBLIC_KEY` /
  `SOLANA_PRIVATE_KEY` from your profile `.env` at runtime.
- Same for RPC endpoints: set `SOLANA_RPC_URLS` (comma-separated, tried in order
  with failover) in your profile `.env`. Never hardcode provider keys (Helius,
  QuickNode, etc.) into the scripts — this repo is public.
- The webhook is HMAC-SHA256 signed; keep `HERMES_WEBHOOK_SECRET` secret and
  matched on both sides.
- Trades real funds. Start with small budgets. No warranty.
