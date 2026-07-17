// Uniswap v4 position executor for Robinhood Chain (chain ID 4663) — the v4
// sibling of uni_executor.js. The v3 executor stays untouched (see
// docs/ROBINHOOD_CHAIN_PLAN.md Phase 7): v4 has no per-pool contract, so every
// assumption the v3 script bakes in (pool address, NPM, SwapRouter02) breaks.
// Here a pool is a bytes32 poolId inside the singleton PoolManager, positions
// are PositionManager (posm) ERC-721s driven through modifyLiquidities' Actions
// encoding, swaps go through UniversalRouter's V4_SWAP command quoted by
// V4Quoter, ERC-20 pulls are mediated by Permit2, and the quote side can be
// WETH, USDG (6 decimals!) or native ETH (currency0 == address(0)).
//
// Commands (mirroring uni_executor.js; --pool takes the bytes32 poolId):
//   node uni_v4_executor.js address
//   node uni_v4_executor.js balance                    # ETH + WETH + USDG
//   node uni_v4_executor.js quote --pool 0x<32B>       # pool state
//   node uni_v4_executor.js deploy --pool 0x<32B> --amount 0.01 [--quote 0x..] [--strategy balanced_tight|weth_below] [--range-pct 10] [--slippage 5]
//   node uni_v4_executor.js positions                  # journal-known live positions
//   node uni_v4_executor.js state --id 123
//   node uni_v4_executor.js collect --id 123
//   node uni_v4_executor.js close --id 123 [--no-swap-out]
//   node uni_v4_executor.js sweep [--token 0x..]
//   node uni_v4_executor.js unwrap [--amount 0.001]
//
// --amount is denominated in QUOTE units (WETH/ETH: 18 decimals, USDG: 6) —
// the deploy caller sizes per quote asset. viem only, no @uniswap/* SDKs: the
// Actions/tick math needed is small and auditable, same policy as the v3
// script. Env contract identical to uni_executor.js (EVM_PRIVATE_KEY,
// ROBINHOOD_RPC_URL, DRY_RUN, UNI_GAS_* / UNI_EXIT_SLIPPAGE_PCT /
// UNI_STRANDED_MAX_BACKOFF_S knobs).

const bs58 = require("bs58");
const dotenv = require("dotenv");
const fs = require("fs");
const path = require("path");
const {
  createPublicClient, createWalletClient, http, parseEther, formatEther,
  parseUnits, formatUnits, getAddress, erc20Abi, parseAbi,
  encodeAbiParameters, keccak256,
} = require("viem");
const { privateKeyToAccount } = require("viem/accounts");

const SCRIPT_DIR = path.dirname(path.isAbsolute(process.argv[1]) ? process.argv[1] : path.resolve(process.argv[1]));
const PROFILE_DIR = path.dirname(path.dirname(path.dirname(SCRIPT_DIR)));
const profileEnvPath = path.join(PROFILE_DIR, ".env");
if (fs.existsSync(profileEnvPath)) dotenv.config({ path: profileEnvPath });

const RPC_URL = process.env.ROBINHOOD_RPC_URL || "https://rpc.mainnet.chain.robinhood.com";
const DRY_RUN = String(process.env.DRY_RUN || "").toLowerCase() === "true";

// Uniswap v4 deployment on Robinhood Chain. Verified on-chain 2026-07-14
// (docs/ROBINHOOD_CHAIN_PLAN.md "v4 + USDG" research: bytecode present, posm
// poolManager() and stateView poolManager() both point at the PoolManager).
const CHAIN_ID = 4663;
const POSM = getAddress("0x58daec3116aae6d93017baaea7749052e8a04fa7");
const STATE_VIEW = getAddress("0xf3334192d15450cdd385c8b70e03f9a6bd9e673b");
const QUOTER = getAddress("0x8dc178efb8111bb0973dd9d722ebeff267c98f94");
const UROUTER = getAddress("0x8876789976decbfcbbbe364623c63652db8c0904");
const PERMIT2 = getAddress("0x000000000022D473030F116dDEE9F6B43aC78BA3");
const WETH = getAddress("0x0bd7d308f8e1639fab988df18a8011f41eacad73");
const USDG = getAddress("0x5fc5360d0400a0fd4f2af552add042d716f1d168");
const ZERO = "0x0000000000000000000000000000000000000000";

// Quote-side whitelist, mirroring internal/robinhood/types.go quoteAssets.
// Native ETH is currency0 == address(0) in a v4 PoolKey (sorts first).
const QUOTES = {
  [ZERO]: { symbol: "ETH", decimals: 18, native: true },
  [WETH.toLowerCase()]: { symbol: "WETH", decimals: 18, native: false },
  [USDG.toLowerCase()]: { symbol: "USDG", decimals: 6, native: false },
};

// v4-periphery Actions bytes (src/libraries/Actions.sol, re-verified upstream
// 2026-07-14) + the UniversalRouter V4_SWAP command byte.
const A = {
  DECREASE_LIQUIDITY: 0x01,
  MINT_POSITION: 0x02,
  BURN_POSITION: 0x03,
  SWAP_EXACT_IN_SINGLE: 0x06,
  SETTLE_ALL: 0x0c,
  SETTLE_PAIR: 0x0d,
  TAKE_ALL: 0x0f,
  TAKE_PAIR: 0x11,
  SWEEP: 0x14,
};
const CMD_V4_SWAP = "0x10";

const EXIT_SLIPPAGE_PCT = parseFloat(process.env.UNI_EXIT_SLIPPAGE_PCT || "15");
const GAS_FLOOR_WEI = parseEther(process.env.UNI_GAS_FLOOR_ETH || "0.0003");
const GAS_TARGET_WEI = parseEther(process.env.UNI_GAS_TARGET_ETH || "0.0008");
const STRANDED_MAX_BACKOFF_S = parseInt(process.env.UNI_STRANDED_MAX_BACKOFF_S || "3600", 10);

// Sibling journals to the v3 executor's, deliberately SEPARATE files: NPM and
// posm are two independent ERC-721 series whose tokenIds both start at 1, so a
// shared journal would collide keys and let a v4 entry poison a v3 position's
// cost basis (or vice versa). Same line format, so uni_monitor.py reads both.
const POS_JOURNAL = path.join(PROFILE_DIR, "memories", "uni_v4_positions.jsonl");
const STRANDED_JOURNAL = path.join(PROFILE_DIR, "memories", "uni_v4_stranded.jsonl");

const chain = {
  id: CHAIN_ID,
  name: "Robinhood Chain",
  nativeCurrency: { name: "Ether", symbol: "ETH", decimals: 18 },
  rpcUrls: { default: { http: [RPC_URL] } },
};

// PoolKey ABI component, reused by every encoder that embeds one.
const POOL_KEY_ABI = {
  type: "tuple",
  components: [
    { name: "currency0", type: "address" },
    { name: "currency1", type: "address" },
    { name: "fee", type: "uint24" },
    { name: "tickSpacing", type: "int24" },
    { name: "hooks", type: "address" },
  ],
};

const posmAbi = parseAbi([
  "function modifyLiquidities(bytes unlockData, uint256 deadline) payable",
  "function nextTokenId() view returns (uint256)",
  "function getPositionLiquidity(uint256 tokenId) view returns (uint128)",
  "function getPoolAndPositionInfo(uint256 tokenId) view returns ((address currency0, address currency1, uint24 fee, int24 tickSpacing, address hooks), uint256 info)",
  "function poolKeys(bytes25 poolId) view returns (address currency0, address currency1, uint24 fee, int24 tickSpacing, address hooks)",
  "function ownerOf(uint256 tokenId) view returns (address)",
]);

const stateViewAbi = parseAbi([
  "function getSlot0(bytes32 poolId) view returns (uint160 sqrtPriceX96, int24 tick, uint24 protocolFee, uint24 lpFee)",
  "function getLiquidity(bytes32 poolId) view returns (uint128)",
  "function getPositionInfo(bytes32 poolId, address owner, int24 tickLower, int24 tickUpper, bytes32 salt) view returns (uint128 liquidity, uint256 feeGrowthInside0LastX128, uint256 feeGrowthInside1LastX128)",
  "function getFeeGrowthInside(bytes32 poolId, int24 tickLower, int24 tickUpper) view returns (uint256 feeGrowthInside0X128, uint256 feeGrowthInside1X128)",
]);

const quoterAbi = parseAbi([
  "function quoteExactInputSingle(((address currency0, address currency1, uint24 fee, int24 tickSpacing, address hooks) poolKey, bool zeroForOne, uint128 exactAmount, bytes hookData)) returns (uint256 amountOut, uint256 gasEstimate)",
]);

const urouterAbi = parseAbi([
  "function execute(bytes commands, bytes[] inputs, uint256 deadline) payable",
]);

const permit2Abi = parseAbi([
  "function approve(address token, address spender, uint160 amount, uint48 expiration)",
  "function allowance(address owner, address token, address spender) view returns (uint160 amount, uint48 expiration, uint48 nonce)",
]);

const wethAbi = parseAbi([
  "function deposit() payable",
  "function withdraw(uint256 wad)",
]);

// ---------------------------------------------------------------------------
// Tick math — the minimal BigInt port of v4-core's TickMath/LiquidityAmounts.
// sqrtPriceX96 semantics are identical to v3, so the constants are the
// canonical ones and valueInQuote below is the same decimals-cancel trick the
// v3 executor documents on valueInWeth.

const Q96 = 1n << 96n;
const MAX_UINT256 = (1n << 256n) - 1n;

// getSqrtRatioAtTick: exact port of TickMath.getSqrtRatioAtTick (Q64.96).
function getSqrtRatioAtTick(tick) {
  const absTick = tick < 0 ? -tick : tick;
  if (absTick > 887272) throw new Error(`tick ${tick} out of range`);
  let ratio = (absTick & 0x1) !== 0
    ? 0xfffcb933bd6fad37aa2d162d1a594001n
    : 0x100000000000000000000000000000000n;
  if (absTick & 0x2) ratio = (ratio * 0xfff97272373d413259a46990580e213an) >> 128n;
  if (absTick & 0x4) ratio = (ratio * 0xfff2e50f5f656932ef12357cf3c7fdccn) >> 128n;
  if (absTick & 0x8) ratio = (ratio * 0xffe5caca7e10e4e61c3624eaa0941cd0n) >> 128n;
  if (absTick & 0x10) ratio = (ratio * 0xffcb9843d60f6159c9db58835c926644n) >> 128n;
  if (absTick & 0x20) ratio = (ratio * 0xff973b41fa98c081472e6896dfb254c0n) >> 128n;
  if (absTick & 0x40) ratio = (ratio * 0xff2ea16466c96a3843ec78b326b52861n) >> 128n;
  if (absTick & 0x80) ratio = (ratio * 0xfe5dee046a99a2a811c461f1969c3053n) >> 128n;
  if (absTick & 0x100) ratio = (ratio * 0xfcbe86c7900a88aedcffc83b479aa3a4n) >> 128n;
  if (absTick & 0x200) ratio = (ratio * 0xf987a7253ac413176f2b074cf7815e54n) >> 128n;
  if (absTick & 0x400) ratio = (ratio * 0xf3392b0822b70005940c7a398e4b70f3n) >> 128n;
  if (absTick & 0x800) ratio = (ratio * 0xe7159475a2c29b7443b29c7fa6e889d9n) >> 128n;
  if (absTick & 0x1000) ratio = (ratio * 0xd097f3bdfd2022b8845ad8f792aa5825n) >> 128n;
  if (absTick & 0x2000) ratio = (ratio * 0xa9f746462d870fdf8a65dc1f90e061e5n) >> 128n;
  if (absTick & 0x4000) ratio = (ratio * 0x70d869a156d2a1b890bb3df62baf32f7n) >> 128n;
  if (absTick & 0x8000) ratio = (ratio * 0x31be135f97d08fd981231505542fcfa6n) >> 128n;
  if (absTick & 0x10000) ratio = (ratio * 0x9aa508b5b7a84e1c677de54f3e99bc9n) >> 128n;
  if (absTick & 0x20000) ratio = (ratio * 0x5d6af8dedb81196699c329225ee604n) >> 128n;
  if (absTick & 0x40000) ratio = (ratio * 0x2216e584f5fa1ea926041bedfe98n) >> 128n;
  if (absTick & 0x80000) ratio = (ratio * 0x48a170391f7dc42444e8fa2n) >> 128n;
  if (tick > 0) ratio = MAX_UINT256 / ratio;
  // Q128.128 -> Q64.96, rounding up (matches the Solidity cast).
  return (ratio >> 32n) + ((ratio & 0xffffffffn) > 0n ? 1n : 0n);
}

// liquidityForAmounts: LiquidityAmounts.getLiquidityForAmounts. Floor division
// throughout — under-asking by a wei is safe, over-asking reverts the mint.
function liquidityForAmount0(sqrtA, sqrtB, amount0) {
  if (sqrtA > sqrtB) [sqrtA, sqrtB] = [sqrtB, sqrtA];
  const intermediate = (sqrtA * sqrtB) / Q96;
  return (amount0 * intermediate) / (sqrtB - sqrtA);
}
function liquidityForAmount1(sqrtA, sqrtB, amount1) {
  if (sqrtA > sqrtB) [sqrtA, sqrtB] = [sqrtB, sqrtA];
  return (amount1 * Q96) / (sqrtB - sqrtA);
}
function liquidityForAmounts(sqrtP, sqrtA, sqrtB, amount0, amount1) {
  if (sqrtA > sqrtB) [sqrtA, sqrtB] = [sqrtB, sqrtA];
  if (sqrtP <= sqrtA) return liquidityForAmount0(sqrtA, sqrtB, amount0);
  if (sqrtP < sqrtB) {
    const l0 = liquidityForAmount0(sqrtP, sqrtB, amount0);
    const l1 = liquidityForAmount1(sqrtA, sqrtP, amount1);
    return l0 < l1 ? l0 : l1;
  }
  return liquidityForAmount1(sqrtA, sqrtB, amount1);
}

// amountsForLiquidity: LiquidityAmounts.getAmountsForLiquidity (floor — a
// conservative valuation, same spirit as the v3 monitor's fee undercount).
function amountsForLiquidity(sqrtP, sqrtA, sqrtB, liquidity) {
  if (sqrtA > sqrtB) [sqrtA, sqrtB] = [sqrtB, sqrtA];
  let amount0 = 0n, amount1 = 0n;
  if (sqrtP <= sqrtA) {
    amount0 = ((liquidity << 96n) * (sqrtB - sqrtA)) / sqrtB / sqrtA;
  } else if (sqrtP < sqrtB) {
    amount0 = ((liquidity << 96n) * (sqrtB - sqrtP)) / sqrtB / sqrtP;
    amount1 = (liquidity * (sqrtP - sqrtA)) / Q96;
  } else {
    amount1 = (liquidity * (sqrtB - sqrtA)) / Q96;
  }
  return [amount0, amount1];
}

// valueInQuote prices raw (amount0, amount1) in raw quote-token units using
// sqrtPriceX96 — decimals cancel exactly as in the v3 executor's valueInWeth.
function valueInQuote(amount0, amount1, sqrtPriceX96, quoteIs0) {
  const Q192 = 1n << 192n;
  const p2 = sqrtPriceX96 * sqrtPriceX96;
  if (quoteIs0) return amount0 + (amount1 * Q192) / p2;
  return amount1 + (amount0 * p2) / Q192;
}

function pctToTicks(pct) { return Math.round(Math.log(1 + pct / 100) / Math.log(1.0001)); }
function roundToSpacing(tick, spacing, up) {
  const q = tick / spacing;
  return (up ? Math.ceil(q) : Math.floor(q)) * spacing;
}

// ---------------------------------------------------------------------------
// Actions encoding: modifyLiquidities takes abi.encode(bytes actions,
// bytes[] params); UniversalRouter's V4_SWAP input is the same shape.

function encodeActions(pairs) {
  const actions = "0x" + pairs.map(([a]) => a.toString(16).padStart(2, "0")).join("");
  const params = pairs.map(([, p]) => p);
  return encodeAbiParameters(
    [{ type: "bytes" }, { type: "bytes[]" }],
    [actions, params],
  );
}

const enc = encodeAbiParameters;
const mintParams = (key, tickLower, tickUpper, liquidity, amount0Max, amount1Max, owner) =>
  enc([POOL_KEY_ABI, { type: "int24" }, { type: "int24" }, { type: "uint256" },
    { type: "uint128" }, { type: "uint128" }, { type: "address" }, { type: "bytes" }],
  [key, tickLower, tickUpper, liquidity, amount0Max, amount1Max, owner, "0x"]);
const decreaseParams = (tokenId, liquidity) =>
  enc([{ type: "uint256" }, { type: "uint256" }, { type: "uint128" }, { type: "uint128" }, { type: "bytes" }],
    [tokenId, liquidity, 0n, 0n, "0x"]);
const burnParams = (tokenId) =>
  enc([{ type: "uint256" }, { type: "uint128" }, { type: "uint128" }, { type: "bytes" }],
    [tokenId, 0n, 0n, "0x"]);
const settlePairParams = (c0, c1) =>
  enc([{ type: "address" }, { type: "address" }], [c0, c1]);
const takePairParams = (c0, c1, to) =>
  enc([{ type: "address" }, { type: "address" }, { type: "address" }], [c0, c1, to]);
const sweepParams = (currency, to) =>
  enc([{ type: "address" }, { type: "address" }], [currency, to]);
// Robinhood's UniversalRouter is an OLDER v4-periphery build whose
// ExactInputSingleParams still carries sqrtPriceLimitX96 (removed upstream
// before the final release). Encoding the modern 5-field struct reverts with
// empty data; the 6-field layout below simulates clean (verified on-chain
// 2026-07-15). 0 = no limit — this build still translates the sentinel.
// The V4Quoter on this chain is a NEWER build (5-field quote struct), so the
// two deliberately disagree; don't "fix" one to match the other.
const swapExactInSingleParams = (key, zeroForOne, amountIn, minOut) =>
  enc([{
    type: "tuple",
    components: [
      { name: "poolKey", ...POOL_KEY_ABI },
      { name: "zeroForOne", type: "bool" },
      { name: "amountIn", type: "uint128" },
      { name: "amountOutMinimum", type: "uint128" },
      { name: "sqrtPriceLimitX96", type: "uint160" },
      { name: "hookData", type: "bytes" },
    ],
  }], [{ poolKey: key, zeroForOne, amountIn, amountOutMinimum: minOut, sqrtPriceLimitX96: 0n, hookData: "0x" }]);
const settleAllParams = (currency, maxAmount) =>
  enc([{ type: "address" }, { type: "uint256" }], [currency, maxAmount]);
const takeAllParams = (currency, minAmount) =>
  enc([{ type: "address" }, { type: "uint256" }], [currency, minAmount]);

// poolIdOf recomputes keccak256(abi.encode(PoolKey)) — used to verify that a
// resolved key really is the pool the signal named before money touches it.
function poolIdOf(key) {
  return keccak256(enc(
    [{ type: "address" }, { type: "address" }, { type: "uint24" }, { type: "int24" }, { type: "address" }],
    [key.currency0, key.currency1, key.fee, key.tickSpacing, key.hooks],
  ));
}

// unpackInfo splits posm's packed PositionInfo uint256:
// 200 bits truncated poolId | 24 bits tickUpper | 24 bits tickLower | 8 bits hasSubscriber.
function signExtend24(x) { return x >= 0x800000 ? x - 0x1000000 : x; }
function unpackInfo(info) {
  return {
    tickLower: signExtend24(Number((info >> 8n) & 0xffffffn)),
    tickUpper: signExtend24(Number((info >> 32n) & 0xffffffn)),
  };
}

// ---------------------------------------------------------------------------
// Journals + account plumbing — same shapes as uni_executor.js.

function journalEntry(rec) {
  try {
    fs.mkdirSync(path.dirname(POS_JOURNAL), { recursive: true });
    fs.appendFileSync(POS_JOURNAL, JSON.stringify(rec) + "\n");
  } catch (e) {
    console.error(`warn: could not journal position entry: ${e.message}`);
  }
}

function journalStranded(rec) {
  try {
    fs.mkdirSync(path.dirname(STRANDED_JOURNAL), { recursive: true });
    // ts/timestamp go LAST so a re-journaled bag (spread from its previous
    // line) records when THIS line was written — same rationale as v3.
    fs.appendFileSync(STRANDED_JOURNAL, JSON.stringify({
      ...rec,
      ts: Math.floor(Date.now() / 1000),
      timestamp: new Date().toISOString().replace(/\.\d+Z$/, "Z"),
    }) + "\n");
  } catch (e) {
    console.error(`warn: could not journal stranded bag: ${e.message}`);
  }
}

function retryDelay(attempts) {
  return Math.min(60 * 2 ** Math.max(0, attempts - 1), STRANDED_MAX_BACKOFF_S);
}

function openStranded() {
  const latest = new Map();
  try {
    for (const line of fs.readFileSync(STRANDED_JOURNAL, "utf8").trim().split("\n")) {
      if (!line) continue;
      const r = JSON.parse(line);
      latest.set(getAddress(r.token), r);
    }
  } catch { /* no journal yet */ }
  return [...latest.values()].filter((r) => !r.resolved);
}

function readEntry(tokenId) {
  try {
    const lines = fs.readFileSync(POS_JOURNAL, "utf8").trim().split("\n");
    for (let i = lines.length - 1; i >= 0; i--) {
      if (!lines[i]) continue;
      const r = JSON.parse(lines[i]);
      if (String(r.tokenId) === String(tokenId)) return r;
    }
  } catch { /* no journal yet */ }
  return null;
}

// journalTokenIds returns every distinct tokenId ever journaled — the v4
// position enumeration source. posm's ERC-721 is NOT enumerable (no
// tokenOfOwnerByIndex, unlike v3's NPM), so the journal of our own mints is
// the only index; cmdPositions filters it by live on-chain ownership.
function journalTokenIds() {
  const ids = new Set();
  try {
    for (const line of fs.readFileSync(POS_JOURNAL, "utf8").trim().split("\n")) {
      if (!line) continue;
      const r = JSON.parse(line);
      if (r.tokenId) ids.add(String(r.tokenId));
    }
  } catch { /* no journal yet */ }
  return [...ids];
}

function getAccount() {
  const raw = (process.env.EVM_PRIVATE_KEY || "").trim();
  if (!raw) throw new Error("EVM_PRIVATE_KEY not set in profile .env");
  if (raw.startsWith("0x") && raw.length === 66) return privateKeyToAccount(raw);
  // Base58 Solana secret key reuse — same stopgap as the v3 executor.
  const decoded = Buffer.from(bs58.decode(raw));
  if (decoded.length !== 64 && decoded.length !== 32) {
    throw new Error(`EVM_PRIVATE_KEY: expected 0x-hex(32B) or base58 Solana key, got ${decoded.length} bytes`);
  }
  return privateKeyToAccount(`0x${decoded.subarray(0, 32).toString("hex")}`);
}

function arg(name, def) {
  const i = process.argv.indexOf(`--${name}`);
  if (i === -1 || i + 1 >= process.argv.length) return def;
  return process.argv[i + 1];
}
function hasFlag(name) { return process.argv.includes(`--${name}`); }

const pub = createPublicClient({ chain, transport: http(RPC_URL) });

async function send(wallet, req, label) {
  if (DRY_RUN) {
    console.log(`[dry-run] would send: ${label}`);
    return { hash: "DRY_RUN_TX_HASH", rcpt: null };
  }
  // Same 30% gas pad as the v3 executor: pool state can move between estimate
  // and inclusion; a failed estimate falls through to writeContract's own
  // simulation, which surfaces the actual revert reason.
  const gas = await pub
    .estimateContractGas({ ...req, account: req.account ?? wallet.account })
    .then((g) => (g * 130n) / 100n)
    .catch(() => undefined);
  const hash = await wallet.writeContract(gas ? { ...req, gas } : req);
  const rcpt = await pub.waitForTransactionReceipt({ hash, timeout: 120_000 });
  if (rcpt.status !== "success") throw new Error(`${label} reverted: ${hash}`);
  console.log(`${label}: ${hash}`);
  return { hash, rcpt };
}

async function ensureGas(wallet, account) {
  if (DRY_RUN) return null;
  const eth = await pub.getBalance({ address: account.address });
  if (eth >= GAS_FLOOR_WEI) return null;
  const weth = await pub.readContract({ address: WETH, abi: erc20Abi, functionName: "balanceOf", args: [account.address] });
  if (weth === 0n) {
    console.error(`warn: gas low (${formatEther(eth)} ETH) and no WETH to unwrap — fund this wallet`);
    return { low: true, eth: formatEther(eth), unwrapped: "0", reason: "no WETH to unwrap" };
  }
  const target = GAS_TARGET_WEI > GAS_FLOOR_WEI ? GAS_TARGET_WEI : GAS_FLOOR_WEI;
  const need = target - eth;
  if (need <= 0n) return null;
  const amount = need < weth ? need : weth;
  try {
    const { hash } = await send(wallet, {
      address: WETH, abi: wethAbi, functionName: "withdraw", args: [amount],
      account: wallet.account, chain,
    }, `unwrap ${formatEther(amount)} WETH -> ETH (gas top-up, had ${formatEther(eth)})`);
    return { low: false, eth_before: formatEther(eth), unwrapped: formatEther(amount), tx: hash };
  } catch (e) {
    console.error(`warn: gas top-up failed: ${e.shortMessage || e.message}`);
    return { low: true, eth: formatEther(eth), unwrapped: "0", reason: e.shortMessage || e.message };
  }
}

// ensurePermit2 lines up the two-hop allowance a v4 pull needs: the wallet
// approves Permit2 on the ERC-20, then authorizes `spender` (posm or the
// UniversalRouter) inside Permit2. Exact amounts, 30-minute expiry — the same
// no-unlimited-allowances policy as the v3 executor, made cheaper by the
// expiry: a stale exact allowance dies on its own, no zeroing tx later.
async function ensurePermit2(wallet, account, token, spender, amount) {
  if (token === ZERO) return; // native ETH settles via msg.value, no approvals
  const erc = await pub.readContract({ address: token, abi: erc20Abi, functionName: "allowance", args: [account.address, PERMIT2] });
  if (erc < amount) {
    await send(wallet, {
      address: token, abi: erc20Abi, functionName: "approve", args: [PERMIT2, amount],
      account: wallet.account, chain,
    }, `approve Permit2 for ${token.slice(0, 10)}`);
  }
  const [p2Amount, p2Exp] = await pub.readContract({
    address: PERMIT2, abi: permit2Abi, functionName: "allowance",
    args: [account.address, token, spender],
  });
  const now = Math.floor(Date.now() / 1000);
  if (p2Amount >= amount && p2Exp > now + 60) return;
  const max160 = (1n << 160n) - 1n;
  const amt = amount > max160 ? max160 : amount;
  await send(wallet, {
    address: PERMIT2, abi: permit2Abi, functionName: "approve",
    args: [token, spender, amt, now + 1800],
    account: wallet.account, chain,
  }, `permit2 ${spender === POSM ? "posm" : "router"} for ${token.slice(0, 10)}`);
}

// resolvePoolKey recovers the full PoolKey for a bytes32 poolId from posm's
// poolKeys(bytes25) registry (populated by the first posm mint into the pool
// — every pool that passed the liquidity screens has LPs, so in practice it
// is always there). The recomputed keccak MUST round-trip to the poolId:
// poolKeys is keyed by a TRUNCATED 25-byte id, and acting on a 200-bit prefix
// match without verifying the full 256 bits would trust a collision.
async function resolvePoolKey(poolId) {
  if (!/^0x[0-9a-fA-F]{64}$/.test(poolId)) throw new Error(`--pool must be a bytes32 v4 poolId, got ${poolId || "(empty)"}`);
  const bytes25 = poolId.slice(0, 52); // 0x + 50 hex chars = 25 bytes
  const [currency0, currency1, fee, tickSpacing, hooks] = await pub.readContract({
    address: POSM, abi: posmAbi, functionName: "poolKeys", args: [bytes25],
  });
  const key = {
    currency0: currency0 === ZERO ? ZERO : getAddress(currency0),
    currency1: getAddress(currency1),
    fee: Number(fee), tickSpacing: Number(tickSpacing), hooks: getAddress(hooks),
  };
  if (key.currency1 === getAddress(ZERO) && key.tickSpacing === 0) {
    throw new Error(`posm has no PoolKey for ${poolId} (no position ever minted through posm) — cannot mint blind`);
  }
  if (poolIdOf(key).toLowerCase() !== poolId.toLowerCase()) {
    throw new Error(`PoolKey verification failed for ${poolId}: truncated-id collision`);
  }
  return key;
}

// quoteSide picks which side of the key is our quote asset. --quote from the
// caller wins (the daemon knows the signal's orientation); otherwise the
// whitelist decides, preferring the ETH-ish side when both qualify (a
// WETH/USDG pool is quoted in ETH terms, like every other position we hold).
function quoteSide(key, explicit) {
  const c0 = key.currency0.toLowerCase(), c1 = key.currency1.toLowerCase();
  if (explicit) {
    const q = explicit.toLowerCase();
    if (q === c0) return 0;
    if (q === c1) return 1;
    throw new Error(`--quote ${explicit} is neither side of the pool`);
  }
  const pref = (a) => (a === ZERO ? 3 : a === WETH.toLowerCase() ? 2 : QUOTES[a] ? 1 : 0);
  const p0 = pref(c0), p1 = pref(c1);
  if (p0 === 0 && p1 === 0) throw new Error("pool has no whitelisted quote side (WETH/USDG/native ETH)");
  return p0 >= p1 ? 0 : 1;
}

async function slot0(poolId) {
  const [sqrtPriceX96, tick] = await pub.readContract({
    address: STATE_VIEW, abi: stateViewAbi, functionName: "getSlot0", args: [poolId],
  });
  return { sqrtPriceX96, tick: Number(tick) };
}

async function tokenMeta(addr) {
  if (addr === ZERO) return { symbol: "ETH", decimals: 18 };
  const [symbol, decimals] = await Promise.all([
    pub.readContract({ address: addr, abi: erc20Abi, functionName: "symbol" }).catch(() => "?"),
    pub.readContract({ address: addr, abi: erc20Abi, functionName: "decimals" }).catch(() => 18),
  ]);
  return { symbol, decimals: Number(decimals) };
}

async function balanceOf(addr, owner) {
  if (addr === ZERO) return pub.getBalance({ address: owner });
  return pub.readContract({ address: addr, abi: erc20Abi, functionName: "balanceOf", args: [owner] });
}

// ensureNative makes sure the wallet holds `amount` native ETH ON TOP of the
// gas target, unwrapping WETH for the shortfall. Native-quoted pools settle
// in raw ETH, but the daemon sizes deploys off the WETH balance — WETH is
// where this wallet keeps capital — so the executor bridges the difference.
async function ensureNative(wallet, account, amount) {
  if (DRY_RUN) return;
  const eth = await pub.getBalance({ address: account.address });
  const want = amount + GAS_TARGET_WEI;
  if (eth >= want) return;
  const need = want - eth;
  const weth = await pub.readContract({ address: WETH, abi: erc20Abi, functionName: "balanceOf", args: [account.address] });
  if (weth < need) throw new Error(`need ${formatEther(need)} more ETH for a native-quote deploy but only ${formatEther(weth)} WETH to unwrap`);
  await send(wallet, {
    address: WETH, abi: wethAbi, functionName: "withdraw", args: [need],
    account: wallet.account, chain,
  }, `unwrap ${formatEther(need)} WETH -> ETH (native-quote deploy)`);
}

// rewrapExcess puts native ETH above the gas target back into WETH after a
// native-quote close/sell paid out in raw ETH — capital lives as WETH so the
// daemon's sizing sees it.
async function rewrapExcess(wallet, account) {
  if (DRY_RUN) return null;
  const eth = await pub.getBalance({ address: account.address });
  if (eth <= GAS_TARGET_WEI * 2n) return null;
  const excess = eth - GAS_TARGET_WEI;
  await send(wallet, {
    address: WETH, abi: wethAbi, functionName: "deposit", value: excess,
    account: wallet.account, chain,
  }, `wrap ${formatEther(excess)} ETH -> WETH (post-close rewrap)`);
  return formatEther(excess);
}

// quoteExactIn asks V4Quoter for the real output of a single-pool exact-in
// swap. Simulation only (the quoter is written to be called off-chain); it
// reverts for exactly the reasons the live swap would — the v4 counterpart of
// the v3 executor simulating SwapRouter02. Returns null on revert.
async function quoteExactIn(key, zeroForOne, amountIn) {
  try {
    const { result } = await pub.simulateContract({
      address: QUOTER, abi: quoterAbi, functionName: "quoteExactInputSingle",
      args: [{ poolKey: key, zeroForOne, exactAmount: amountIn, hookData: "0x" }],
    });
    return result[0];
  } catch {
    return null;
  }
}

// v4Swap runs one exact-in single-pool swap through the UniversalRouter:
// V4_SWAP command wrapping [SWAP_EXACT_IN_SINGLE, SETTLE_ALL, TAKE_ALL].
// The input side gets Permit2-authorized here (or is native, sent as
// msg.value). Throws on revert — callers wrap it when a failure is survivable.
async function v4Swap(wallet, account, key, zeroForOne, amountIn, minOut, label) {
  const cIn = zeroForOne ? key.currency0 : key.currency1;
  const cOut = zeroForOne ? key.currency1 : key.currency0;
  await ensurePermit2(wallet, account, cIn, UROUTER, amountIn);
  const input = encodeActions([
    [A.SWAP_EXACT_IN_SINGLE, swapExactInSingleParams(key, zeroForOne, amountIn, minOut)],
    [A.SETTLE_ALL, settleAllParams(cIn, amountIn)],
    [A.TAKE_ALL, takeAllParams(cOut, minOut)],
  ]);
  const deadline = BigInt(Math.floor(Date.now() / 1000) + 120);
  return send(wallet, {
    address: UROUTER, abi: urouterAbi, functionName: "execute",
    args: [CMD_V4_SWAP, [input], deadline],
    value: cIn === ZERO ? amountIn : 0n,
    account: wallet.account, chain,
  }, label);
}

// sellTokenForQuote unloads the token side back into the quote through the
// SAME pool we LP'd. One deliberate divergence from the v3 exit: v3 walks
// every fee tier via the factory, but v4 has no on-chain pool enumeration —
// there is no factory.getPool to walk — so the journaled PoolKey is the only
// route we can prove exists. A dead pool means a stranded bag for `sweep`,
// which retries on the same backoff schedule as the v3 script.
async function sellTokenForQuote(wallet, account, key, quoteIs0, amount) {
  if (amount <= 0n) return { ok: false, reason: "zero balance" };
  const zeroForOne = !quoteIs0; // token -> quote
  const quoted = await quoteExactIn(key, zeroForOne, amount);
  if (quoted == null) return { ok: false, reason: "quoter revert (pool dead or blacklisted)" };
  if (quoted === 0n) return { ok: false, reason: "quote 0" };
  const minOut = (quoted * BigInt(Math.floor((100 - EXIT_SLIPPAGE_PCT) * 100))) / 10000n;
  try {
    const { hash } = await v4Swap(wallet, account, key, zeroForOne, amount, minOut, `sell token -> quote (~${quoted} raw out)`);
    return { ok: true, amountOut: quoted, tx: hash };
  } catch (e) {
    return { ok: false, reason: (e.shortMessage || e.message || "reverted").split("\n")[0].slice(0, 80) };
  }
}

// ---------------------------------------------------------------------------
// Commands

async function cmdAddress(account) {
  console.log(JSON.stringify({ address: account.address, derivedFrom: process.env.EVM_PRIVATE_KEY?.startsWith("0x") ? "hex" : "solana-seed", chainId: CHAIN_ID }));
}

async function cmdBalance(account) {
  const [eth, weth, usdg] = await Promise.all([
    pub.getBalance({ address: account.address }),
    pub.readContract({ address: WETH, abi: erc20Abi, functionName: "balanceOf", args: [account.address] }),
    pub.readContract({ address: USDG, abi: erc20Abi, functionName: "balanceOf", args: [account.address] }).catch(() => 0n),
  ]);
  console.log(JSON.stringify({
    address: account.address,
    eth: formatEther(eth), weth: formatEther(weth), usdg: formatUnits(usdg, 6),
  }));
}

async function cmdUnwrap(wallet, account) {
  const raw = arg("amount", "");
  if (!raw) {
    const eth = await pub.getBalance({ address: account.address });
    const weth = await pub.readContract({ address: WETH, abi: erc20Abi, functionName: "balanceOf", args: [account.address] });
    const need = GAS_TARGET_WEI > eth ? GAS_TARGET_WEI - eth : 0n;
    const amount = need < weth ? need : weth;
    if (amount === 0n) {
      console.log(JSON.stringify({ success: true, unwrapped: "0", note: "gas reserve already at target", eth: formatEther(eth) }));
      return;
    }
    await send(wallet, { address: WETH, abi: wethAbi, functionName: "withdraw", args: [amount], account: wallet.account, chain }, `unwrap ${formatEther(amount)} WETH`);
    console.log(JSON.stringify({ success: true, unwrapped: formatEther(amount), eth_before: formatEther(eth) }));
    return;
  }
  const amount = parseEther(raw);
  if (amount <= 0n) throw new Error("--amount must be > 0 (WETH)");
  await send(wallet, { address: WETH, abi: wethAbi, functionName: "withdraw", args: [amount], account: wallet.account, chain }, `unwrap ${formatEther(amount)} WETH`);
  console.log(JSON.stringify({ success: true, unwrapped: formatEther(amount) }));
}

async function cmdQuote() {
  const poolId = arg("pool", "");
  const key = await resolvePoolKey(poolId);
  const [st, liq, m0, m1] = await Promise.all([
    slot0(poolId),
    pub.readContract({ address: STATE_VIEW, abi: stateViewAbi, functionName: "getLiquidity", args: [poolId] }),
    tokenMeta(key.currency0),
    tokenMeta(key.currency1),
  ]);
  console.log(JSON.stringify({
    pool: poolId, protocol: "v4",
    currency0: `${m0.symbol} ${key.currency0}`, currency1: `${m1.symbol} ${key.currency1}`,
    fee: key.fee, tickSpacing: key.tickSpacing, hooks: key.hooks,
    tick: st.tick, sqrtPriceX96: st.sqrtPriceX96.toString(), liquidity: liq.toString(),
  }));
}

async function cmdDeploy(wallet, account) {
  const poolId = arg("pool", "");
  const strategy = arg("strategy", "balanced_tight");
  const rangePct = parseFloat(arg("range-pct", "10"));
  const slippagePct = parseFloat(arg("slippage", "5"));

  const key = await resolvePoolKey(poolId);
  // Belt-and-braces re-check of the screen's v4 hard gate: a hook owns the
  // pool's withdrawal path, and this executor's close sequence assumes nobody
  // can veto a burn. Never mint into a hooked pool, whatever the caller says.
  if (key.hooks !== getAddress(ZERO)) throw new Error(`pool has a hook (${key.hooks}) — refusing to LP`);

  const qSide = quoteSide(key, arg("quote", ""));
  const quoteAddr = qSide === 0 ? key.currency0 : key.currency1;
  const tokenAddr = qSide === 0 ? key.currency1 : key.currency0;
  const q = QUOTES[quoteAddr.toLowerCase()];
  if (!q) throw new Error(`quote side ${quoteAddr} not in whitelist`);
  const quoteIs0 = qSide === 0;

  const amountQuote = parseUnits(arg("amount", "0"), q.decimals);
  if (amountQuote <= 0n) throw new Error(`--amount required (${q.symbol} units)`);

  await ensureGas(wallet, account);
  if (q.native) await ensureNative(wallet, account, amountQuote);

  let st = await slot0(poolId);
  const spacing = key.tickSpacing;
  const entryTick = st.tick;
  const bandTicks = Math.max(pctToTicks(rangePct), spacing);

  let amount0 = 0n, amount1 = 0n, swapped = 0n, tokenBal = 0n;

  if (strategy === "balanced_tight") {
    // Swap first, band around the post-swap tick — same staleness rationale
    // as the v3 executor (its #111130 post-mortem comment): our own buy plus
    // in-flight market moves make the pre-swap tick stale by mint time.
    const half = amountQuote / 2n;
    const quoted = await quoteExactIn(key, quoteIs0, half);
    if (quoted == null || quoted === 0n) throw new Error("entry swap quote failed — pool illiquid?");
    const minOut = (quoted * BigInt(Math.floor((100 - slippagePct) * 100))) / 10000n;
    const balBefore = DRY_RUN ? 0n : await balanceOf(tokenAddr, account.address);
    await v4Swap(wallet, account, key, quoteIs0, half, minOut, `swap ${formatUnits(half, q.decimals)} ${q.symbol} -> token`);
    swapped = half;

    if (!DRY_RUN) st = await slot0(poolId);
    const movedPct = (Math.pow(1.0001, st.tick - entryTick) - 1) * 100;
    console.log(`entry impact: tick ${entryTick} -> ${st.tick} (${movedPct >= 0 ? "+" : ""}${movedPct.toFixed(2)}% price) — band +/-${bandTicks} ticks around ${st.tick}`);

    // Only the tokens THIS swap bought are inventory — a leftover bag of the
    // same token from an earlier stranded exit is already written off.
    const balAfter = DRY_RUN ? quoted : await balanceOf(tokenAddr, account.address);
    tokenBal = balAfter - balBefore;
    if (quoteIs0) { amount0 = amountQuote - half; amount1 = tokenBal; }
    else { amount0 = tokenBal; amount1 = amountQuote - half; }
  } else if (strategy === "weth_below") {
    // One-sided quote band adjacent to the tick (name kept from the v3
    // executor for config compatibility; here it means "quote below").
    if (quoteIs0) amount0 = amountQuote; else amount1 = amountQuote;
  } else {
    throw new Error(`unknown strategy ${strategy}`);
  }

  // Permit2 for every ERC-20 side posm must pull — done BEFORE the price is
  // sampled for the liquidity math. Approvals are on-chain txs; on a
  // 100ms-block chain the pool trades right through them, and a mint sized
  // off a pre-approval price arrives stale (live failure 2026-07-15:
  // MaximumAmountExceeded on CASHCAT, a $1.4M/day pool).
  if (key.currency0 !== ZERO && amount0 > 0n) await ensurePermit2(wallet, account, key.currency0, POSM, amount0);
  if (amount1 > 0n) await ensurePermit2(wallet, account, key.currency1, POSM, amount1);

  const nativeValue = key.currency0 === ZERO ? amount0 : 0n;

  // buildMint (re)derives ticks + liquidity from the CURRENT st sample so the
  // mint-retry below can reprice after a failed attempt.
  //
  // amountMax = exactly what we hold for each side. posm mints EXACT
  // liquidity, so the v3 silent-half-fill failure cannot happen here; the
  // exposed failure is overdraw (tick moved, this liquidity now needs more
  // than we offered), and these caps turn that into a revert the unwind
  // below can put back — the v4 mirror image of the v3 MIN_FILL guard.
  //
  // The 0.3% liquidity shave covers the two ways "exactly what we hold" can
  // still overdraw: posm re-derives required amounts with CEIL rounding
  // (ours floor), and the price drifts in the ~1 block between this sample
  // and the mint landing. Dust left behind is noise next to the swap fee.
  const buildMint = () => {
    let tickLower, tickUpper;
    if (strategy === "balanced_tight") {
      tickLower = roundToSpacing(st.tick - bandTicks, spacing, false);
      tickUpper = roundToSpacing(st.tick + bandTicks, spacing, true);
    } else if (quoteIs0) {
      // currency0 inventory is consumed as the tick RISES: band above.
      tickLower = roundToSpacing(st.tick + spacing, spacing, true);
      tickUpper = roundToSpacing(st.tick + spacing + 2 * bandTicks, spacing, true);
    } else {
      tickUpper = roundToSpacing(st.tick - spacing, spacing, false);
      tickLower = roundToSpacing(st.tick - spacing - 2 * bandTicks, spacing, false);
    }
    const sqrtA = getSqrtRatioAtTick(tickLower);
    const sqrtB = getSqrtRatioAtTick(tickUpper);
    const liquidity = liquidityForAmounts(st.sqrtPriceX96, sqrtA, sqrtB, amount0, amount1) * 997n / 1000n;
    if (liquidity <= 0n) throw new Error(`computed 0 liquidity for [${tickLower},${tickUpper}] at tick ${st.tick} — range/inventory mismatch`);
    const mint = mintParams(key, tickLower, tickUpper, liquidity, amount0, amount1, account.address);
    const pairs = [[A.MINT_POSITION, mint], [A.SETTLE_PAIR, settlePairParams(key.currency0, key.currency1)]];
    if (nativeValue > 0n) pairs.push([A.SWEEP, sweepParams(ZERO, account.address)]);
    // sqrtA/sqrtB/liquidity ride along: the post-mint cost-basis valuation
    // needs the numbers of the attempt that actually landed.
    return { tickLower, tickUpper, sqrtA, sqrtB, liquidity, unlockData: encodeActions(pairs) };
  };
  let { tickLower, tickUpper, sqrtA, sqrtB, liquidity, unlockData } = buildMint();

  if (DRY_RUN) {
    console.log(`🧪 DRY RUN DEPLOY pool=${poolId} strategy=${strategy} ticks=[${tickLower},${tickUpper}] amount=${formatUnits(amountQuote, q.decimals)} ${q.symbol}`);
    console.log(JSON.stringify({ success: true, dryRun: true, pool: poolId, protocol: "v4", strategy, tickLower, tickUpper, quote: q.symbol }));
    return;
  }

  const preId = await pub.readContract({ address: POSM, abi: posmAbi, functionName: "nextTokenId" });
  let hash, rcpt;
  try {
    const mintOnce = () => wallet.writeContract({
      address: POSM, abi: posmAbi, functionName: "modifyLiquidities",
      args: [unlockData, BigInt(Math.floor(Date.now() / 1000) + 120)],
      value: nativeValue, account: wallet.account, chain,
    });
    try {
      hash = await mintOnce();
    } catch (e1) {
      // Exact-liquidity v4 mints revert on ANY adverse move between the
      // price sample and the mint simulation (no v3-style partial fill).
      // One retry at the fresh price; a second failure hits the unwind.
      const r1 = (e1.shortMessage || e1.message || "").split("\n").slice(0, 2).join(" ").trim();
      console.error(`mint attempt 1 failed (${r1}) — repricing and retrying`);
      st = await slot0(poolId);
      ({ tickLower, tickUpper, sqrtA, sqrtB, liquidity, unlockData } = buildMint());
      hash = await mintOnce();
    }
    rcpt = await pub.waitForTransactionReceipt({ hash, timeout: 120_000 });
    if (rcpt.status !== "success") throw new Error(`mint reverted: ${hash}`);
  } catch (e) {
    // The mint never landed: no position exists, only the swap leg is at
    // risk. Put it back into the quote or hand it to the stranded journal.
    // Keep TWO lines of the error: viem puts the revert signature/selector on
    // the line after "reverted with the following signature:" — truncating to
    // one line threw away the only diagnosable bit.
    const reason = (e.shortMessage || e.message || "mint failed").split("\n").slice(0, 2).join(" ").trim();
    console.error(`mint failed (no position opened): ${reason}`);
    let refund = null;
    if (swapped > 0n && tokenBal > 0n) {
      const sym = (await tokenMeta(tokenAddr)).symbol;
      const r = await sellTokenForQuote(wallet, account, key, quoteIs0, tokenBal);
      if (r.ok) {
        refund = { symbol: sym, quote_out: formatUnits(r.amountOut, q.decimals), tx: r.tx };
        console.log(`refunded swap leg: sold ${sym} back for ${refund.quote_out} ${q.symbol}`);
      } else {
        refund = { symbol: sym, failed: r.reason };
        journalStranded({
          tokenId: null, token: tokenAddr, symbol: sym, amount: tokenBal.toString(),
          key, quote: quoteAddr, reason: r.reason, resolved: false,
          attempts: 1, next_try: Math.floor(Date.now() / 1000) + retryDelay(1),
        });
        console.error(`warn: could not refund ${sym} swap leg (${r.reason}) — queued for sweep`);
      }
    }
    const refundNote = refund
      ? (refund.quote_out ? `, refunded ${refund.quote_out} ${q.symbol}` : `, ${refund.symbol} REFUND FAILED (${refund.failed}) — queued for sweep`)
      : "";
    console.log(`❌ DEPLOY FAILED (no position opened): ${reason}${refundNote}`);
    console.log(JSON.stringify({ success: false, error: `mint failed: ${reason}`, pool: poolId, protocol: "v4", strategy, tickLower, tickUpper, refund }));
    return;
  }

  // tokenId: the ERC-721 Transfer(0x0 -> us) log from THIS tx is exact; the
  // nextTokenId read before the mint is the deterministic fallback (posm
  // allocates ids sequentially, and ownerOf confirms it took ours).
  let tokenId;
  const acct = account.address.toLowerCase();
  const xfer = rcpt.logs.find((l) =>
    l.address.toLowerCase() === POSM.toLowerCase() &&
    l.topics.length === 4 &&
    BigInt(l.topics[1]) === 0n &&
    `0x${l.topics[2].slice(-40)}`.toLowerCase() === acct);
  if (xfer) {
    tokenId = BigInt(xfer.topics[3]).toString();
  } else {
    const owner = await pub.readContract({ address: POSM, abi: posmAbi, functionName: "ownerOf", args: [preId] }).catch(() => null);
    if (owner && owner.toLowerCase() === acct) {
      tokenId = preId.toString();
    } else {
      // Mint landed, funds are committed — surface the tx for a hand journal
      // rather than write an unmanageable position silently (v3 policy).
      console.error(`ERROR: mint ${hash} succeeded but tokenId unresolved`);
      console.log(JSON.stringify({ success: false, error: "tokenId unresolved", pool: poolId, protocol: "v4", tx: hash }));
      return;
    }
  }

  // Cost basis: value the minted liquidity at a fresh slot0 — deterministic
  // from (liquidity, range, price), the v4 analog of pricing the v3 mint's
  // IncreaseLiquidity amounts. Refunded/idle leftovers stay in the wallet and
  // belong to no position's PnL (the instant-stop-loss lesson from v3).
  const stAfter = await slot0(poolId);
  const [used0, used1] = amountsForLiquidity(stAfter.sqrtPriceX96, sqrtA, sqrtB, liquidity);
  const entryQuoteRaw = valueInQuote(used0, used1, stAfter.sqrtPriceX96, quoteIs0);
  const idle = amountQuote - entryQuoteRaw;
  journalEntry({
    tokenId, pool: poolId, protocol: "v4", key,
    quote: quoteAddr, quoteSymbol: q.symbol, quoteDecimals: q.decimals,
    tickLower, tickUpper, strategy,
    quoteIn: formatUnits(entryQuoteRaw, q.decimals),
    committedQuote: formatUnits(amountQuote, q.decimals),
    liquidity: liquidity.toString(),
    ts: Math.floor(Date.now() / 1000),
  });
  const inRange = stAfter.tick >= tickLower && stAfter.tick < tickUpper;
  console.log(`position value ${formatUnits(entryQuoteRaw, q.decimals)} ${q.symbol} of ${formatUnits(amountQuote, q.decimals)} committed `
    + `(${formatUnits(idle > 0n ? idle : 0n, q.decimals)} left in wallet), ${inRange ? "IN range" : "OUT OF range"} at tick ${stAfter.tick}`);
  console.log(`🚀 DEPLOYED pool=${poolId} strategy=${strategy} position=${tokenId} tx=${hash}`);
  console.log(JSON.stringify({
    success: true, pool: poolId, protocol: "v4", strategy, tokenId, tickLower, tickUpper, tx: hash,
    quote: q.symbol, quoteIn: formatUnits(entryQuoteRaw, q.decimals), committedQuote: formatUnits(amountQuote, q.decimals), inRange,
  }));
}

// positionSnapshot prices one v4 position: principal from tick math plus
// pending fees from the fee-growth deltas. StateView recomputes the
// up-to-date inside growth, so unlike the v3 state read this DOES count
// live-accrued fees.
async function positionSnapshot(id) {
  const [keyRaw, infoPacked] = await pub.readContract({
    address: POSM, abi: posmAbi, functionName: "getPoolAndPositionInfo", args: [id],
  });
  const key = {
    currency0: keyRaw.currency0 === ZERO ? ZERO : getAddress(keyRaw.currency0),
    currency1: getAddress(keyRaw.currency1),
    fee: Number(keyRaw.fee), tickSpacing: Number(keyRaw.tickSpacing), hooks: getAddress(keyRaw.hooks),
  };
  const { tickLower, tickUpper } = unpackInfo(infoPacked);
  const poolId = poolIdOf(key);
  const liquidity = await pub.readContract({ address: POSM, abi: posmAbi, functionName: "getPositionLiquidity", args: [id] });
  const st = await slot0(poolId);

  const sqrtA = getSqrtRatioAtTick(tickLower);
  const sqrtB = getSqrtRatioAtTick(tickUpper);
  let [amount0, amount1] = amountsForLiquidity(st.sqrtPriceX96, sqrtA, sqrtB, liquidity);

  if (liquidity > 0n) {
    // Pending fees: liquidity * (feeGrowthInside_now - feeGrowthInside_last)
    // in Q128, with the same overflow-wrapping subtraction the core uses.
    const salt = `0x${id.toString(16).padStart(64, "0")}`;
    const [[, fg0Last, fg1Last], [fg0Now, fg1Now]] = await Promise.all([
      pub.readContract({ address: STATE_VIEW, abi: stateViewAbi, functionName: "getPositionInfo", args: [poolId, POSM, tickLower, tickUpper, salt] }),
      pub.readContract({ address: STATE_VIEW, abi: stateViewAbi, functionName: "getFeeGrowthInside", args: [poolId, tickLower, tickUpper] }),
    ]);
    const wrapSub = (a, b) => (a - b + (1n << 256n)) & MAX_UINT256;
    amount0 += (liquidity * wrapSub(fg0Now, fg0Last)) >> 128n;
    amount1 += (liquidity * wrapSub(fg1Now, fg1Last)) >> 128n;
  }
  return { key, poolId, tickLower, tickUpper, liquidity, st, amount0, amount1 };
}

async function cmdState() {
  const id = BigInt(arg("id", "0"));
  if (id <= 0n) throw new Error("--id required");
  const snap = await positionSnapshot(id);
  const { key, poolId, tickLower, tickUpper, liquidity, st } = snap;

  const entry = readEntry(id);
  const qSide = quoteSide(key, entry?.quote);
  const quoteAddr = qSide === 0 ? key.currency0 : key.currency1;
  const tokenAddr = qSide === 0 ? key.currency1 : key.currency0;
  const q = QUOTES[quoteAddr.toLowerCase()] || { symbol: "?", decimals: 18, native: false };
  const tokenSymbol = (await tokenMeta(tokenAddr)).symbol;

  const valueRaw = valueInQuote(snap.amount0, snap.amount1, st.sqrtPriceX96, qSide === 0);
  const valueQuote = Number(formatUnits(valueRaw, q.decimals));
  const entryQuote = entry ? Number(entry.quoteIn) : (arg("entry-quote", "") ? Number(arg("entry-quote")) : null);
  const pnlPct = entryQuote ? ((valueQuote - entryQuote) / entryQuote) * 100 : null;
  const inRange = st.tick >= tickLower && st.tick < tickUpper;
  const ageMin = entry ? (Math.floor(Date.now() / 1000) - entry.ts) / 60 : null;

  console.log(JSON.stringify({
    tokenId: id.toString(), pool: poolId, protocol: "v4",
    pair: `${q.symbol} / ${tokenSymbol}`, token: tokenAddr, tokenSymbol,
    quote: quoteAddr, quoteSymbol: q.symbol, quoteDecimals: q.decimals,
    tick: st.tick, tickLower, tickUpper, inRange, liquidity: liquidity.toString(),
    valueQuote, entryQuote, pnlPct, ageMin,
    // v3-name aliases so quote-agnostic readers keep working; quoteSymbol
    // says what unit these are really in.
    valueWeth: valueQuote, entryWeth: entryQuote,
  }));
}

async function cmdPositions(account) {
  const acct = account.address.toLowerCase();
  const out = [];
  for (const idStr of journalTokenIds()) {
    const id = BigInt(idStr);
    const owner = await pub.readContract({ address: POSM, abi: posmAbi, functionName: "ownerOf", args: [id] }).catch(() => null);
    if (!owner || owner.toLowerCase() !== acct) continue; // burned or transferred
    const [keyRaw, infoPacked] = await pub.readContract({ address: POSM, abi: posmAbi, functionName: "getPoolAndPositionInfo", args: [id] });
    const { tickLower, tickUpper } = unpackInfo(infoPacked);
    const liquidity = await pub.readContract({ address: POSM, abi: posmAbi, functionName: "getPositionLiquidity", args: [id] });
    out.push({
      tokenId: idStr, protocol: "v4",
      currency0: keyRaw.currency0, currency1: keyRaw.currency1, fee: Number(keyRaw.fee),
      tickLower, tickUpper, liquidity: liquidity.toString(),
    });
  }
  console.log(JSON.stringify({ address: account.address, count: out.length, positions: out }));
}

async function cmdCollect(wallet, account) {
  const id = BigInt(arg("id", "0"));
  if (id <= 0n) throw new Error("--id required");
  await ensureGas(wallet, account);
  const [keyRaw] = await pub.readContract({ address: POSM, abi: posmAbi, functionName: "getPoolAndPositionInfo", args: [id] });
  // Fee collect in v4 = decrease by 0 + TAKE_PAIR (no collect() exists).
  const unlockData = encodeActions([
    [A.DECREASE_LIQUIDITY, decreaseParams(id, 0n)],
    [A.TAKE_PAIR, takePairParams(keyRaw.currency0, keyRaw.currency1, account.address)],
  ]);
  const deadline = BigInt(Math.floor(Date.now() / 1000) + 120);
  await send(wallet, {
    address: POSM, abi: posmAbi, functionName: "modifyLiquidities",
    args: [unlockData, deadline], account: wallet.account, chain,
  }, `collect #${id}`);
  console.log(JSON.stringify({ success: true, tokenId: id.toString() }));
}

async function cmdClose(wallet, account) {
  const id = BigInt(arg("id", "0"));
  if (id <= 0n) throw new Error("--id required");
  // Same close-authority guard as the v3 executor: uni_monitor.py owns the
  // exit rulebook; anything else needs --force.
  if (!DRY_RUN && process.env.UNI_CLOSE_AUTH !== "1" && !hasFlag("force")) {
    throw new Error("close requires UNI_CLOSE_AUTH=1 (monitor) or --force (manual)");
  }
  const gasTopup = await ensureGas(wallet, account);
  const [keyRaw] = await pub.readContract({ address: POSM, abi: posmAbi, functionName: "getPoolAndPositionInfo", args: [id] });
  const key = {
    currency0: keyRaw.currency0 === ZERO ? ZERO : getAddress(keyRaw.currency0),
    currency1: getAddress(keyRaw.currency1),
    fee: Number(keyRaw.fee), tickSpacing: Number(keyRaw.tickSpacing), hooks: getAddress(keyRaw.hooks),
  };
  const entry = readEntry(id);
  const qSide = quoteSide(key, entry?.quote);
  const quoteIs0 = qSide === 0;
  const quoteAddr = quoteIs0 ? key.currency0 : key.currency1;
  const tokenAddr = quoteIs0 ? key.currency1 : key.currency0;
  const q = QUOTES[quoteAddr.toLowerCase()] || { symbol: "?", decimals: 18, native: false };

  const tokenBalBefore = DRY_RUN ? 0n : await balanceOf(tokenAddr, account.address);

  // BURN_POSITION removes all liquidity + accrued fees in one action and
  // TAKE_PAIR pays both currencies out — one tx replaces the v3 script's
  // decrease/collect/burn triplet.
  const unlockData = encodeActions([
    [A.BURN_POSITION, burnParams(id)],
    [A.TAKE_PAIR, takePairParams(key.currency0, key.currency1, account.address)],
  ]);
  const deadline = BigInt(Math.floor(Date.now() / 1000) + 120);
  await send(wallet, {
    address: POSM, abi: posmAbi, functionName: "modifyLiquidities",
    args: [unlockData, deadline], account: wallet.account, chain,
  }, `burn+take #${id}`);

  // Sell the freed token side back to the quote. Reports itself instead of
  // failing the close — the position is already gone (same lesson as v3:
  // a revert here must not journal success=false on a burned position).
  let sold = null;
  let stranded = null;
  if (!hasFlag("no-swap-out") && !DRY_RUN) {
    const bal = (await balanceOf(tokenAddr, account.address)) - tokenBalBefore;
    if (bal > 0n) {
      const sym = (await tokenMeta(tokenAddr)).symbol;
      const r = await sellTokenForQuote(wallet, account, key, quoteIs0, bal);
      if (r.ok) {
        sold = { token: tokenAddr, symbol: sym, quote_out: formatUnits(r.amountOut, q.decimals), tx: r.tx };
      } else {
        stranded = {
          tokenId: id.toString(), token: tokenAddr, symbol: sym, amount: bal.toString(),
          key, quote: quoteAddr, reason: r.reason, resolved: false,
          attempts: 1, next_try: Math.floor(Date.now() / 1000) + retryDelay(1),
        };
        journalStranded(stranded);
        console.error(`warn: could not sell ${sym} on close #${id} — ${r.reason} (bag journaled for sweep)`);
      }
    }
  }
  // A native-quote close pays out raw ETH; park anything above the gas target
  // back in WETH where the daemon's sizing can see it.
  let rewrapped = null;
  if (q.native && !DRY_RUN) rewrapped = await rewrapExcess(wallet, account);

  console.log(JSON.stringify({
    success: true, closed: id.toString(), protocol: "v4",
    swapped_out: !!sold,
    quote_out: sold ? sold.quote_out : "0", quote_symbol: q.symbol,
    // v3-name alias for quote-agnostic consumers (uni_monitor.py close path).
    weth_out: sold ? sold.quote_out : "0",
    stranded, rewrapped, gas_topup: gasTopup,
  }));
}

async function cmdSweep(wallet, account) {
  const only = arg("token", "");
  let bags = openStranded();
  if (bags.length || only) await ensureGas(wallet, account);
  if (only) {
    const t = getAddress(only);
    const known = bags.find((b) => getAddress(b.token) === t);
    // No adoption path here, unlike v3: selling a v4 bag needs its PoolKey,
    // and an arbitrary token's key cannot be derived from its address alone
    // (no factory to walk). A bag this journal has never seen belongs to the
    // v3 sweep or to the operator.
    if (!known) throw new Error(`${t} is not in the v4 stranded journal — v4 sweep cannot adopt unknown tokens (no PoolKey)`);
    bags = [known];
  }

  const now = Math.floor(Date.now() / 1000);
  const results = [];
  let waiting = 0;
  for (const bag of bags) {
    if (!only && bag.next_try && bag.next_try > now) { waiting++; continue; }
    const token = getAddress(bag.token);
    const bal = await pub.readContract({ address: token, abi: erc20Abi, functionName: "balanceOf", args: [account.address] });
    if (bal === 0n) {
      journalStranded({ ...bag, amount: "0", resolved: true, note: "balance is zero" });
      results.push({ token, symbol: bag.symbol, resolved: true, quote_out: "0", note: "balance is zero" });
      continue;
    }
    if (DRY_RUN) {
      results.push({ token, symbol: bag.symbol, dry_run: true, amount: bal.toString() });
      continue;
    }
    const key = bag.key;
    if (!key) {
      results.push({ token, symbol: bag.symbol, resolved: false, reason: "journal line has no PoolKey" });
      continue;
    }
    const quoteIs0 = (bag.quote || "").toLowerCase() === key.currency0.toLowerCase();
    const q = QUOTES[(bag.quote || "").toLowerCase()] || { symbol: "?", decimals: 18 };
    const r = await sellTokenForQuote(wallet, account, key, quoteIs0, bal);
    if (r.ok) {
      journalStranded({ ...bag, amount: bal.toString(), resolved: true, quote_out: formatUnits(r.amountOut, q.decimals), tx: r.tx });
      results.push({ token, symbol: bag.symbol, resolved: true, quote_out: formatUnits(r.amountOut, q.decimals), quote_symbol: q.symbol, tx: r.tx });
    } else {
      const attempts = (bag.attempts || 0) + 1;
      const delay = retryDelay(attempts);
      journalStranded({ ...bag, amount: bal.toString(), reason: r.reason, resolved: false, attempts, next_try: now + delay });
      results.push({ token, symbol: bag.symbol, resolved: false, amount: bal.toString(), reason: r.reason, attempts, retry_in_s: delay });
    }
  }
  console.log(JSON.stringify({
    success: true,
    swept: results.length,
    backing_off: waiting,
    still_stranded: results.filter((r) => r.resolved === false).length,
    results,
  }));
}

async function main() {
  const cmd = process.argv[2];
  const account = getAccount();
  const wallet = createWalletClient({ account, chain, transport: http(RPC_URL) });
  switch (cmd) {
    case "address": return cmdAddress(account);
    case "balance": return cmdBalance(account);
    case "quote": return cmdQuote();
    case "deploy": return cmdDeploy(wallet, account);
    case "positions": return cmdPositions(account);
    case "state": return cmdState();
    case "collect": return cmdCollect(wallet, account);
    case "close": return cmdClose(wallet, account);
    case "sweep": return cmdSweep(wallet, account);
    case "unwrap": return cmdUnwrap(wallet, account);
    default:
      console.error("usage: uni_v4_executor.js address|balance|quote|deploy|positions|state|collect|close|sweep|unwrap [--flags]");
      process.exit(2);
  }
}

main().catch((e) => {
  console.log(JSON.stringify({ success: false, error: e.shortMessage || e.message }));
  process.exit(1);
});
