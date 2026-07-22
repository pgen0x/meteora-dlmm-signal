# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [1.14.1](https://github.com/pgen0x/meteora-dlmm-trading-bot/compare/v1.14.0...v1.14.1) (2026-07-22)


### Bug Fixes

* give release-please a parseable commit for the sol_bidask merge (PR [#39](https://github.com/pgen0x/meteora-dlmm-trading-bot/issues/39)) ([bc937d2](https://github.com/pgen0x/meteora-dlmm-trading-bot/commit/bc937d28cafbe30478a8847706e5e905f90edd18))

## [1.14.0](https://github.com/pgen0x/meteora-dlmm-trading-bot/compare/v1.13.0...v1.14.0) (2026-07-21)


### Features

* sol_bidask default strategy + asymmetric exit overhaul ([bb9ed72](https://github.com/pgen0x/meteora-dlmm-trading-bot/commit/bb9ed7273e09a959b9362bc3744d3fe68d29bddf))


### Bug Fixes

* GUARD hard floor matches the new -25% SL and cites the rug gate ([75a5aea](https://github.com/pgen0x/meteora-dlmm-trading-bot/commit/75a5aea40d1df73ff35333a3f44e1acf820773db))

## [1.13.0](https://github.com/pgen0x/meteora-dlmm-trading-bot/compare/v1.12.0...v1.13.0) (2026-07-17)


### Features

* auto-unwrap WETH to keep a native-ETH gas reserve ([255d20d](https://github.com/pgen0x/meteora-dlmm-trading-bot/commit/255d20d3cc7516317d9298ecc07dc07f80b564a7))
* automatic direct-deploy dispatch for Robinhood Chain (ROBINHOOD_DEPLOY_ENABLED) ([f1504bf](https://github.com/pgen0x/meteora-dlmm-trading-bot/commit/f1504bfba4bb6c61a6e7def82776041ca9110bf1))
* copycat guard for Robinhood venue — intra-batch same-symbol collision ([e0ce2f0](https://github.com/pgen0x/meteora-dlmm-trading-bot/commit/e0ce2f01f46ca86fe6df55e742b9c41b92ebc862))
* dynamic position sizing for the Robinhood venue (port of compute_deploy_amount) ([11ab447](https://github.com/pgen0x/meteora-dlmm-trading-bot/commit/11ab447c8952135545518894eb3dd95481f279f3))
* monitor walks both executors so v4 positions get exits ([14a8af7](https://github.com/pgen0x/meteora-dlmm-trading-bot/commit/14a8af70dbb18e6db0b1dd93ac879f29a4a321bf))
* pad gas estimates 30% and label v3 positions by pair ([bfde144](https://github.com/pgen0x/meteora-dlmm-trading-bot/commit/bfde144eb18cc3faddd79da4b2706b49cebd54be))
* port meridian screening + exit upgrades ([8aaa241](https://github.com/pgen0x/meteora-dlmm-trading-bot/commit/8aaa241e155a8af42f680ba983de01bf9e46c3d9))
* rh-mature mode — established fee-printers via Uniswap's own gateway ([2853812](https://github.com/pgen0x/meteora-dlmm-trading-bot/commit/2853812c60223afc8f4653a51f29d04b2389204e))
* Robinhood Chain venue — GeckoTerminal discovery, screening, GMGN/Blockscout safety gates (observe-only) ([fe9f8f9](https://github.com/pgen0x/meteora-dlmm-trading-bot/commit/fe9f8f98ea105b7b26371265940550efe806dec7))
* screening recalibration from the 14d close journal ([3208589](https://github.com/pgen0x/meteora-dlmm-trading-bot/commit/3208589629afc93317682d5b5418acc6e5be3c00))
* supertrend/RSI timing gates for the Robinhood venue ([f305db9](https://github.com/pgen0x/meteora-dlmm-trading-bot/commit/f305db97e5648805b8fd67108b494bb307634ba1))
* uni_executor.js — Uniswap v3 executor for Robinhood Chain (viem) ([21372c2](https://github.com/pgen0x/meteora-dlmm-trading-bot/commit/21372c29b1863b6d68a700c5175429760af414b1))
* uni_monitor.py --report-only + rh_dlmm_position_monitor Hermes cron ([bbcd7e8](https://github.com/pgen0x/meteora-dlmm-trading-bot/commit/bbcd7e8b7ab03747ab93a1b26efd3d554a8d3f21))
* uni_monitor.py — Robinhood Chain position monitor with Solana exit rulebook ([77cdc67](https://github.com/pgen0x/meteora-dlmm-trading-bot/commit/77cdc671faa329174627588c07685998df252d05))
* Uniswap v4 + USDG — discovery, screening, and live execution (Phases 6+7) ([4057171](https://github.com/pgen0x/meteora-dlmm-trading-bot/commit/40571716980ba3133aebc9560427ef601a32d4c0))


### Bug Fixes

* a failed exit sell no longer strands the token side of a close ([1b3e28b](https://github.com/pgen0x/meteora-dlmm-trading-bot/commit/1b3e28b2e6639f9b26f875b53193d9599345fb3b))
* GeckoTerminal keyless tier throttles ~4 req/min, not 30 — shrink discovery budget ([44a2a72](https://github.com/pgen0x/meteora-dlmm-trading-bot/commit/44a2a727cefef844c6102454c3023e2c8d29c087))
* MinReserveUSD 8000-&gt;2500, was killing 73% of pools before any real gate ran ([3f003b6](https://github.com/pgen0x/meteora-dlmm-trading-bot/commit/3f003b62fd98d7584f4764f1575d93cdfda062e1))
* monitor loops froze DRY_RUN at launch; GeckoTerminal 403'd the Python UA ([61f5423](https://github.com/pgen0x/meteora-dlmm-trading-bot/commit/61f5423c55fa77dac0a628456b5fd628dd26ec3f))
* never journal tokenId="unknown" — orphan disables monitor SL/TP ([5fe73e8](https://github.com/pgen0x/meteora-dlmm-trading-bot/commit/5fe73e826c09532cd74f7d2625cbf03384ceb0f5))
* price the position at what the mint actually took, not what we offered ([09e1a8e](https://github.com/pgen0x/meteora-dlmm-trading-bot/commit/09e1a8ed54bb87ea4002d7349857c6a539cd00b9))
* strip executor JSON tail from the Robinhood deploy report line ([9ccef1a](https://github.com/pgen0x/meteora-dlmm-trading-bot/commit/9ccef1a2e41ec284084a35daebb83bae37e71b64))

## [1.12.0](https://github.com/pgen0x/meteora-dlmm-trading-bot/compare/v1.11.2...v1.12.0) (2026-07-13)


### Features

* deterministic batch picker (direct deploy mode) ([d438b9e](https://github.com/pgen0x/meteora-dlmm-trading-bot/commit/d438b9eb9ec99639d5c81f91a4aaa2054f49dfd3))

## [1.11.2](https://github.com/pgen0x/meteora-dlmm-trading-bot/compare/v1.11.1...v1.11.2) (2026-07-10)


### Bug Fixes

* version badge lost color segment on release-please bump ([f7fae01](https://github.com/pgen0x/meteora-dlmm-trading-bot/commit/f7fae01e9999d44c00372899fd32d6381a0d5996))

## [1.11.1](https://github.com/pgen0x/meteora-dlmm-trading-bot/compare/v1.11.0...v1.11.1) (2026-07-10)


### Bug Fixes

* guard against phantom -100% PnL reads from the Portfolio API ([d0a0132](https://github.com/pgen0x/meteora-dlmm-trading-bot/commit/d0a0132c1de8fa6be1380e36c514d7844c1d244e))
* guard against phantom -100% PnL reads from the Portfolio API ([77c14d0](https://github.com/pgen0x/meteora-dlmm-trading-bot/commit/77c14d07e5510465886a05a46eff61b750765e89))

## [1.11.0](https://github.com/pgen0x/meteora-dlmm-trading-bot/compare/v1.10.0...v1.11.0) (2026-07-10)


### Features

* balanced_tight two-sided strategy + GMGN insider/bundler hard gate ([a09d775](https://github.com/pgen0x/meteora-dlmm-trading-bot/commit/a09d775e2d5550919f8b333b7fe38fc5db11d8d7))
* balanced_tight two-sided strategy, GMGN rug gate, unmark-on-close ([b1b99b3](https://github.com/pgen0x/meteora-dlmm-trading-bot/commit/b1b99b3f3521164b59a6f776dca7548853f2c073))
* clear signal-seen marker on position close (unmark-on-close) ([5d81a33](https://github.com/pgen0x/meteora-dlmm-trading-bot/commit/5d81a3358947cd9101f3f4c01f03924fb1b1c616))

## [1.10.0](https://github.com/pgen0x/meteora-dlmm-trading-bot/compare/v1.9.0...v1.10.0) (2026-07-09)


### Features

* GMGN holder-quality enrichment for signal candidates ([7309540](https://github.com/pgen0x/meteora-dlmm-trading-bot/commit/7309540e6f49d88135a8e1b65c1ae6483783b113))
* GMGN holder-quality enrichment for signal candidates ([e715bb5](https://github.com/pgen0x/meteora-dlmm-trading-bot/commit/e715bb531b27f61ae1cff32d389ff270861197fe))
* mode-scoped dedup window for casual (CASUAL_SEEN_TTL, default 6h) ([fb383c5](https://github.com/pgen0x/meteora-dlmm-trading-bot/commit/fb383c55f4dc1eb5801f3f335171d00d2bff0e12))
* mode-scoped dedup window for casual (CASUAL_SEEN_TTL, default 6h) ([66ba00d](https://github.com/pgen0x/meteora-dlmm-trading-bot/commit/66ba00daafecc5d8f5dd939c867e76de851eb9a8))

## [1.9.0](https://github.com/pgen0x/meteora-dlmm-trading-bot/compare/v1.8.0...v1.9.0) (2026-07-09)


### Features

* **cron:** 1h-momentum override in position-monitor GUARD ([75d1008](https://github.com/pgen0x/meteora-dlmm-trading-bot/commit/75d1008e0d868ccd888c38885aedae8d18b6f537))
* **monitor:** sustained-downtrend exit rule in the 20s loop ([6edce3a](https://github.com/pgen0x/meteora-dlmm-trading-bot/commit/6edce3ad0ea51f864a40dc6c0e7210a7486b9455))
* **turnover:** fast-cycle — no trailing TP, 2h dedup window ([18d868d](https://github.com/pgen0x/meteora-dlmm-trading-bot/commit/18d868dcdd12d18d1a000c8b87eaa406b90e735c))


### Bug Fixes

* **cron:** forbid fee/TVL as HOLD justification for OOR positions ([784633d](https://github.com/pgen0x/meteora-dlmm-trading-bot/commit/784633d7c62324768afca6c68a0126a2257f373f))
* exit discipline + turnover fast-cycle ([d685923](https://github.com/pgen0x/meteora-dlmm-trading-bot/commit/d68592333303ca83ada145669f3142d8ac01e22c))
* remove hardcoded wallet address fallback from web3 scripts ([3148c70](https://github.com/pgen0x/meteora-dlmm-trading-bot/commit/3148c7046a7a9af268e8c58c87b4c50db8f1e715))

## [1.8.0](https://github.com/pgen0x/meteora-dlmm-trading-bot/compare/v1.7.0...v1.8.0) (2026-07-08)


### Features

* **skill:** dlmm_stats.py scoreboard + operator-configurable report timezone ([fe96923](https://github.com/pgen0x/meteora-dlmm-trading-bot/commit/fe96923b6c9bfdf4c656466a991747e71f4fbcd6))
* **skill:** fast-cycle scoreboard + operator-configurable report timezone ([afd9da8](https://github.com/pgen0x/meteora-dlmm-trading-bot/commit/afd9da81c85e3ee68adecd183688dd0ff89b91f6))

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

* **skill:** bid_ask bin shapes + fast-cycle turnover OOR rebalance ([6c2a2e3](https://github.com/pgen0x/meteora-dlmm-trading-bot/commit/6c2a2e35b65d8d2faabd88a63946401e6fb0ea9b))
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
  status, and a best-effort DexScreener downtrend filter (the reference bot
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
