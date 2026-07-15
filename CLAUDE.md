# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A standalone Go daemon (`mdtb`) that polls Meteora's public DLMM pool-discovery
API, screens pools through quality gates, dedups, and hands each poll cycle's
*batch* of newly-qualifying pools to one of two sinks:

- **Webhook mode** (default): one HMAC-signed webhook per batch to a Hermes
  agent (logic in `assets/hermes/`), which ranks the batch, picks one pool +
  strategy, and deploys via the skill in `assets/skill/`.
- **Direct-deploy mode** (`DEPLOY_CMD` set): the daemon runs
  `dlmm_pipeline.py --from-batch` itself — the pipeline re-ranks the batch
  deterministically (same heuristics the agent prompt encoded) and deploys the
  strongest survivor in seconds instead of an LLM agent turn.

This daemon owns **entry signals only** — exits are the `dlmm_monitor.py`
cron's job.

## Build / run

```bash
go build -o mdtb .              # build the daemon
go vet ./...                   # vet
set -a && . ./.env && set +a   # load env (fish: use bass or `env` prefix)
./mdtb                          # run (reads config from environment)
./install.sh ~/.hermes/profiles/dlmm   # wire assets into a Hermes profile + build
```

Go tests exist for `internal/deploy` and `internal/robinhood`
(`go test ./internal/...`); there is no Makefile. All config is environment-driven (`internal/config`,
defaults in `.env.example`); nothing is hardcoded except the screening
thresholds.

## Architecture

Pipeline is a single loop in `internal/scanner`: `poll ▸ screen ▸ dedup ▸ forward`,
one pass per enabled mode per `POLL_INTERVAL`.

- `internal/config` — env → `Config`. Only enable-toggles live here; screening
  thresholds live in `internal/meteora`.
- `internal/meteora`
  - `discover.go` — builds the discovery API query. `buildFilters` pushes mode
    thresholds into the API's `filter_by` param (API-side prefilter), but
    `Screen` re-checks **every** gate locally — the API filter is best-effort.
  - `screen.go` — the gate logic and `ModeParams` (`Casual`, `Multiday`). This
    is a **verbatim port** of the Python `dlmm_pipeline.py` config.
    When changing gates or thresholds, keep them in sync with that upstream, or
    note the divergence — the comments cite where each value came from.
  - `momentum.go` — best-effort DexScreener downtrend gate (fail-open).
  - `audit.go` — best-effort Jupiter token-audit gate (fail-open): hard-rejects
    >30% bot holders, enriches signals with bot % + global fees paid.
  - `pvp.go` — best-effort same-symbol rival detection (fail-open, advisory):
    flags candidates whose ticker is contested by an established token with
    its own live DLMM pool (`is_pvp` + rival stats); never rejects.
  - `types.go` — JSON structs mirroring the discovery API response exactly.
- `internal/robinhood` — second venue: Uniswap v3 WETH pools on Robinhood Chain
  (chain ID 4663). **Two modes, two discovery sources** — no single feed spans
  both theses, which is why `pollRobinhood` takes an `rhFetcher`:
  - `Fresh` (`discover.go`) — newly-created pools via GeckoTerminal
    `new_pools`. A launch feed: a pool scrolls off it within minutes.
  - `Mature` (`mature.go`, `ROBINHOOD_MATURE`) — established pools (24h+) still
    printing outsized fee/TVL, via Uniswap's keyless interface GraphQL gateway,
    which indexes **nothing younger than a day**. One gateway call + one
    GeckoTerminal `/pools/multi/` enrich call per cycle; the local prefilter is
    what keeps the enrich to a single request. Sets `FeePaceH24` — the fee pace
    comes from realized 24h volume, because extrapolating an h1 window would let
    one busy hour fake a 24× daily rate.

  Both modes share `Screen` and every safety gate (GMGN OpenAPI
  `chain=robinhood` security + holder quality, Blockscout holders). The deploy
  pick also passes a supertrend/RSI entry-timing gate (`indicators.go`, the Go
  port of the skill's `local_indicators.py` — keep them in sync; fail-open,
  `ROBINHOOD_INDICATOR_GATE`), and `uni_monitor.py` confirms non-emergency
  exits through the same indicators before closing. Phase 1 is
  **observe-only** (`ROBINHOOD_ENABLED`, batches journal to the log;
  `ROBINHOOD_WEBHOOK` forwards them) and NEVER routes to `DEPLOY_CMD` — the
  pipeline is Solana-only. One deliberate divergence from the fail-open rule:
  a POSITIVE honeypot/blacklist/sell-tax detection hard-rejects; unknown still
  passes. Plan + phase status: `docs/ROBINHOOD_CHAIN_PLAN.md`.
- `internal/store` — `Seen` dedup set: Redis (`SetNX`, one key + TTL per pool)
  or in-memory map. Empty `REDIS_ADDR` selects in-memory.
- `internal/webhook` — HMAC-SHA256 forwarder. Signature scheme
  (`hex(HMAC-SHA256(secret, body))` in `X-Webhook-Signature`) must match the
  Hermes/gobot side; the shared secret is `HERMES_WEBHOOK_SECRET`.
- `internal/deploy` — direct-deploy runner: execs `DEPLOY_CMD` with
  `--from-batch <payload> --mode <mode>` per batch, parses the pipeline's
  `🚀 DEPLOYED` / `🧪 DRY RUN DEPLOY` stdout markers, and pipes a condensed
  outcome to `REPORT_CMD` (e.g. `hermes send -t telegram`). Execution failure
  unmarks the batch for retry; a deterministic REJECT does not.
- `assets/` — copied into a Hermes profile by `install.sh`, which rewrites the
  literal `__PROFILE__` token to the target path. `assets/skill` = solana-dlmm
  skill (Python pipeline/monitor + JS executor); `assets/hermes` = the webhook
  subscription (agent decision prompt).

## Conventions that matter

- **Batch, not per-pool.** One cycle emits all fresh candidates as a single
  signal array so the agent compares the set. Don't revert to first-come
  per-pool sends.
- **Dedup before momentum fetch** — avoids hitting DexScreener for already-seen
  pools. On webhook failure, the whole batch is `Unmark`ed to retry next cycle.
- **Fail-open gates.** `verified` / Jupiter shield / momentum treat missing data
  as passing (`boolOr(..., true)`). Preserve this — the API omits these fields
  for some tokens and failing closed would over-reject.
- **Redis TTL is per-key on purpose.** `SetNX` per pool gives each an independent
  rolling `SEEN_TTL`. The old `SAdd`+`Expire` refreshed the whole set's TTL every
  write so pools were deduped forever — see the comment in `store.go`; don't
  reintroduce a single-key set.
- **No hidden clock reads.** `webhook.Send` takes `nowUnix` from the caller.
  Keep time injection at the edges.
- **Webhook payload is a contract** documented in `docs/SIGNAL_SCHEMA.md`.
  Update that doc when changing the emitted shape.

## Security

Wallet keys never live in this repo — the skill reads `SOLANA_PUBLIC_KEY` /
`SOLANA_PRIVATE_KEY` from the Hermes profile `.env` at runtime. Keep
`HERMES_WEBHOOK_SECRET` matched on both daemon and subscription. Trades real funds.
