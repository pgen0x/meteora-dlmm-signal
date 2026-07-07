---
name: solana-dlmm
description: |
  Autonomous Meteora DLMM pool screening and position management.
  Filters candidates, deploys concentrated liquidity, and tracks ranges.
trigger:
  - "solana dlmm"
  - "meteora dlmm"
  - "solana lp"
  - "dlmm portfolio"
---

# Solana DLMM Liquidity Provision Skill

## Overview
Automated lifecycle of a Solana Meteora DLMM concentrated liquidity position:
Screen Pools (Fee/TVL, TVL, Volatility, Base Token Safety Gates) → Deploy Single-Sided SOL LP Position → Monitor Active Bins / PnL → Exit Out-of-Range or SL/TP positions → Account realized yields.

## Scripts Directory
`<profile>/skills/solana-dlmm/scripts/` (symlinked to this repo's `assets/skill/scripts/` by `install.sh` — edit here, it's live everywhere)

---

## Tools & Commands

### 1. `dlmm_pipeline.py` — Ingestion Pipeline
**Purpose**: Screens Meteora's pool discovery API and deploys into the best candidate.
**Command**: `python3 ~/.hermes/profiles/<profile>/skills/solana-dlmm/scripts/dlmm_pipeline.py --mode <casual|multiday>`

Two modes with **isolated position budgets** (2 slots each, max 4 total):

**`--mode casual`** (30m timeframe — volume spike plays, hold 2-6h):
*   TVL >= $5k, Fee/TVL >= 0.3%, Mcap >= $250k, Holders >= 500, Max 2 positions

**`--mode multiday`** (24h timeframe — quality holds, target 24h+):
*   TVL >= $50k, Fee/TVL >= 1.0%, Mcap >= $1M, Holders >= 1,000, Max 2 positions

**Shared filters (both modes)**:
*   Organic Score >= 75, Volatility 0–15
*   Momentum: reject if 5m <= -5% or 1h <= -15%
*   Downtrend gate: reject if 6h <= -12% or 24h <= -25%
*   Verified + Jupiter shield (fail-open if API omits field)
*   Dev balance <= 20%, top-10 holders <= 60%, no freeze/mint authority, no critical warnings

**Flags**: `--analyze-only` (screen only, non-blocking), `--pool <ADDR>`, `--strategy <NAME>`

**Entry memory gates (all modes, incl. `--from-signal`)**:
*   Symbol cooldown (`sol:dlmm:cooldown:<SYMBOL>`) and pool cooldown (`sol:dlmm:cooldown:pool:<POOL>`) — skip while set.
*   Pool memory: skip a pool whose journaled closes (`sol:dlmm:history:pool:<POOL>`) show >= 2 past closes netting negative PnL — this pool already cost us.
*   Repeat-deploy churn guard: the 3rd deploy into the same pool within 24h sets a 12h pool cooldown.
*   Every deploy snapshots the winning candidate's entry signals into the position record (`signal` field) — feeds the darwinian weights (see `dlmm_weights.py`).

### 2. `dlmm_monitor.py` — Position Monitor
**Purpose**: Monitors all open positions in Redis and checks SL/TP or Out-of-Range limits.
**Command**: `python3 ~/.hermes/profiles/<profile>/skills/solana-dlmm/scripts/dlmm_monitor.py`
**Exits managed**:
*   Stop-Loss: Price drops >= -25% from entry price.
*   Take-Profit: Price rises >= +50% from entry price.
*   Out of Range: Position sits outside active bins for >= 30 minutes.
*   Thin Liquidity: Live pool liquidity drains below $7k floor (SOUL.md `Min Exit Liquidity`) — exit before the position strands. Re-checked every cycle; fail-open on fetch error.
*   Trailing TP: activates at SOUL `Trailing TP Trigger`; exit floor is a profit ratchet (peak ≥5% locks +2%, ≥10% locks +6%, ≥20% locks 70% of peak; below that, flat `Trailing TP Drop` from peak).
*   Emergency SL floor: 3pp below `Hard Stop-Loss` — always closes immediately, bypassing the SL grace, AI holds, and indicator timing. SL grace itself only applies to a young (<15m) in-range position with fee/TVL ≥ 10%.
*   Note: `--report-only` is read-only — never claims/closes/redeploys (incl. fee_compounding & partial_harvest strategies) — with ONE exception: an emergency close (SL floor breach / thin liquidity) executes even in report-only, because the cron runs report-only and the agent hop adds minutes. Pass `--no-enforce` for pure reporting.
*   Every close is journaled to `<profile>/memories/dlmm_closes.jsonl` (uniform schema, API-verified PnL, carries the entry-signal snapshot) AND to per-pool memory `sol:dlmm:history:pool:<POOL>` (last 10 closes, 30d expiry — the pipeline's "past losses" skip gate reads this; the daemon ships a summary as `prior_closes`/`prior_net_pnl_sol` on future signals for that pool). Audit journal vs Meteora portfolio API ground truth with `python3 .../scripts/dlmm_reconcile.py [--days 30]`.
*   A "Low yield" close also sets a 4h pool cooldown (`sol:dlmm:cooldown:pool:<POOL>`) — fee flow that already decayed doesn't recover within the 1h symbol cooldown, so block the pool itself from immediate re-entry.
*   Each run ends by triggering `dlmm_weights.py --quiet` (self-guarded, never fails the monitor).

**Close GUARD (overrides all exits above):** NEVER close when `in_range` AND `fee_per_tvl_24h >= 10%` AND no hard rule triggered. Do not discretionarily close an empty-`triggered_rules` position unless `5m <= -3%` OR `break_even_days >= 5`. Hard floor `pnl < -20%` and thin-liquidity always close. (Full policy: SOUL.md §9 "Close GUARD".) The `--override-close` path enforces this in code and refuses a healthy close unless `--force`.

**AI HOLD blocks `--override-close`:** If `ai_hold_active: true` in the report, do NOT call `--override-close` — the code will exit(2) and refuse. Only exceptions: (1) pass `--force` for genuine manual override, or (2) `pnl_pct <= -25%` (hard SL emergency, hold auto-bypassed by code). If conditions worsened to emergency, use `--force` and explain in `--reason`.

**Exit chokepoint:** `dlmm_monitor.py` is the ONLY authorized closer — it sets `DLMM_CLOSE_AUTH` after the GUARD/rules pass. A raw `node dlmm_executor.js close <addr>` is REFUSED (exit 3) unless `--force`/`DRY_RUN`. The gateway agent and the spot fast-monitor must never close DLMM positions directly.

### 3. `dlmm_weights.py` — Darwinian Signal Weights
**Purpose**: Learns which entry signals predict winners. Correlates the entry-signal snapshots in `dlmm_closes.jsonl` (last 60d, needs >= 10 closes with both wins and losses) with realized PnL; boosts top-quartile signals ×1.05, decays bottom ×0.95, clamped [0.3, 2.5]. Persists to `<profile>/memories/signal_weights.json` and mirrors to Redis `sol:dlmm:signal_weights`, which the deploy agent reads when ranking candidates.
**Commands**:
*   `python3 ~/.hermes/profiles/<profile>/skills/solana-dlmm/scripts/dlmm_weights.py --show` — print current weights
*   `python3 .../dlmm_weights.py --force` — recalc now (normally self-guarded to once per 6h, auto-run by the monitor)

### 4. `dlmm_executor.js` — SDK Transaction Executor
**Purpose**: Interacts directly with Meteora DLMM program on-chain. Invoked by Python runners. Supports RPC failover rotation.
**Commands**:
*   `node ~/.hermes/profiles/<profile>/skills/solana-dlmm/scripts/dlmm_executor.js active-bin <pool_address>`
*   `node ~/.hermes/profiles/<profile>/skills/solana-dlmm/scripts/dlmm_executor.js deploy <pool_address> <amount_sol> <bins_below> [bins_above]`
*   `node ~/.hermes/profiles/<profile>/skills/solana-dlmm/scripts/dlmm_executor.js claim <position_address>`
*   `node ~/.hermes/profiles/<profile>/skills/solana-dlmm/scripts/dlmm_executor.js close <position_address>`
*   `node ~/.hermes/profiles/<profile>/skills/solana-dlmm/scripts/dlmm_executor.js positions`

---

## Redis State Keys

| Key | TTL | Purpose |
|---|---|---|
| `sol:dlmm:position:<ADDR>` | 7d | LP Position metadata: pool, pair, base_mint, base_symbol, entry_price, entry_bin, bins_below, bins_above, size_sol, deployed_at, tx_hash, strategy, **mode** (casual\|multiday) |
| `sol:dlmm:active_positions` | permanent set | All currently active DLMM position addresses |
| `sol:dlmm:position:<ADDR>:oor_since` | permanent | Timestamp when the position was first detected out of range |
| `sol:dlmm:pnl:daily:YYYY-MM-DD` | 7d | Realized yields tracker: total_sol, count_exits |
| `sol:dlmm:cooldown:<SYMBOL>` | 1-72h | Token re-entry cooldown set on close (dump closes 2h, others 1h; repeat losses escalate 24h/72h) |
| `sol:dlmm:cooldown:pool:<POOL>` | 4-12h | Pool-level cooldown: 12h repeat-deploy churn guard (pipeline), 4h low-yield close (monitor) |
| `sol:dlmm:deploys:<POOL>` | 24h | Rolling deploy counter per pool; 3rd deploy sets the pool cooldown |
| `sol:dlmm:history:pool:<POOL>` | 30d | Last 10 close outcomes per pool (`ts`, `pnl_pct`, `pnl_sol`, `mode`, `reason`) — pipeline's "past losses" skip gate |
| `sol:dlmm:loss_streak:<SYMBOL>` | 7d | Consecutive-loss counter per token, escalates the symbol cooldown |
| `sol:dlmm:signal_weights` | permanent | Learned signal weights (JSON), written by `dlmm_weights.py`, read by the deploy agent |
