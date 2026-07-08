# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [1.7.0](https://github.com/pgen0x/meteora-dlmm-trading-bot/compare/v1.6.0...v1.7.0) (2026-07-08)


### Features

* **skill:** instant script-side event alerts + 30m monitor cron ([4aa6315](https://github.com/pgen0x/meteora-dlmm-trading-bot/commit/4aa6315180c959f0509ec81b503c8017f7adcc6e))
* **skill:** instant script-side event alerts, stretch monitor cron to 30m ([b708caf](https://github.com/pgen0x/meteora-dlmm-trading-bot/commit/b708caf56f934d88965ed857d9407feaf0466728))

## [1.6.0](https://github.com/pgen0x/meteora-dlmm-trading-bot/compare/v1.5.0...v1.6.0) (2026-07-08)


### Features

* **install:** ship the 20s monitor-loop systemd service ([06d7d94](https://github.com/pgen0x/meteora-dlmm-trading-bot/commit/06d7d941aabe7be0970c6c77c45da38b01db5fb8))
* **skill:** turnover fast-cycle — 2m OOR fuse, PnL circuit breaker, fee compounding ([ba294c9](https://github.com/pgen0x/meteora-dlmm-trading-bot/commit/ba294c97477f00ea2dfc5b2813c0163eda9c5cc7))
* **skill:** turnover fast-cycle — 2m OOR fuse, PnL circuit breaker, fee compounding ([330a854](https://github.com/pgen0x/meteora-dlmm-trading-bot/commit/330a8544aad5238dc9d84b0e5cfb8d2a4c2692a7))

## [1.5.0](https://github.com/pgen0x/meteora-dlmm-trading-bot/compare/v1.4.0...v1.5.0) (2026-07-08)


### Features

* **skill:** extend OOR rebalance to casual and multiday modes ([362cfba](https://github.com/pgen0x/meteora-dlmm-trading-bot/commit/362cfbafd4b084045a6770c8646a2636a2cd5be6))
* **skill:** extend OOR rebalance to casual and multiday modes ([2eb4bae](https://github.com/pgen0x/meteora-dlmm-trading-bot/commit/2eb4bae92b7152a6f9f824de3ffe3ab21b9046a7))

## [1.4.0](https://github.com/pgen0x/meteora-dlmm-trading-bot/compare/v1.3.0...v1.4.0) (2026-07-08)


### Features

* **skill:** bid_ask bin shapes + Meridian-style turnover OOR rebalance ([6c2a2e3](https://github.com/pgen0x/meteora-dlmm-trading-bot/commit/6c2a2e35b65d8d2faabd88a63946401e6fb0ea9b))
* **skill:** bid_ask bin shapes + turnover OOR rebalance ([7833c3d](https://github.com/pgen0x/meteora-dlmm-trading-bot/commit/7833c3d681a83c1727faa64d884f4d30d0a57a65))

## [1.3.0](https://github.com/pgen0x/meteora-dlmm-trading-bot/compare/v1.2.0...v1.3.0) (2026-07-07)


### Features

* **scanner:** cooldown-aware screening + shorter casual harvest cooldown ([0e52fc7](https://github.com/pgen0x/meteora-dlmm-trading-bot/commit/0e52fc7ea5281c11013cffb466dc65a2b2ad2e78))
* **scanner:** cooldown-aware screening, PVP rival detection, casual harvest cooldown ([59b79c3](https://github.com/pgen0x/meteora-dlmm-trading-bot/commit/59b79c38c8a584ed0f3bc165060bc746d24e1911))

## [1.2.0](https://github.com/pgen0x/meteora-dlmm-trading-bot/compare/v1.1.0...v1.2.0) (2026-07-07)


### Features

* **hermes:** weights-aware pick, audit hard-reject, lone-candidate rule ([f5c7daf](https://github.com/pgen0x/meteora-dlmm-trading-bot/commit/f5c7daf0ddf75b1b41d0cfb96e27068b1ef352b2))
* **scanner,skill:** degen fallback, pool-history payload, low-yield pool cooldown ([63a7363](https://github.com/pgen0x/meteora-dlmm-trading-bot/commit/63a736385b754a66096c6fad69c8b2a0c072ad3e))
* **scanner:** audit gate, lone-candidate conviction gate, degen payload fields ([129dbb5](https://github.com/pgen0x/meteora-dlmm-trading-bot/commit/129dbb5705ea26f89327b1cd3b5d39b3645c6cec))
* **skill:** pool memory, repeat-deploy cooldown, darwinian signal weights ([e14bd9c](https://github.com/pgen0x/meteora-dlmm-trading-bot/commit/e14bd9cbd558841f97f35433561d0bf58f0bdee3))


### Bug Fixes

* **hermes:** forbid execute_code wrappers, treat empty stdout as failure ([7f829af](https://github.com/pgen0x/meteora-dlmm-trading-bot/commit/7f829af6c33f47f04d7c176639c8de2702a06137))
* **install:** preserve profile secret/delivery/model on subscription re-merge ([b6e5f71](https://github.com/pgen0x/meteora-dlmm-trading-bot/commit/b6e5f71eebfeb79dda93045c445b5ccf7fad4232))

## [1.1.0](https://github.com/pgen0x/meteora-dlmm-trading-bot/compare/v1.0.0...v1.1.0) (2026-07-06)


### Features

* **monitor:** compact position status card ([022d84b](https://github.com/pgen0x/meteora-dlmm-trading-bot/commit/022d84b9d30298ae347db72c514e7748931ba85e))
* **monitor:** script-side report delivery via hermes send ([9e2531b](https://github.com/pgen0x/meteora-dlmm-trading-bot/commit/9e2531bd8b775eb5c243eeb5053572e6f605706a))
* **scanner:** add turnover fee-capture mode ([53f7af0](https://github.com/pgen0x/meteora-dlmm-trading-bot/commit/53f7af0ca91516fcb0e2b1aae46664befd58c6cd))


### Bug Fixes

* **hermes:** require execution proof before DEPLOYED reports ([b9be444](https://github.com/pgen0x/meteora-dlmm-trading-bot/commit/b9be444a762e245791a6e6fa58a3213d1fb912ce))
* **pipeline:** validate --from-signal record before use ([e116975](https://github.com/pgen0x/meteora-dlmm-trading-bot/commit/e1169750f194aaa8fe92962f52918581f8cefaeb))

## [1.0.0](https://github.com/pgen0x/meteora-dlmm-trading-bot/compare/v0.1.0...v1.0.0) (2026-07-05)


### ⚠ BREAKING CHANGES

* rename repo to meteora-dlmm-trading-bot for SEO, add keyword-rich README intro

### Features

* loosen casual fee gate, trailing gap-through grace, reject tally; scrub instance-specific refs ([e0b3fc0](https://github.com/pgen0x/meteora-dlmm-trading-bot/commit/e0b3fc05174f301bd8bd2d231741823767e61162))
* **skill:** asymmetric-exit fixes — emergency SL floor, profit ratchet, journal reconciliation ([c4c4369](https://github.com/pgen0x/meteora-dlmm-trading-bot/commit/c4c4369a8763c87ccc5c4f927ea538001485bd28))


### Miscellaneous Chores

* rename repo to meteora-dlmm-trading-bot for SEO, add keyword-rich README intro ([768ed17](https://github.com/pgen0x/meteora-dlmm-trading-bot/commit/768ed17f54da62035772b3c6a3f265d4cd817bb5))

## [Unreleased]

### Changed
- Casual screening `MinFeeTVL` lowered 0.3 → 0.1. The discovery API's
  `fee_tvl_ratio` is scoped to the queried timeframe, so for the 30m casual
  window 0.3 demanded a ~14.4%/day fee pace and passed ~0 pools outside meme
  frenzies (live probe: 30m median ratio ~0.01%). Diverges intentionally from
  the Python pipeline's `max(0.3, 0.15)` — see the comment in `screen.go`.
- Scanner cycle log now appends a per-gate reject tally
  (`rejects[fee/TVL=36 non-SOL_pool=12 ...]`) so screening behavior is
  observable without a custom probe.
- Trailing take-profit gained a one-tick gap-through grace: when PnL wicks
  below both the ratchet floor and the +0.3% round-trip-cost lock between
  monitor ticks, the close is deferred one cycle instead of realizing a loss
  labeled "take-profit". Slow bleeds still close one tick later; the
  emergency stop-loss floor is unaffected.
- Template and asset copy scrubbed of instance-specific references (agent
  name, wallet-history stats) — the repo is public; deployment personalizes
  via the profile.

### Added
- `dlmm_reconcile.py`: audits the local close journal against the Meteora
  portfolio API (ground truth) — flags unjournaled closes and PnL divergences.
- Weekly journal-reconciliation cron job (Monday 09:00) added to the cron
  template; template regenerated from the live profile jobs (all 5 jobs,
  chat ids and profile paths re-templated).
- Monitor journals every close to `memories/dlmm_closes.jsonl` with a uniform,
  API-verified schema (previously the journal missed ~95% of closes).
- Emergency stop-loss floor 3pp below the configured hard SL: closes
  immediately, bypassing the age grace, AI holds, indicator timing, and
  `--report-only` (opt out with the new `--no-enforce` flag).

### Changed
- Trailing take-profit now uses a profit-ratchet floor (peak ≥5% locks +2%,
  ≥10% locks +6%, ≥20% locks 70% of peak) instead of a flat drop from peak;
  SOUL trigger lowered 5% → 3% (close-history analysis showed the 5% trigger
  almost never activated).
- Hard-SL grace period is now conditional (young AND in-range AND
  fee/TVL ≥ 10%) instead of unconditional 15 minutes — the unconditional
  grace let dumping positions ride far past the SL before it fired.
- Emergency close reasons can no longer be overwritten by softer rules
  (pumped-above / OOR / low-yield) that fire in the same cycle.

## [0.1.0] - 2026-07-02

Initial beta release. Everything below was consolidated from pre-release
development history into this first tagged version.

### Added
- Go daemon (`mdtb`): continuous poll ▸ screen ▸ dedup ▸ forward loop against
  Meteora's public pool-discovery API.
- Dual-mode screening: `casual` (30m, volume-spike plays) and `multiday`
  (24h, quality holds), each with independent thresholds and isolated
  position budgets.
- Layered risk gates: TVL, fee/TVL, market cap, holder count, organic score,
  top-10/dev supply concentration, mint/freeze authority, Jupiter shield
  status, and a best-effort DexScreener downtrend filter (Meridian
  degen-score and bin-step gates ported from the upstream Python pipeline).
- Batch AI-Pick signalling: one HMAC-signed webhook per cycle carries every
  qualifying pool as an array, replacing first-come-per-pool sends so the
  agent compares the full batch before deploying.
- Pluggable dedup store: in-memory, or Redis with a per-pool rolling TTL
  (`SetNX`, not a shared-set `SAdd`+`Expire`, so pools don't dedupe forever).
- `solana-dlmm` skill: `dlmm_pipeline.py` (ingestion/deploy), `dlmm_monitor.py`
  (exit management — stop-loss, trailing take-profit, out-of-range, and a
  Close GUARD that refuses to close a healthy in-range high-fee position),
  `dlmm_executor.js` (on-chain execution).
- `install.sh`: wires the skill, webhook subscription, SOUL.md section-9
  template, and DLMM-relevant cron job templates (5m position monitor, daily
  self-improvement review) into a Hermes profile, and builds the daemon.
- `docs/SIGNAL_SCHEMA.md`: the webhook payload contract.
- `CLAUDE.md`: architecture and convention notes for AI-assisted development.
- `./mdtb -version` / `main.Version` for reporting the running build version.

### Changed
- `assets/skill/scripts/` is symlinked into installed profiles instead of
  copied + `sed`-rewritten — edits in this repo now take effect in every
  installed profile immediately. Scripts resolve their own profile directory
  at runtime instead of relying on an install-time path substitution.
- `dlmm_executor.js` resolves its own path via `process.argv[1]` rather than
  `__dirname`, since Node always resolves symlinks for the latter (unlike
  Python's `__file__`), which broke wallet-key lookup once scripts became
  symlinked.
- npm dependencies for the skill are installed at the repo level
  (`assets/skill/node_modules`), not per-profile, since `require()` resolution
  follows the same realpath-through-symlinks behavior as `__dirname`.

### Fixed
- Webhook report formatting switched to a native pipe-table (code-block
  fencing was falling back to legacy MarkdownV2 and leaking literal escape
  characters).
- Audit-token reject gate loosened to stop over-rejecting on non-critical
  risk levels.

### Security
- Removed hardcoded Helius/QuickNode RPC provider keys that had been
  committed in plaintext since the initial commit. `dlmm_executor.js` now
  reads `SOLANA_RPC_URLS` from the profile `.env`, falling back to the public
  mainnet-beta RPC if unset. Git history was scrubbed
  (`git filter-repo --replace-text`) and force-pushed; the original keys were
  rotated at the provider regardless, since history rewrites don't un-expose
  something GitHub already cached.
