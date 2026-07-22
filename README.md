# Meteora DLMM Trading Bot — Solana Liquidity Pool Signal Daemon for AI Agents

[![Version](https://img.shields.io/static/v1?label=version&message=1.14.1&color=informational)](CHANGELOG.md) <!-- x-release-please-version -->
[![Go Version](https://img.shields.io/badge/go-1.22%2B-00ADD8?logo=go&logoColor=white)](go.mod)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Status](https://img.shields.io/badge/status-beta-yellow)](#project-status)
[![Chain](https://img.shields.io/badge/chain-Solana-9945FF?logo=solana&logoColor=white)](#)

**meteora-dlmm-trading-bot** is a Go daemon that watches **Meteora DLMM**
(Dynamic Liquidity Market Maker) pools on **Solana**, screens them through
quality gates, and hands an **AI trading agent** (built on
[Hermes](https://github.com/NousResearch/hermes)) a batch of vetted candidates
to pick from and deploy — instead of you babysitting a screener or grabbing the
first mediocre pool a dumb cron finds.

> ⚠️ **This trades real funds.** DYOR. NFA. Use at your own risk — see
> [Disclaimer](#disclaimer).

---

## Table of Contents

- [Why this exists](#why-this-exists)
- [Features](#features)
- [Architecture](#architecture)
- [Quick Start](#quick-start)
- [What gets screened](#what-gets-screened)
- [Configuration](#configuration)
- [Repo layout](#repo-layout)
- [Example output](#example-output)
- [Project Status](#project-status)
- [Contributing](#contributing)
- [Security](#security)
- [Disclaimer](#disclaimer)

## Why this exists

Most pool screeners run on a fixed schedule and deploy into whatever happens
to be trending at that moment, grabbing the first pool that clears their
filters. Both habits cost money: stale snapshots miss short-lived fee
opportunities, and first-match selection lets a mediocre pool take the slot a
stronger one deserved.

This daemon instead watches Meteora's pool-discovery API *continuously* and,
each cycle, emits every pool that crosses all quality gates as one batch. Your
AI agent sees the full set side by side and deploys only the strongest
candidate — always off fresh data.

It runs entirely on Meteora's **public pool-discovery API** — no third-party
accounts, API keys, or scraping required to source signals.

## Features

- **Continuous discovery, not polling snapshots** — every `POLL_INTERVAL`
  cycle, not just on a fixed cron tick.
- **Batch signalling** — one HMAC-signed webhook per cycle carries *every*
  qualifying pool, so your agent ranks the set instead of racing to grab the
  first one.
- **Three isolated screening modes** — `casual` (30m, volume-spike plays),
  `multiday` (24h, quality holds) and `turnover` (30m, fee-capture plays on
  small high-base-fee pools) with independent thresholds and position budgets.
- **Layered risk gates** — TVL, fee/TVL, market cap, holder count, organic
  score, top-10/dev supply concentration, mint/freeze authority, Jupiter
  shield status, a best-effort DexScreener downtrend filter, and a Jupiter
  token-audit gate (bot-holder %, global fees paid).
- **Conviction scoring** — every signal carries a 0–100 Degen Score (balanced
  trading/LP/fee/liquidity efficiency); single-candidate cycles must clear a
  conviction floor, so "only option" never auto-reads as "good option".
- **PVP rival detection** — candidates whose ticker is contested by an
  established same-symbol token with its own live DLMM pool are flagged with
  the rival's stats, so the agent avoids LPing the losing side of a ticker war.
- **Learning loop** — deploys snapshot their entry signals, closes journal the
  outcome per pool (skip pools that already lost money, cool down churned
  pools), and a darwinian recalc boosts/decays signal weights the deploy agent
  reads on every pick.
- **Fail-open by design** — gates with unreliable upstream data (verified
  status, momentum) default to *pass* instead of over-rejecting on missing
  fields.
- **Pluggable dedup store** — in-memory for a single instance, or Redis to
  share "seen" pools across restarts/instances with a per-pool rolling TTL.
- **Exit management included** — a companion `dlmm_monitor.py` cron owns all
  closes, applying stop-loss, trailing take-profit, out-of-range, and a
  "don't close a healthy winner" GUARD.
- **One install script** — wires the skill, webhook subscription, and
  SOUL.md/cron templates into a [Hermes](https://github.com/NousResearch/hermes)
  profile and builds the daemon.

## Architecture

```
┌─────────────────────────┐   HMAC-signed   ┌─────────────────────┐
│ mdtb (this Go daemon)   │ POST /webhooks/ │ Hermes agent        │
│                         ├────────────────▶│ ranks the batch,    │
│                         │   dlmm-signal   │ picks 1 + strategy  │
│ poll -> screen -> dedup │  (batch array)  │ -> dlmm_pipeline.py │
└─────────────────────────┘                 └─────────────────────┘
             │                                        │
   Meteora discovery API              Meteora on-chain (deploy/monitor)
```

The daemon (`internal/scanner`) does one thing on a loop: poll the discovery
API, screen every candidate locally (the API's own filter is best-effort),
dedup against pools already signalled, and forward the batch. Your Hermes
agent owns the judgment call — which pool, which strategy, whether to reject
the whole batch.

## Quick Start

### Prerequisites

- **Go 1.22+** — builds the daemon.
- **Node.js** (18+) and **Python 3** — run the `solana-dlmm` skill (pipeline,
  monitor, on-chain executor) inside your Hermes profile.
- A [Hermes](https://github.com/NousResearch/hermes) agent profile to install
  into.
- **Redis** (optional) — only needed if you want the dedup set to survive
  restarts or run multiple instances; in-memory works fine for a single box.
- A Solana RPC endpoint (Helius, QuickNode, or the public
  `api.mainnet-beta.solana.com` as a fallback) and a funded wallet.

### Create a Hermes profile

If you don't have a Hermes profile yet, create a dedicated one for the trading
agent (full guide: [Hermes docs — Profiles](https://hermes-agent.nousresearch.com/docs/user-guide/profiles)):

```bash
# Create a profile named "dlmm" — this also registers a `dlmm` command alias
hermes profile create dlmm

# Configure API keys and model settings interactively
dlmm setup

# Optional config tweaks
dlmm config set model.default anthropic/claude-sonnet-4
```

Key files under `~/.hermes/profiles/dlmm/`:

- `.env` — API keys and, for this project, your wallet (`SOLANA_PUBLIC_KEY` /
  `SOLANA_PRIVATE_KEY`, see [Configuration](#quick-start) below).
- `config.yaml` — model, platforms, and the webhook port this daemon posts to.
- `SOUL.md` — the agent's personality/policy document; `install.sh` merges the
  DLMM trading rules in as section 9.

Start the messaging gateway (Telegram delivery, webhook listener):

```bash
dlmm gateway start        # foreground
dlmm gateway install      # or persistent systemd/launchd service
```

### Installation

```bash
git clone https://github.com/pgen0x/meteora-dlmm-trading-bot.git
cd meteora-dlmm-trading-bot

# Installs the skill (symlinked, not copied — edits here go live instantly),
# the webhook subscription, SOUL.md section + cron job templates, the 20s
# monitor-loop systemd service, and builds mdtb.
./install.sh ~/.hermes/profiles/<your-profile>
```

The install enables `sol-dlmm-monitor.service` (user-level systemd), which runs
`dlmm_monitor.py` every 20 seconds. **This loop is the trader-side safety net** —
auto-close, auto-swap-to-SOL, out-of-range re-centering and cooldowns all fire
from it; the Hermes cron job is only the reporting/judgment layer. Make it
survive logout once per machine:

```bash
loginctl enable-linger $USER
systemctl --user status sol-dlmm-monitor   # verify it's running
```

Without user systemd (e.g. macOS), run the loop some other persistent way:
`nohup bash <profile>/skills/solana-dlmm/scripts/dlmm_monitor_loop.sh &`

### Configuration

`install.sh` prints the exact next steps for your profile path, but in short,
create `<profile>/.env`:

```bash
SOLANA_PUBLIC_KEY=...
SOLANA_PRIVATE_KEY=...            # base58, used by dlmm_executor.js
SOLANA_RPC_URLS=https://mainnet.helius-rpc.com/?api-key=YOUR_KEY,https://api.mainnet-beta.solana.com
DLMM_ALERT_TARGET=telegram        # instant trade-event alerts (close/rebalance/compound)
                                  # sent script-side via `hermes send` — no LLM tokens.
                                  # "platform" or "platform:chat_id"; empty disables.
DLMM_TZ=Asia/Jakarta              # timezone for every report-card timestamp (IANA name,
                                  # e.g. America/New_York; empty/unset = system zone)
DLMM_STATS_HOUR=09                # daily scoreboard send hour in DLMM_TZ (00-23, default 09)
```

And the daemon's own `.env` (this repo's root):

```bash
cp .env.example .env        # set HERMES_WEBHOOK_SECRET to match the subscription
```

### Launch

```bash
set -a && . ./.env && set +a
./mdtb
```

The daemon is stateless except for its dedup set (in-memory by default; point
`REDIS_ADDR` at a Redis instance to persist "seen" pools across restarts).

## What gets screened

Three isolated modes, each with its own budget in the agent:

| Mode | Timeframe | Min TVL | Min fee/TVL (window) | Min mcap | Min holders | Min fees/day |
|------|-----------|---------|----------------------|----------|-------------|--------------|
| `casual`   | 30m | $5k  | 0.1%  | $250k | 500  | $20  |
| `multiday` | 24h | $50k | 1.0%  | $1M   | 1000 | $150 |
| `turnover` | 30m | $5k  | 0.15% | $1M   | 500  | $25  |

Shared gates (all modes): SOL-paired · `0 < volatility ≤ 15` · organic score
floor · fee/TVL change ≥ −40% · top-10 ≤ 60% · dev ≤ 20% · no freeze/mint
authority · `is_verified` not false · no critical warnings · (optional) not
dumping (5m > −5%, 1h > −15%, 6h > −12%, 24h > −25%).

### Turnover mode

While `casual`/`multiday` chase trending pools, `turnover` targets the niche
they never see: **small pools (TVL $5k–$300k) with degen base fees (≥1%)
turning their TVL over fast**. The thesis is fee capture, not price — fee
income is `fee_pct × turnover` and isn't capped by the monitor's trailing
take-profit, so a $50k pool doing 5× volume/TVL at a 2% fee out-earns a
"better" trending pool.

Extra gates on top of the shared set: TVL ≤ $300k · pool base fee ≥ 1% ·
volume/TVL ≥ 3 per 30m window · ≥ 20 swaps and ≥ 15 unique traders in-window
(wash-trade guard — this is what lets the organic floor relax to 50) · fee/TVL
≥ 0.15% per 30m (~7.2%/day pace). Discovery queries `category=all` sorted by
`fee:desc` instead of trending.

**Enable it** in the daemon's `.env` (off by default):

```bash
ENABLE_TURNOVER=true
```

then restart `./mdtb`. Signals arrive with `"mode": "turnover"`; the agent
prompt and `dlmm_pipeline.py --mode turnover` already handle the mode end to
end (2 position slots, tight-range `custom_ratio_spot` preferred).

See [`docs/SIGNAL_SCHEMA.md`](docs/SIGNAL_SCHEMA.md) for the exact webhook payload.

## Configuration

All daemon config is via environment (see `.env.example`):

| Variable | Purpose |
|---|---|
| `METEORA_DISCOVER_URL` | Base pool-discovery endpoint |
| `POLL_INTERVAL` | How often to poll each enabled timeframe |
| `HERMES_WEBHOOK_URL` / `HERMES_WEBHOOK_SECRET` | Where signals go, HMAC secret |
| `REDIS_ADDR` / `REDIS_SEEN_KEY` / `SEEN_TTL` | Dedup store (empty `REDIS_ADDR` = in-memory) |
| `ENABLE_CASUAL` / `ENABLE_MULTIDAY` / `ENABLE_TURNOVER` | Toggle each screening mode (`turnover` off by default) |
| `ENABLE_MOMENTUM_GATE` | DexScreener downtrend filter (fails open) |
| `ENABLE_AUDIT_GATE` | Jupiter token-audit gate: rejects >30% bot holders, ships bot % + global fees in the signal (fails open) |
| `ENABLE_PVP_CHECK` | same-symbol rival detection: flags candidates contested by an established token with its own live DLMM pool — advisory `is_pvp` + rival stats, never rejects (fails open) |
| `LONE_MIN_SCORE` | Conviction floor for single-candidate batches (degen score 0–100, default 50, 0 disables) |

## Repo layout

```
main.go                     daemon entrypoint
internal/config             env config
internal/meteora            discovery client, screening gates, momentum
internal/scanner            poll ▸ screen ▸ dedup ▸ forward loop
internal/webhook            HMAC-signed forwarder
internal/store              seen-pool dedup (Redis or in-memory)
assets/skill                solana-dlmm skill (pipeline/monitor/executor) + safety scripts
assets/hermes               dlmm-signal webhook subscription + SOUL.md/cron templates
docs/SIGNAL_SCHEMA.md       webhook contract
install.sh                  wires assets into a Hermes profile + builds daemon
```

`install.sh` symlinks `assets/skill/scripts/` and the DLMM-relevant
`solana-web3` scripts into your profile instead of copying them — edits in
this repo take effect in every installed profile immediately, no reinstall
needed.

### Performance scoreboard

```bash
python3 <profile>/skills/solana-dlmm/scripts/dlmm_stats.py [--hours 24] [--send]
```

Deterministic metlex-style card (closes, win rate, avg hold, realized PnL,
fees-vs-IL split, volume churned, per-mode breakdown, rebalance-chain PnL)
built from the Meteora portfolio API + close journal + Redis — no LLM. The
monitor loop auto-sends it daily at 09:00 WIB via `hermes send` when
`DLMM_ALERT_TARGET` is set.

## Example output

The agent reports each deploy decision as a card like this (real values, not
placeholders — the prompt never lets it fabricate a size/entry/TX):

```
🤖 AI Pick — multiday · 14:32 WIB

Candidates screened: 5
Chose: world-SOL over 4 others — highest organic score, healthiest fee/TVL
Strategy: custom_ratio_spot — meme-pool volatility, symmetric range fits

`CQEYFv3KGnJ6xxRyrUNWbXjPHGnbyCbjuZDTGocV92ug`

| Metric | Value |
|---|---|
| Token | world |
| Decision | ✅ DEPLOYED (multiday) pos DoSj3Ga... |
| Size | 1.03 SOL |
| Range | 62↓ 62↑ bins |
| Fee/TVL | 10.8%/d |
| Score | 290.7 |
```

## Project Status

**Beta.** The entry-signal daemon (this repo) and the screening gates are
stable and running against real capital. There is no CI and no Go test suite
yet (`go vet ./...` is the current bar) — if that's a blocker for your use
case, treat this as a reference implementation to adapt rather than a
drop-in production dependency.

## Contributing

Issues and PRs welcome — see [`CONTRIBUTING.md`](CONTRIBUTING.md) for dev
setup, commit conventions, and the versioning policy. Keep changes scoped:
this is a small, single-purpose daemon by design. If you're proposing a new
screening gate or threshold change, explain the reasoning (what failure mode
it prevents) in the PR description.

See [`CHANGELOG.md`](CHANGELOG.md) for release history. This project follows
[Semantic Versioning](https://semver.org/); the current version is reported
by `./mdtb -version`.

## Security

- Wallet keys never live in this repo. The skill reads `SOLANA_PUBLIC_KEY` /
  `SOLANA_PRIVATE_KEY` from your profile `.env` at runtime.
- Same for RPC endpoints: set `SOLANA_RPC_URLS` (comma-separated, tried in order
  with failover) in your profile `.env`. Never hardcode provider keys (Helius,
  QuickNode, etc.) into the scripts — this repo is public.
- The webhook is HMAC-SHA256 signed; keep `HERMES_WEBHOOK_SECRET` secret and
  matched on both sides.
- Found a vulnerability? Please open a private security advisory on GitHub
  rather than a public issue.

## Disclaimer

**DYOR. NFA (Not Financial Advice).** This software trades real funds
autonomously on Solana. Meme-pool liquidity provision carries real, frequent
risk of loss (impermanent loss, rug pulls, thin-liquidity exits). Nothing
here is a guarantee of profit. Start with a small, disposable budget, read
the screening gates and exit rules before trusting it with real capital, and
never deploy more than you can afford to lose. No warranty, express or
implied — see [LICENSE](LICENSE).
