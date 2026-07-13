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

### Phase 0 — spike (no code merged)
- Verify with curl: GeckoTerminal `new_pools` fields for `robinhood`,
  DexScreener `robinhood` pair schema, GoPlus support for chain 4663
  (fallback: honeypot.is or Blockscout-based heuristics).
- Testnet/small-mainnet manual tx: mint + collect + burn a v3 position via
  script to validate the executor path end-to-end.

### Phase 1 — daemon: discover + screen + signal (dry-run)
- `internal/venue` — small interface: `Discover(ctx, mode) ([]Signal, error)`
  extracted over current meteora flow (thin refactor; meteora keeps its
  behavior verbatim).
- `internal/robinhood/` new package:
  - `discover.go` — GeckoTerminal new_pools poll (respect 30/min budget
    shared across modes).
  - `screen.go` — port `ModeParams` gates: age window, fee/TVL (computed),
    volume, liquidity floor, txn counts, holder count via Blockscout.
  - `safety.go` — GoPlus/honeypot gate (fail-closed on positive detection,
    fail-open on API absence), verified-contract check via Blockscout.
  - `momentum.go` — reuse DexScreener logic with `robinhood` chainId
    (parameterize existing `internal/meteora/momentum.go` instead of copying).
- `internal/config` — `ROBINHOOD_ENABLED`, `ROBINHOOD_RPC_URL`,
  `GECKOTERMINAL_*`, `GOPLUS_*`; per-venue mode toggles.
- Store: dedup key prefixed `rh:` (same Redis, independent TTLs).
- Signal schema: add `"chain": "robinhood"` field + EVM-specific fields
  (pool address, fee tier, token0/1). Update `docs/SIGNAL_SCHEMA.md`.
- Run **signal-only** (webhook/report, no deploy) ≥3 days; journal what the
  gates would pick (reuse GateNarrative pattern).

### Phase 2 — executor: deploy path
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

### Phase 3 — monitor: exits
- Extend `dlmm_monitor.py` with a chain-dispatch layer or add
  `uni_monitor.py` sharing the exit rulebook (trailing TP/SL, fast-out
  velocity exit, phantom-PnL guards). Position state from
  `NonfungiblePositionManager` + pool slot0 via RPC (no subgraph dependency
  in the hot path; Blockscout as fallback enrichment).
- PnL in WETH terms; fee collection cadence decision (collect on exit only,
  v3 fees don't auto-compound).

### Phase 4 — calibrate + harden
- Tune gates from Phase 1–2 journals (expect different scarcity profile than
  Solana casual mode).
- Rate-limit management: GeckoTerminal 30/min public — consider CoinGecko
  paid onchain tier if poll cadence needs it.
- Optional later: v4 pools (hooks introduce per-pool custom logic = new rug
  vector — needs its own safety screen), UniswapX, tokenized-stock LP mode
  (different regime: low vol, fee-capture only).

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
