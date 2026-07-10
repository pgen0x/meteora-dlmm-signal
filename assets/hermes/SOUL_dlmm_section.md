## 9. Meteora DLMM LP Ingestion & Management Parameters

Before any DLMM pool is ingested or monitored, these rules and parameters are applied.

Pipeline supports three modes with **isolated position budgets** — each mode's positions do NOT block the other modes' slots.

### Casual Mode Parameters (30m timeframe — volume spike plays, hold 2-6h)
*   Casual Min TVL: $5,000
*   Casual Min Fee/TVL: 0.3%
*   Casual Min Mcap: $250,000
*   Casual Min Holders: 500
*   Casual Max Positions: 3

### Multiday Mode Parameters (24h timeframe — quality holds, target 24h+; screens every 4h)
*   Multiday Min TVL: $50,000
*   Multiday Min Fee/TVL: 1.0%
*   Multiday Min Mcap: $1,000,000
*   Multiday Min Holders: 1,000
*   Multiday Max Positions: 3

### Turnover Mode Parameters (30m timeframe — fee-capture on small high-fee pools, hold hours)
*   Turnover Min TVL: $5,000 / Max TVL: $300,000
*   Turnover Min Fee/TVL (30m window): 0.15% (~7%/day pace)
*   Turnover Min Base Fee: 1.0% (degen fee tier; fee income is the thesis, not price)
*   Turnover Min Mcap: $1,000,000
*   Turnover Min Holders: 500
*   Turnover Max Positions: 2
*   Prefer tight ranges around active bin (balanced_tight two-sided) — profit is fee_pct × turnover, so stay in range; exit on turnover decay, not price targets

### Shared Ingestion Gates
*   Minimum Base Organic Score: 75
*   Slippage: 1000 (bps, e.g. 10% slippage tolerance)

### Entry Conviction & Learning
*   Lone-Candidate Conviction Floor: 50 (degen score 0–100; a signal cycle producing exactly ONE fresh pool only emits it above this — "only option" is not "good option")
*   Audit Gate: reject > 30% bot holders (daemon-side); reject global fees < 30 SOL when the value is present (agent-side — bundled/scam line; absent = unknown, never reject on absence)
*   Pool Memory: never re-enter a pool whose last closes (>= 2) net out negative PnL
*   Repeat-Deploy Cooldown: 3rd deploy into the same pool within 24h → 12h pool cooldown
*   Signal Weights: darwinian — entry signals of every close are correlated with realized PnL (60d window, recalc <= 1×/6h); the deploy pick prioritizes candidates strong on high-weight signals (`sol:dlmm:signal_weights`)

### Active Strategy Configuration
*   Strategy: stage_aware (options: spot, custom_ratio_spot, balanced_tight, single_sided_reseed, fee_compounding, partial_harvest, stage_aware)
*   Indicators Enabled: true (enable indicator timing checks before entry/exit)
*   Indicators Preset: supertrend_break (timing presets)

### Exit Parameters
*   Hard Stop-Loss: -15.0% (grace applies only to a young in-range position with fee/TVL ≥ 10%; an EMERGENCY floor 3pp below this always closes immediately — bypasses grace, AI holds, indicator timing, and report-only mode)
*   Trailing TP Trigger: 3.0% (activate trailing exits once profit exceeds this; tune against your own close history — set too high, trailing never activates before another rule cuts the position)
*   Trailing TP Drop: 1.5% (floor below peak before the first ratchet tier; above +5% peak the monitor's profit ratchet takes over: peak ≥5% locks +2%, ≥10% locks +6%, ≥20% locks 70% of peak)
*   Max Bins Pumped Above: 10 (exit if active bin exceeds upper bin by this count)
*   Max Out of Range Minutes: 30 (exit if out of range for this long)
*   Turnover Max OOR Minutes: 2 (turnover-mode fast fuse — an OOR turnover position is idle fee-capture capital, so it closes into a re-center after minutes instead of the long fuse above)
*   Turnover CB Loss SOL: -0.05 (turnover rebalance circuit breaker — once a pool's cumulative realized PnL across rebalance closes in the last 24h drops below this many SOL, re-centering stops and normal exit + cooldown applies; count backstop 20/24h)
*   Min Age for Yield Check: 60 minutes
*   Min 24h Fee/TVL for Yield Check: 1.0% (exit if age exceeds minimum and fee/TVL drops below this)
*   Min Exit Liquidity: $7,000 (exit if live pool liquidity drains below this after entry — can't exit cleanly; set below the $10k entry TVL gate so fresh positions never trip it)

### Close GUARD — hold the healthy winner (applies to EVERY actor)

This GUARD overrides every exit parameter above and binds all of me — the cron monitor, the
interactive/gateway agent, and any manual action:

*   **NEVER close a position when ALL of these hold:** `in_range == true` AND `fee_per_tvl_24h >= 10%`
    AND no hard exit rule has triggered (`triggered_rules` empty). A young, in-range position earning
    high fees is healthy — HOLD it regardless of a small unrealized drawdown. Closing a fresh,
    in-range, high-fee winner is the single worst error I can make (it is the Joby-class bug).
*   **Do not discretionarily close** an empty-`triggered_rules` position unless `5m price <= -3%`
    (real dump) OR `break_even_days >= 5`. A mild pullback (e.g. -2.9% 5m) is NOT a close trigger.
*   **Hard floor:** `pnl_pct < -15%` always closes; thin-liquidity (< Min Exit Liquidity) always
    closes (can't exit later is worse than forgone fees).

### Exit ownership — single chokepoint

*   **Only `dlmm_monitor.py` may close a DLMM position.** It applies this GUARD, then sets the
    `DLMM_CLOSE_AUTH` token the executor requires. A raw `dlmm_executor.js close` is REFUSED.
*   The interactive/gateway agent must **NOT** close DLMM positions on request or its own judgment —
    the monitor owns all exits. If asked to close one, defer to the monitor / explain the GUARD,
    and only `--force` on explicit, deliberate human override.
*   The spot fast-monitor (`monitor_positions.py`) operates a **separate** keyspace and must never
    touch a DLMM base-token bag (it excludes active DLMM base mints).
