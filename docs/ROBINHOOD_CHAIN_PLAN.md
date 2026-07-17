# Robinhood Chain Support — Research & Implementation Plan

Status: **planning** (branch `feat/robinhood-chain-support`, 2026-07-13)

## 1. What Robinhood Chain is (research summary)

- Ethereum L2 on the **Arbitrum Orbit** stack, public mainnet since **2026-07-01**.
  Chain ID **4663**, native gas token **ETH**, ~100 ms block times, settles to
  Ethereum. Public RPC: `https://rpc.mainnet.chain.robinhood.com`, explorer:
  `robinhoodchain.blockscout.com` (Blockscout, REST + JSON-RPC APIs).
- Fully EVM-compatible. Docs: `docs.robinhood.com/chain/`.
- **Uniswap v2 / v3 / v4 + UniswapX live from day one** — Uniswap is the
  primary AMM. First-week DEX volume crossed **$500M/24h**, driven by WETH
  pairs and a memecoin wave (~193k daily active addresses by 07-08).
- Launchpad ecosystem (the "pump.fun layer" that feeds new pools):
  - **Noxa.fun** — deploys ERC-20 **directly into a Uniswap v3 pool**
    (single-sided, 1% fee tier, LP locked) in one tx; "graduation" is only a
    milestone, not a migration. Standard v3 interfaces → trivially botable.
    1.8k → 6.7k token deploys in days.
  - **hood.fun** — fair-launch bonding-curve platform for the chain.
  - **pump.fun** added Robinhood Chain support (cross-chain).
- Tokenized stocks (AAPL/NVDA/... "Stock Tokens") trade 24/7 on Uniswap —
  separate, lower-volatility opportunity class from memecoins.

### Data/infra availability (maps to our current dependencies)

| Need (Solana today) | Robinhood Chain equivalent | Notes |
|---|---|---|
| Meteora discovery API | **GeckoTerminal** `/networks/robinhood/new_pools` (+ `/pools` trending) | 48h window, public tier 30 req/min; also mirrored via CoinGecko `/onchain` paid tier |
| DexScreener momentum gate | **DexScreener supports `robinhood`** chain slug (`dexscreener.com/robinhood`) | `momentum.go` mostly reusable — same API, different chainId |
| Jupiter token audit | **GoPlus Token Security API** (60+ EVM chains; verify 4663 support) + **honeypot.is** | honeypot/sell-tax simulation is the EVM must-have; Blockscout API for holder distribution & contract verification |
| GMGN smart money | **GMGN fully supports Robinhood Chain** (web/app/API, `gmgn.ai/security?chain=robinhood`) — confirmed 2026-07 | reuse `gmgn.go` with chain param; GMGN security data can also backstop the honeypot gate |
| Meteora DLMM SDK (JS executor) | **Uniswap v3 `NonfungiblePositionManager`** (mint/increase/decrease/collect) via viem + `@uniswap/v3-sdk`; v4 SDK later if needed | v3 first: Noxa launches land on v3, position model closest to DLMM bins |
| Solana wallet | EVM keypair; gas in ETH; capital in **WETH** | Need bridge step to fund; ERC-20 approval hygiene |

## 2. Strategy fit

Same alpha thesis as Solana: catch newly-created pools early, LP into a tight
concentrated range, harvest fees, exit on velocity/trailing rules. Uniswap v3
concentrated liquidity ≈ DLMM bins (ticks instead of bins). Differences that
change behavior:

> True bin-based DLMM **does exist on Robinhood Chain**: AEON Protocol hosts
> LFJ (Trader Joe) **Liquidity Book** pools — the model Meteora's DLMM was
> ported from — plus Algebra Integral CL pools. But launch-week volume (and
> therefore fee capture) concentrates on Uniswap, and launchpads (Noxa)
> graduate into Uniswap v3. Primary venue = Uniswap v3; AEON Liquidity Book
> is a secondary venue to evaluate once its new-pool flow is meaningful.

- **Fee model**: v3 fee tier is fixed per pool (1% on Noxa launches); no
  dynamic fees like DLMM. Fee/TVL gate still computable from GeckoTerminal
  (`volume_usd` × fee tier ÷ `reserve_in_usd`).
- **Honeypots/sell-tax** replace Solana's bot-holder problem as the #1 rug
  vector → GoPlus/honeypot.is gate must be **fail-closed** for
  honeypot=true, unlike our Solana fail-open convention (missing data can
  stay fail-open; positive detection rejects).
- **Gas is cheap ETH, blocks 100 ms** → deploy latency dominated by our poll
  interval, not the chain.
- **One-sided LP above price** (our `single_sided_reseed` analog) is native
  in v3: mint with range entirely above current tick, token-only.

## 3. Architecture plan

Principle: **new venue package beside `internal/meteora`, not a rewrite.**
Scanner loop, dedup store, deploy runner, webhook forwarder are already
venue-agnostic in shape; screening/discovery/executor are venue-specific.

### Phase 0 — spike ✅ (ran 2026-07-13, results below)
- **GeckoTerminal** `/networks/robinhood/new_pools`: 200 OK, 20 pools/page.
  Dex split on sample page: 14 uniswap-v3 / 4 uniswap-v4 / 1 v2 / 1 virtuals —
  v3-first confirmed. Fields: `reserve_in_usd`, `volume_usd` +
  `transactions` + `price_change_percentage` per window (m5→h24),
  `pool_created_at`, `fdv_usd`, `market_cap_usd`. `include=base_token,...`
  adds name/symbol/decimals. **Caveats**: no fee-tier field (parse from pool
  `name`, e.g. "CALLIE / WETH 0.3%", or `fee()` via RPC); v4 pool `address`
  is a bytes32 pool ID, not a contract address.
- **DexScreener**: `latest/dex/pairs/robinhood/{pool}` works, standard
  schema → `momentum.go` reusable with chainId swap.
- **GoPlus**: chain 4663 **NOT supported** (code 2022). **honeypot.is**:
  not supported ("Invalid chain"). Both dead ends.
- **GMGN OpenAPI**: `chain=robinhood` fully supported with existing
  exist-auth. `/v1/token/info` returns complete `wallet_tags_stat` /
  `stat` (rat %, bundler %, top-10) / `dev`, plus new useful fields:
  `launchpad`, `launchpad_status`, `trade_fee`, `standard`.
  **`/v1/token/security`** also live: `is_honeypot`, `is_blacklist`,
  `is_open_source`, `buy_tax`/`sell_tax`, `is_renounced`, lock info —
  fills the GoPlus hole. Fields are often `null`/`-1` (unknown) on fresh
  tokens → gate = fail-closed ONLY on positive detection
  (`honeypot=1`, sell_tax over cap), fail-open on null/unknown.
- **Blockscout API v2**: `/api/v2/tokens/{addr}` gives `holders_count`;
  `/holders` gives top-holder distribution. No key needed.
- **RPC** `rpc.mainnet.chain.robinhood.com`: live, `eth_chainId` = 0x1237
  (4663), batch JSON-RPC works.
- Still open: manual v3 mint + collect + burn (needs funded EVM wallet —
  do before Phase 2 goes live).

### Phase 1 — daemon: discover + screen + signal ✅ (landed 2026-07-13)
Implemented as a sibling package, not an interface extraction — the two
venues share the scanner loop, store and forwarder but their pool shapes
diverge too much for a common `Discover()` signature to earn its keep.
- `internal/robinhood/`:
  - `discover.go` — GeckoTerminal new_pools ×5 pages + trending_pools ×1,
    merged/deduped, Uniswap-v3-only. Pagination is load-bearing: launch
    velocity is ~7 pools/min, so ONE page spans ~2-3 minutes and anything
    old enough to pass MinAge has already scrolled off (first two smoke
    runs rejected 57/57 on age). 6 req/cycle « 30/min public budget.
  - `screen.go` — `Fresh` mode: WETH quote, age 3m–24h, reserve $8k–$500k,
    fee tier ≥0.25%, projected fee/TVL ≥5%/day (volume×tier — GT has no fee
    field), ≥30 txns + ≥12 buyers h1, buys-without-sells honeypot shape
    reject, FDV $20k–$50M, momentum gates on GT's own windows (no
    DexScreener call needed). Geometric-mean score like the degen score.
  - `safety.go` — GMGN OpenAPI `chain=robinhood` `/token/security` (honeypot/
    blacklist/sell-tax, **fail-closed on positive detection only**) +
    `/token/info` (rat/bundler caps reuse `GMGN_MAX_*`), Blockscout holder
    floor. GoPlus/honeypot.is dead ends per Phase 0.
- Config: `ROBINHOOD_ENABLED` (default false), `ROBINHOOD_DISCOVER_URL`,
  `ROBINHOOD_SEEN_TTL` (6h), `ROBINHOOD_MIN_HOLDERS` (50),
  `ROBINHOOD_WEBHOOK` (default false = **observe-only**: batches journal to
  the daemon log; the live Hermes prompt + deploy pipeline are Solana-only,
  so EVM payloads stay out of both until Phase 2).
- Store: dedup keys `rh:<mode>:<pool>`; schema documented in
  `docs/SIGNAL_SCHEMA.md` ("robinhood_pool_discovery").
- Unit tests: `internal/robinhood/screen_test.go` (gate matrix + security
  tri-state). Live smoke 2026-07-13: 61 fetched → 3 passed, fully enriched
  (holders, taxes, bundler %); rejects `reserve=50 fee_tier=5 too-young=3`.
- **Next**: run observe-only ≥3 days, calibrate `Fresh` thresholds from the
  journaled batches. Copycat same-symbol
  launches (two CALLIE pools in one cycle) now flagged: `copycat.go`
  `EnrichCopycat` sets `is_copycat`/`copycat_count` on intra-batch ticker
  collisions (advisory, EVM analog of Solana `pvp.go`), and the picker
  (`pickBest`) demotes copycats below any clean candidate. Same-cycle only;
  cross-cycle correlation would need persistent symbol memory (not built).

### Phase 2 — executor: deploy path (executor ✅ 2026-07-13; live spike + dispatch pending)
Landed: `assets/skill/scripts/uni_executor.js` (viem-only, no @uniswap SDKs).
Contract addresses verified on-chain (NPM.factory + NPM.WETH9 cross-checked):
factory `0x1f7d…2efa`, NPM `0x7399…e0d3`, SwapRouter02 `0xcaf6…5cb2`.
Strategies: `balanced_tight` (swap half, ±range% band) and `weth_below`
(one-sided WETH band adjacent to tick, no swap). Exact-amount approvals only.
Wallet: `EVM_PRIVATE_KEY` in profile `.env` accepts a dedicated 0x-hex key OR
the base58 Solana secret (ed25519 seed reused as secp256k1 scalar) — the
Solana-derived account is the current setup. Read-only + DRY_RUN paths tested
live. **Wallet funded 2026-07-13**: `0x94098ccD4536729AAEcc113C313E11926A5bec2d`
(Phantom-exported EVM key, standard BIP44 path — NOT the ed25519-seed-derived
stopgap account) holds ~0.00104 ETH (gas) + 0.0085 WETH (LP capital), both
bridged from BNB/SOL via Across/Mayan.

**Automatic dispatch landed** (commit f1504bf, `internal/robinhood/deploy.go`
+ `Scanner.robinhoodDeploy`): mirrors Solana's `DEPLOY_CMD` pattern exactly —
`ROBINHOOD_DEPLOY_ENABLED=true` makes the daemon pick the batch's
highest-`Score` candidate and mint it via `uni_executor.js`, bypassing
OBSERVE/webhook entirely. No re-ranking pipeline needed (screen.go already
scores every candidate) — picking is a plain argmax. Fails closed on any
`OpenPositions` read error and enforces `ROBINHOOD_MAX_OPEN_POSITIONS`
(default 1) before every deploy — **the only safety brake**, since Phase 3
(monitor/exits) does not exist yet: a deployed position stays open until
closed by hand via `uni_executor.js close --id N`.

**LIVE since 2026-07-13**: `ROBINHOOD_DEPLOY_ENABLED=true` + profile
`DRY_RUN=false` — the daemon now mints real positions with real WETH on a
qualifying batch. Went live after the paper-mode (`DRY_RUN=true`) pick→deploy
log path was observed end-to-end and Phase 3 (monitor exits) landed, so a
deployed position auto-closes on the exit rules rather than sitting open until
closed by hand. Capital is the funded wallet's ~0.0085 WETH; `uni_executor.js`
sizes each deploy from that balance.

**Remaining**: gate calibration from live journals (Phase 4) — no code gaps in
the entry→monitor→exit→report loop.
- `assets/skill/scripts/uni_executor.js` (or `.ts`) — viem +
  `@uniswap/v3-sdk`: wrap ETH→WETH, swap for target token, mint position
  (two-sided `balanced_tight` analog and one-sided above-price reseed),
  collect fees, decrease/burn. Strict approval scoping (exact allowance,
  revoke on close).
- `dlmm_pipeline.py --from-batch` grows a `--chain` switch: same
  deterministic re-rank, dispatches to uni executor for robinhood signals.
  Keep per-mode floors/penalty weights separate from Solana's (fresh
  calibration, don't share Darwinian weights).
- Wallet: `EVM_PUBLIC_KEY`/`EVM_PRIVATE_KEY` in Hermes profile `.env` only
  (same secrets policy as Solana keys — never in repo).
- Start `DRY_RUN`, then tiny fixed size (e.g. 0.005–0.01 WETH) real deploys.

### Phase 3 — monitor: exits ✅ (landed 2026-07-13, commit 77cdc67)
- `assets/skill/scripts/uni_monitor.py` — one-shot scan, run every 60s by
  `uni_monitor_loop.sh` under user systemd unit **`rh-dlmm-monitor.service`**
  (enabled + active). Ports dlmm_monitor.py's exit rulebook verbatim (same
  percent thresholds per operator "same like solana"): emergency SL, hard SL
  -25%, hard TP +50%, trailing profit-ratchet (arms +5%, `trailing_floor_pct`
  identical shape), fast-out velocity (-3% 5m dump while armed), sustained
  downtrend (1h ≤ -5% AND pnl ≤ -5%), out-of-range timeout 30m.
- PnL is WETH-denominated. Position value comes from `uni_executor.js state`:
  a **simulated** full `decreaseLiquidity` (reuses the pool contract's own
  tick math — no reimplemented LiquidityAmounts) + owed fees, converted to
  WETH via `sqrtPriceX96`, compared to the journaled entry cost basis
  (`memories/uni_positions.jsonl`, written on each real mint). Momentum
  (m5/h1) from GeckoTerminal per pool, best-effort/fail-open.
- The **phantom-PnL guard was intentionally NOT ported**: it existed for the
  Meteora portfolio API returning pnl=-100 on unindexed positions. Here PnL
  is computed from on-chain state we read directly, so that failure mode
  doesn't exist.
- Close authority: `uni_executor.js close` refuses without `UNI_CLOSE_AUTH=1`
  (set only by the monitor) or `--force` (manual) — the monitor is the sole
  automated closer, mirroring Solana's `DLMM_CLOSE_AUTH`. On close: swap token
  leg back to WETH, journal to `memories/uni_closes.jsonl`, hermes alert.
- Peak/OOR-timer state persists in `memories/uni_monitor_state.json` across
  ticks. `DRY_RUN=true` tracks peaks + prints decisions but simulates closes.
- With the monitor live, `ROBINHOOD_MAX_OPEN_POSITIONS` is no longer the
  *only* safety brake — positions now auto-close on the exit rules.
  `DRY_RUN=false` set 2026-07-13 (after a dry-run monitor cycle was observed).

### Phase 3b — Hermes reporting cron ✅ (landed 2026-07-13)
Operator-facing visibility, the EVM analog of Solana's `sol_dlmm_position_monitor`
cron. Distinct from the systemd loop: the loop *acts* (closes), the cron only
*reports*.
- `uni_monitor.py --report-only` — pure read: positions + persisted peak/OOR
  state + GeckoTerminal momentum → a "Robinhood LP Status" card + a
  `MONITOR_REPORT:{...}` JSON line. **Never** closes or writes
  `uni_monitor_state.json`, so a report tick can't race the loop's on-chain
  writes (the loop owns all state mutation). Empty positions → parseable
  `{"positions":[]}`; a positions-read failure → `{"positions":[],"error":...}`
  so the agent surfaces the error instead of fabricating a card.
- Cron `rh_dlmm_position_monitor` in `assets/hermes/cron_jobs_template.json`
  (installed live in the `solanza` profile, `deliver=telegram`, every 30m,
  `terminal` toolset only). Prompt hard-forbids trading (no executor, no
  close/mint) and copies the script's card verbatim; empty positions → `[SILENT]`.
- Validated 2026-07-13: `hermes cron run` → ok, 0 open positions → SILENT (no
  spam). Card format proven against a synthetic 2-position sample.
- Parity choice to revisit: `[SILENT]` on empty mirrors the Solana monitor, but
  the operator has said "don't silent until I ask" — may want a heartbeat card
  instead (one-line prompt change).

Original Phase 3 sketch (superseded by the above):

### Phase 4 — calibrate + harden
- Tune gates from Phase 1–2 journals (expect different scarcity profile than
  Solana casual mode).
- Rate-limit management: GeckoTerminal 30/min public — consider CoinGecko
  paid onchain tier if poll cadence needs it.
- Optional later: v4 pools (hooks introduce per-pool custom logic = new rug
  vector — needs its own safety screen), UniswapX, tokenized-stock LP mode
  (different regime: low vol, fee-capture only).

### Phase 5 — `rh-mature` mode: established fee-printers ✅ (landed 2026-07-14)

Phase 1–4 only ever saw the launch window. That was a **structural blind spot,
not a threshold choice**: GeckoTerminal's `new_pools` is a launch feed, a pool
scrolls off it in minutes, and `Fresh.MaxAge` rejects anything over 24h. Pools
that survive their launch and keep printing fees for days were invisible to us.

**Discovery source: Uniswap's own interface GraphQL gateway** —
`https://interface.gateway.uniswap.org/v1/graphql`, the backend behind
app.uniswap.org's Explore ▸ Pools table. Keyless, unauthenticated, speaks
`chain: ROBINHOOD` natively (verified 2026-07-14). Needs a browser-shaped
`Origin` header. Schema introspection is disabled; operation names come from the
interface's own network traffic.

Measured shape of `topV3Pools(chain: ROBINHOOD)` on 2026-07-14:

- Returns the **entire indexed universe: 74 pools**, sorted TVL descending,
  bottoming out at ~$12.6k TVL (`tvlCursor` pages down to it, then stops).
- **Zero pools younger than 24h.** It is a TVL leaderboard with an indexing lag,
  NOT a discovery feed — so it cannot replace `new_pools` for `rh-fresh`, and
  `new_pools` cannot serve `rh-mature`. The two modes need two sources. That is
  the whole reason `pollRobinhood` takes an `rhFetcher`.
- Gives: address, `createdAtTimestamp`, `feeTier` (hundredths of a bip),
  `totalLiquidity`, `cumulativeVolume(duration: DAY)`, token0/token1 with
  decimals. token0/token1 are ordered **by address, not by role** — WETH lands
  on either side, hence `toPool`'s orientation step.
- Does NOT give: h1 volume, buys/sells/buyers/sellers, price-change windows, or
  FDV. `cumulativeVolume` rejects an `HOUR` duration; DAY is the finest window.

**Two-hop fetch** (`mature.go`), two HTTP calls per cycle:

1. Gateway → whole v3 universe (TVL, fee tier, age, 24h volume).
2. Local prefilter (no I/O, a strict subset of `Screen`) → ONE GeckoTerminal
   `/pools/multi/` call (takes up to 30 addresses) fills in the h1 flow,
   price-change windows and FDV that `Screen` gates on.

The prefilter is load-bearing: without it this fans out per-pool and burns the
GT budget `rh-fresh` depends on (the keyless tier really throttles at ~4 req/min
— see `discover.go`).

**`Mature` gates** — start where `Fresh` ends so the two modes partition the age
axis and no pool can signal twice: age ≥24h, **no ceiling** (`MaxAge: 0`; a pool
printing fees for a month is more proven, not less — the fee-pace gate expires a
stale pool on evidence, not on a clock), reserve $12.5k–$500k (5× Fresh's floor:
this mode holds for days and needs an exit), fee tier ≥0.25%, fee/TVL **≥8%/day**
(~2900% APR), ≥60 txns + ≥20 buyers h1, FDV $20k–$50M.

**`FeePaceH24`** is the one new `ModeParams` knob. Fresh extrapolates the h1
window out to a day because a minutes-old pool has no other history; a mature
pool has a realized 24h volume, and extrapolating h1 would let one busy hour
mint a 24×-inflated daily rate — precisely the pool this mode must not buy.

**Live calibration run (2026-07-14), and the finding that matters:**

| | |
|---|---|
| Gateway universe | 74 pools |
| Cleared a 5%/day fee pace inside the reserve band | 19 — **every one 66–144h old**, i.e. 100% invisible to `rh-fresh` |
| Shortlist after `Mature` prefilter (8%/day) | 7 |
| Passed the full `Screen` | **1** (MEOW/WETH: 9.0%/day, $82k TVL, 46 buyers/h) |

Of the 6 rejects, **4 failed the momentum gates, not the yield gates.**
DATABEAR/WETH — the headline pool, $65k TVL, $1.44M 24h volume, 1% tier =
**22%/day ≈ 8000% APR** — was rejected on `1h -18.3%`. That is the APR trap
stated precisely: **on these pools a spectacular fee yield is frequently being
paid by a collapsing price**, and impermanent loss eats the fees. The existing
momentum gates already catch it; no new gate was needed, and the high-yield
rejects are the point of the mode, not a bug in it.

Config: `ROBINHOOD_MATURE=true` (independent of `ROBINHOOD_ENABLED` — either mode
runs alone). Every downstream gate is shared and unmodified: dedup
(`rh:rh-mature:<pool>`), Blockscout holders, GMGN security + holder quality,
copycat guard, and the same deploy path.

**Open**: thresholds are FIRST-PASS, calibrated on one 74-pool sample — expect
churn. `MinTxH1: 60` already rejected one pool (MEOWOOD, 52 txns) that may
deserve to pass. `ROBINHOOD_SEEN_TTL=6h` means a still-qualifying mature pool
re-signals every 6h; that is arguably correct for a re-entry candidate, but it is
untested and interacts with `ROBINHOOD_MAX_OPEN_POSITIONS`.

### Phase 6 — v4 + USDG discovery ✅ (landed 2026-07-14, observe-only)

The 2026-07-14 research run measured why these are ONE feature, not two: of the
top 80 v4 pools (gateway `topV4Pools`), **47 were USDG-sided, 36 native-ETH,
only 4 WETH** — while v3 is 67/69 WETH. v4-without-USDG or USDG-on-v3 each buy
approximately nothing; together they open a fee-printer universe the WETH/v3
screen was structurally blind to (6 of the day's 9 v4 printers ≥10%/day were
USDG-quoted).

What landed (discovery/screening only — **no v4 execution**):

- **Quote whitelist** (`types.go`): WETH + USDG
  (`0x5fc5360d0400a0fd4f2af552add042d716f1d168`, **6 decimals**) + native ETH
  (zero address — v4 pools ether directly, no wrap). `orientQuote` repairs
  orientation on BOTH feeds: GT lists USDG base-side in USDG/memecoin pools,
  and GT spells v4 native ETH as the zero address with symbol "WETH".
- **rh-fresh** (`discover.go`): the dex filter now branches on
  `uniswap-v4-robinhood` instead of dropping it (v4's `address` is the bytes32
  poolId; GT accepts it everywhere, `/pools/multi/` and OHLCV included). One
  extra aliased gateway call per cycle (`fillV4Meta` → `v4Pool(chain, poolId)`
  fan-in) resolves what GT does not carry: hook, dynamic-fee flag, true fee
  tier (v4 names often omit the fee suffix `parseFeePct` reads).
- **rh-mature** (`mature.go`): fetches both leaderboards — `topV4Pools` beside
  `topV3Pools`, no unified query exists (`first` max 100). A v4 fetch failure
  degrades to a v3-only cycle rather than blanking the mode.
- **v4 hard gates** (`screen.go` + prefilter): reject `hook != null` and
  `isDynamicFee`. A hook can block or skim withdrawals (behavior is encoded in
  the 14 low bits of the hook address; the Cork-exploit class) and a dynamic
  fee invalidates the fee-pace math. Cost today: ~zero — 79/80 top v4 pools
  are hookless.
- **Second fail-closed divergence**: a v4 pool whose gateway meta cannot be
  resolved this cycle is dropped (not marked seen, retries next cycle) — an
  unverified hook must never pass by looking hookless.
- **Deploy stays v3-only**: `robinhoodDeploy` filters `protocol == "v4"` out
  before picking; the v3 executor cannot speak to a poolId (no pool contract
  exists — state lives in the singleton PoolManager).
- Payload adds `protocol` ("v3"/"v4") and `hook` (omitted while the hook gate
  holds) — see docs/SIGNAL_SCHEMA.md.

### Phase 7 — v4 execution ✅ (LIVE 2026-07-15)

Validation round-trip completed on-chain 2026-07-15 (position 96988,
CASHCAT/WETH 0.2888%: mint 0.002 WETH → state → collect → close, net cost
~0.000015 WETH + gas), then `ROBINHOOD_V4_EXECUTOR_CMD` enabled in the live
daemon env and the service restarted. Two chain-specific findings from the
rollout, both fixed in `uni_v4_executor.js`:

1. **Robinhood's UniversalRouter is an older v4-periphery build** — its
   `ExactInputSingleParams` still has `sqrtPriceLimitX96`; the modern 5-field
   struct reverts with empty data. The V4Quoter is a NEWER build (5-field).
   The executor encodes each contract's own vintage; don't harmonize them.
2. **Exact-liquidity mints are drift-brittle**: posm has no v3-style partial
   fill, so any adverse price move between sizing and mint reverts
   `MaximumAmountExceeded` — on a 100ms-block chain with approval txs in
   between, that was every attempt on a busy pool. Fixed by approving before
   the price sample, shaving liquidity 0.3%, and one reprice-retry.

Verified-on-chain addresses (chain 4663):
PoolManager `0x8366a39cc670b4001a1121b8f6a443a643e40951`, PositionManager
`0x58daec3116aae6d93017baaea7749052e8a04fa7`, StateView
`0xf3334192d15450cdd385c8b70e03f9a6bd9e673b`, V4Quoter
`0x8dc178efb8111bb0973dd9d722ebeff267c98f94`, UniversalRouter
`0x8876789976decbfcbbbe364623c63652db8c0904`, Permit2 (canonical)
`0x000000000022D473030F116dDEE9F6B43aC78BA3`.

What landed:

- **`uni_v4_executor.js`** — full sibling of the v3 executor (same command
  set: address/balance/quote/deploy/positions/state/collect/close/sweep/
  unwrap, same DRY_RUN + `UNI_CLOSE_AUTH` contracts, same JSON-last-line
  output). The execution deltas the research called out are all in: state via
  StateView `getSlot0(poolId)` + fee-growth deltas (the v4 state read DOES
  price live-accrued fees, unlike v3's); mint/burn via posm
  `modifyLiquidities` with hand-rolled Actions encoding; fee collect =
  decrease-0 + TAKE_PAIR; Permit2 approvals for ERC-20 sides; native-ETH
  quotes settle raw ether (msg.value in, excess rewrapped on close); swaps
  via UniversalRouter `V4_SWAP` quoted by V4Quoter. Journals are separate
  files (`uni_v4_positions.jsonl`, `uni_v4_stranded.jsonl`) in the v3 line
  format — NPM and posm tokenId series both start at 1, so a shared file
  would collide cost bases. The posm has no `tokenOfOwnerByIndex`, so the
  mint journal (filtered by live `ownerOf`) IS the position index.
- **Daemon dispatch** (`scanner.go`, `deploy.go`, `config.go`):
  `ROBINHOOD_V4_EXECUTOR_CMD` wires a second `robinhood.Runner`; unset keeps
  v4 observe-only (the pre-Phase-7 behavior, now an eligibility filter
  instead of a hard drop). The pick chooses the runner by candidate
  protocol; the position cap counts BOTH executors (one wallet, one brake);
  USDG-quoted picks size off the USDG balance with dollar-unit params
  (`ROBINHOOD_DEPLOY_{RESERVE,PCT,FLOOR,CEIL}_USDG`) and forward `--quote`
  so the executor knows which PoolKey side to settle in.
  `ROBINHOOD_DEPLOY_MODES` gates direct-deploy per mode — the fresh feed's
  live close journal (9 of 10 closes were emergency-SL losses, median −49%)
  argued for mature-only trading with fresh kept as an observe journal.
- **Monitor** (`uni_monitor.py`): one tick walks both executors (v4 skipped
  when the script is absent); v4 peak/oor state keys are namespaced
  `v4:<tokenId>` (v3 keys stay bare for compat with the live state file);
  the card, close journal, and alerts are quote-aware (`quoteSymbol` —
  USDG values are already dollars, no ETH/USD conversion); sweep runs per
  executor against the separate stranded journals. A positions-read failure
  on one executor no longer blinds — or prunes the state of — the other.

**Open before live v4 money**: no on-chain v4 mint has run yet — first
rollout step is a tiny manual `deploy --pool <poolId> --amount <floor>` on a
top hookless pool, then a `state`/`collect`/`close` round-trip, before
enabling `ROBINHOOD_V4_EXECUTOR_CMD` for the daemon. USDG sizing defaults
(reserve $5 / floor $8 / ceil $150) are unlived-in; recalibrate from the
close journal like the WETH set.

## 4. Risks / open questions

1. **GoPlus coverage of chain 4663** unverified — if unsupported at launch,
   fall back to honeypot.is or local simulation (eth_call a buy+sell probe).
2. **Sniper/MEV competition** on 100 ms blocks; we LP rather than snipe, so
   less exposed, but entry swaps need slippage caps + deadline.
3. **GeckoTerminal freshness/lag** vs Meteora's first-party API — measure in
   Phase 0; DexScreener `new-pairs` as cross-check source.
4. **Capital split** between Solana and Robinhood wallets — operational
   decision, not code.
5. Noxa's "LP locked" claim ≠ token contract safety — creator can still hold
   supply; holder-concentration gate stays mandatory.

## 5. Deliverable order

| # | Deliverable | Depends on |
|---|---|---|
| 1 | Phase 0 spike notes (API field samples, one manual v3 mint/burn) | — |
| 2 | `internal/venue` extraction, zero behavior change, tests | — |
| 3 | `internal/robinhood` discover+screen+safety, signal-only | 1, 2 |
| 4 | Schema + config + docs updates | 3 |
| 5 | `uni_executor.js` + pipeline `--chain` dispatch, DRY_RUN | 1 |
| 6 | Monitor exits for v3 positions | 5 |
| 7 | Live tiny-size rollout + gate calibration | 3–6 |

## Sources

- [Robinhood newsroom — mainnet launch](https://robinhood.com/us/en/newsroom/robinhood-accelerates-global-expansion-robinhood-chain-mainnet-stock-tokens-agentic-trading/)
- [Arbitrum DAO factsheet — Robinhood Chain mainnet](https://forum.arbitrum.foundation/t/arbitrumdao-factsheet-robinhood-chain-mainnet-launch/31041)
- [Chainlist — chain 4663](https://chainlist.org/chain/4663) · [Robinhood Chain docs](https://docs.robinhood.com/chain/connecting/) · [Blockscout explorer/API](https://robinhoodchain.blockscout.com/api-docs)
- [Uniswap blog — live on Robinhood Chain (v2/v3/v4/X)](https://blog.uniswap.org/robinhood-chain-is-live)
- [Uniswap v4 SDK — position minting](https://docs.uniswap.org/sdk/v4/guides/liquidity/position-minting)
- [GeckoTerminal — Robinhood pools](https://www.geckoterminal.com/robinhood/pools) · [API docs (new_pools, 30 req/min)](https://api.geckoterminal.com/docs/index.html) · [API guide/FAQ](https://apiguide.geckoterminal.com/)
- [DexScreener — Robinhood chain](https://dexscreener.com/robinhood)
- [Noxa Fun launchpad overview (direct-to-v3, 1% tier, LP lock)](https://docs.noxa.fi/launchpad/overview/) · [Bitrue guide](https://www.bitrue.com/blog/noxa-fun-robinhood-chain-guide)
- [hood.fun launch announcement](https://technologymagazine.com/globenewswire/3324698)
- [CryptoSlate — memecoin wave / $150M cat coin](https://cryptoslate.com/robinhood-launched-a-wall-street-layer-2-chain-and-the-market-crowned-a-150m-cat-coin-first/) · [CryptoTimes — active addresses record](https://www.cryptotimes.io/2026/07/09/robinhood-chain-active-addresses-hit-record-high-amid-meme-coin-frenzy/)
- [GoPlus Token Security API](https://gopluslabs.io/en/token-security-api) · [response docs](https://docs.gopluslabs.io/reference/response-details)
- [GMGN — Robinhood Chain live, API coverage](https://x.com/gmgnai/status/2075215360580603990) · [GMGN security page (robinhood)](https://gmgn.ai/security?chain=robinhood)
- [LFJ Liquidity Book primer (EVM DLMM)](https://docs.lfj.gg/lfj-dex/liquidity/liquidity_book-_primer_6893873) · [AEON Protocol on DefiLlama (vAMM + Algebra CL + Liquidity Book pools)](https://defillama.com/protocol/aeon-protocol)
