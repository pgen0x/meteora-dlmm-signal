// Uniswap v3 position executor for Robinhood Chain (chain ID 4663).
// EVM sibling of dlmm_executor.js: wraps ETH, swaps WETH<->token via
// SwapRouter02, and mints/collects/closes NonfungiblePositionManager
// positions. viem only — no @uniswap/* SDKs (tick math needed here is small).
//
// Commands:
//   node uni_executor.js address                       # derived EVM address (fund this)
//   node uni_executor.js balance                       # ETH + WETH balances
//   node uni_executor.js wrap --amount 0.05            # ETH -> WETH
//   node uni_executor.js quote --pool 0x..             # pool state (tick, price, fee)
//   node uni_executor.js deploy --pool 0x.. --amount 0.01 [--strategy balanced_tight|weth_below] [--range-pct 10] [--slippage 5]
//   node uni_executor.js positions                     # owned NPM positions
//   node uni_executor.js collect --id 123              # collect fees only
//   node uni_executor.js close --id 123 [--no-swap-out]  # remove + collect + burn (+ token->WETH)
//
// Env (Hermes profile .env): EVM_PRIVATE_KEY — either a 0x-prefixed 32-byte
// hex key, or a base58 Solana secret key (the 32-byte ed25519 seed is reused
// as the secp256k1 scalar so one funded identity serves both venues until a
// dedicated EVM key exists). ROBINHOOD_RPC_URL optional. DRY_RUN=true skips
// every send and prints the 🧪 DRY RUN DEPLOY marker instead of 🚀 DEPLOYED.

const bs58 = require("bs58");
const dotenv = require("dotenv");
const fs = require("fs");
const path = require("path");
const {
  createPublicClient, createWalletClient, http, parseEther, formatEther,
  getAddress, erc20Abi, parseAbi, maxUint128,
} = require("viem");
const { privateKeyToAccount } = require("viem/accounts");

// Same profile resolution as dlmm_executor.js: process.argv[1], not __dirname,
// so a symlinked scripts/ dir still resolves to the profile, not this repo.
const SCRIPT_DIR = path.dirname(path.isAbsolute(process.argv[1]) ? process.argv[1] : path.resolve(process.argv[1]));
const PROFILE_DIR = path.dirname(path.dirname(path.dirname(SCRIPT_DIR)));
const profileEnvPath = path.join(PROFILE_DIR, ".env");
if (fs.existsSync(profileEnvPath)) dotenv.config({ path: profileEnvPath });

const RPC_URL = process.env.ROBINHOOD_RPC_URL || "https://rpc.mainnet.chain.robinhood.com";
const DRY_RUN = String(process.env.DRY_RUN || "").toLowerCase() === "true";

// Uniswap v3 deployment on Robinhood Chain. Verified on-chain 2026-07-13:
// NPM.factory() and NPM.WETH9() match, bytecode present at every address
// (docs: developers.uniswap.org v3-robinhood-chain-deployments).
const CHAIN_ID = 4663;
const WETH = getAddress("0x0bd7d308f8e1639fab988df18a8011f41eacad73");
const NPM = getAddress("0x73991a25c818bf1f1128deaab1492d45638de0d3");
const ROUTER = getAddress("0xcaf681a66d020601342297493863e78c959e5cb2");

const chain = {
  id: CHAIN_ID,
  name: "Robinhood Chain",
  nativeCurrency: { name: "Ether", symbol: "ETH", decimals: 18 },
  rpcUrls: { default: { http: [RPC_URL] } },
};

const poolAbi = parseAbi([
  "function slot0() view returns (uint160 sqrtPriceX96, int24 tick, uint16 observationIndex, uint16 observationCardinality, uint16 observationCardinalityNext, uint8 feeProtocol, bool unlocked)",
  "function liquidity() view returns (uint128)",
  "function tickSpacing() view returns (int24)",
  "function token0() view returns (address)",
  "function token1() view returns (address)",
  "function fee() view returns (uint24)",
]);

const npmAbi = parseAbi([
  "function mint((address token0, address token1, uint24 fee, int24 tickLower, int24 tickUpper, uint256 amount0Desired, uint256 amount1Desired, uint256 amount0Min, uint256 amount1Min, address recipient, uint256 deadline)) payable returns (uint256 tokenId, uint128 liquidity, uint256 amount0, uint256 amount1)",
  "function positions(uint256 tokenId) view returns (uint96 nonce, address operator, address token0, address token1, uint24 fee, int24 tickLower, int24 tickUpper, uint128 liquidity, uint256 feeGrowthInside0LastX128, uint256 feeGrowthInside1LastX128, uint128 tokensOwed0, uint128 tokensOwed1)",
  "function balanceOf(address owner) view returns (uint256)",
  "function tokenOfOwnerByIndex(address owner, uint256 index) view returns (uint256)",
  "function decreaseLiquidity((uint256 tokenId, uint128 liquidity, uint256 amount0Min, uint256 amount1Min, uint256 deadline)) payable returns (uint256 amount0, uint256 amount1)",
  "function collect((uint256 tokenId, address recipient, uint128 amount0Max, uint128 amount1Max)) payable returns (uint256 amount0, uint256 amount1)",
  "function burn(uint256 tokenId) payable",
]);

const routerAbi = parseAbi([
  "function exactInputSingle((address tokenIn, address tokenOut, uint24 fee, address recipient, uint256 amountIn, uint256 amountOutMinimum, uint160 sqrtPriceLimitX96)) payable returns (uint256 amountOut)",
]);

const wethAbi = parseAbi(["function deposit() payable"]);

function getAccount() {
  const raw = (process.env.EVM_PRIVATE_KEY || "").trim();
  if (!raw) throw new Error("EVM_PRIVATE_KEY not set in profile .env");
  if (raw.startsWith("0x") && raw.length === 66) return privateKeyToAccount(raw);
  // Base58 Solana secret key: 64 bytes (seed || ed25519 pubkey) or a bare
  // 32-byte seed. The seed bytes become the secp256k1 private key — a
  // deliberate stopgap so the Solana wallet identity funds this venue too.
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
    return "DRY_RUN_TX_HASH";
  }
  const hash = await wallet.writeContract(req);
  const rcpt = await pub.waitForTransactionReceipt({ hash, timeout: 120_000 });
  if (rcpt.status !== "success") throw new Error(`${label} reverted: ${hash}`);
  console.log(`${label}: ${hash}`);
  return hash;
}

async function ensureAllowance(wallet, owner, token, spender, amount) {
  const current = await pub.readContract({ address: token, abi: erc20Abi, functionName: "allowance", args: [owner, spender] });
  if (current >= amount) return;
  // Exact-amount approval on purpose — no unlimited allowances on a memecoin venue.
  await send(wallet, { address: token, abi: erc20Abi, functionName: "approve", args: [spender, amount], account: wallet.account, chain }, `approve ${spender.slice(0, 10)}`);
}

async function poolState(pool) {
  const [slot0, tickSpacing, token0, token1, fee, liquidity] = await Promise.all([
    pub.readContract({ address: pool, abi: poolAbi, functionName: "slot0" }),
    pub.readContract({ address: pool, abi: poolAbi, functionName: "tickSpacing" }),
    pub.readContract({ address: pool, abi: poolAbi, functionName: "token0" }),
    pub.readContract({ address: pool, abi: poolAbi, functionName: "token1" }),
    pub.readContract({ address: pool, abi: poolAbi, functionName: "fee" }),
    pub.readContract({ address: pool, abi: poolAbi, functionName: "liquidity" }),
  ]);
  return { sqrtPriceX96: slot0[0], tick: slot0[1], tickSpacing, token0: getAddress(token0), token1: getAddress(token1), fee, liquidity };
}

// pctToTicks converts a +/- percent band to a tick count (1 tick = 1.0001x).
function pctToTicks(pct) { return Math.round(Math.log(1 + pct / 100) / Math.log(1.0001)); }
function roundToSpacing(tick, spacing, up) {
  const q = tick / spacing;
  return (up ? Math.ceil(q) : Math.floor(q)) * spacing;
}

// spotOutFor computes the spot-price output of `amountIn` of tokenIn using
// sqrtPriceX96 (price of token1 in token0 terms), for the swap minOut guard.
function spotOutFor(amountIn, sqrtPriceX96, zeroForOne) {
  const Q96 = 1n << 96n;
  // price1per0 = (sqrtP/Q96)^2 -> amount1 = amount0 * sqrtP^2 / Q96^2
  if (zeroForOne) return (amountIn * sqrtPriceX96 * sqrtPriceX96) / (Q96 * Q96);
  return (amountIn * Q96 * Q96) / (sqrtPriceX96 * sqrtPriceX96);
}

async function cmdAddress(account) {
  console.log(JSON.stringify({ address: account.address, derivedFrom: process.env.EVM_PRIVATE_KEY?.startsWith("0x") ? "hex" : "solana-seed", chainId: CHAIN_ID }));
}

async function cmdBalance(account) {
  const [eth, weth] = await Promise.all([
    pub.getBalance({ address: account.address }),
    pub.readContract({ address: WETH, abi: erc20Abi, functionName: "balanceOf", args: [account.address] }),
  ]);
  console.log(JSON.stringify({ address: account.address, eth: formatEther(eth), weth: formatEther(weth) }));
}

async function cmdWrap(wallet) {
  const amount = parseEther(arg("amount", "0"));
  if (amount <= 0n) throw new Error("--amount required (ETH)");
  await send(wallet, { address: WETH, abi: wethAbi, functionName: "deposit", value: amount, account: wallet.account, chain }, `wrap ${formatEther(amount)} ETH`);
  console.log(JSON.stringify({ success: true, wrapped: formatEther(amount) }));
}

async function cmdQuote() {
  const pool = getAddress(arg("pool", ""));
  const st = await poolState(pool);
  const [sym0, sym1] = await Promise.all([
    pub.readContract({ address: st.token0, abi: erc20Abi, functionName: "symbol" }).catch(() => "?"),
    pub.readContract({ address: st.token1, abi: erc20Abi, functionName: "symbol" }).catch(() => "?"),
  ]);
  console.log(JSON.stringify({
    pool, token0: `${sym0} ${st.token0}`, token1: `${sym1} ${st.token1}`,
    fee: Number(st.fee), tick: Number(st.tick), tickSpacing: Number(st.tickSpacing),
    sqrtPriceX96: st.sqrtPriceX96.toString(), liquidity: st.liquidity.toString(),
    wethIsToken0: st.token0 === WETH,
  }));
}

async function cmdDeploy(wallet, account) {
  const pool = getAddress(arg("pool", ""));
  const amountWeth = parseEther(arg("amount", "0"));
  const strategy = arg("strategy", "balanced_tight");
  const rangePct = parseFloat(arg("range-pct", "10"));
  const slippagePct = parseFloat(arg("slippage", "5"));
  if (amountWeth <= 0n) throw new Error("--amount required (WETH)");

  const st = await poolState(pool);
  if (st.token0 !== WETH && st.token1 !== WETH) throw new Error("pool has no WETH side");
  const wethIs0 = st.token0 === WETH;
  const token = wethIs0 ? st.token1 : st.token0;
  const spacing = Number(st.tickSpacing);
  const tick = Number(st.tick);
  const bandTicks = Math.max(pctToTicks(rangePct), spacing);

  let tickLower, tickUpper, amount0 = 0n, amount1 = 0n, swapped = 0n;

  if (strategy === "balanced_tight") {
    // Two-sided +/- rangePct around the current tick; half the WETH is
    // swapped into the token so both sides carry inventory.
    tickLower = roundToSpacing(tick - bandTicks, spacing, false);
    tickUpper = roundToSpacing(tick + bandTicks, spacing, true);
    const half = amountWeth / 2n;
    const spotOut = spotOutFor(half, st.sqrtPriceX96, wethIs0);
    const minOut = (spotOut * BigInt(Math.floor((100 - slippagePct) * 100))) / 10000n;
    await ensureAllowance(wallet, account.address, WETH, ROUTER, half);
    await send(wallet, {
      address: ROUTER, abi: routerAbi, functionName: "exactInputSingle",
      args: [{ tokenIn: WETH, tokenOut: token, fee: st.fee, recipient: account.address, amountIn: half, amountOutMinimum: minOut, sqrtPriceLimitX96: 0n }],
      account: wallet.account, chain,
    }, `swap ${formatEther(half)} WETH -> token`);
    swapped = half;
    const tokenBal = DRY_RUN ? spotOut : await pub.readContract({ address: token, abi: erc20Abi, functionName: "balanceOf", args: [account.address] });
    if (wethIs0) { amount0 = amountWeth - half; amount1 = tokenBal; }
    else { amount0 = tokenBal; amount1 = amountWeth - half; }
  } else if (strategy === "weth_below") {
    // One-sided WETH band adjacent to the current tick (bid side): no swap,
    // pure fee capture that converts to the token only if price crosses in.
    // Direction depends on token ordering: WETH-as-token0 inventory is
    // consumed as the tick RISES, so its band sits above the current tick;
    // WETH-as-token1 the reverse.
    if (wethIs0) {
      tickLower = roundToSpacing(tick + spacing, spacing, true);
      tickUpper = roundToSpacing(tick + spacing + 2 * bandTicks, spacing, true);
      amount0 = amountWeth;
    } else {
      tickUpper = roundToSpacing(tick - spacing, spacing, false);
      tickLower = roundToSpacing(tick - spacing - 2 * bandTicks, spacing, false);
      amount1 = amountWeth;
    }
  } else {
    throw new Error(`unknown strategy ${strategy}`);
  }

  await ensureAllowance(wallet, account.address, WETH, NPM, wethIs0 ? amount0 : amount1);
  if (swapped > 0n) await ensureAllowance(wallet, account.address, token, NPM, wethIs0 ? amount1 : amount0);

  const deadline = BigInt(Math.floor(Date.now() / 1000) + 120);
  const mintArgs = {
    token0: st.token0, token1: st.token1, fee: st.fee,
    tickLower, tickUpper,
    amount0Desired: amount0, amount1Desired: amount1,
    // Min amounts stay 0: mint pulls at spot with no price impact, the swap
    // leg above already carries the slippage guard, and leftovers stay in
    // the wallet rather than reverting a tight-band mint on tick drift.
    amount0Min: 0n, amount1Min: 0n,
    recipient: account.address, deadline,
  };

  if (DRY_RUN) {
    console.log(`🧪 DRY RUN DEPLOY pool=${pool} strategy=${strategy} ticks=[${tickLower},${tickUpper}] amount=${formatEther(amountWeth)} WETH`);
    console.log(JSON.stringify({ success: true, dryRun: true, pool, strategy, tickLower, tickUpper }));
    return;
  }
  const hash = await wallet.writeContract({ address: NPM, abi: npmAbi, functionName: "mint", args: [mintArgs], account: wallet.account, chain });
  const rcpt = await pub.waitForTransactionReceipt({ hash, timeout: 120_000 });
  if (rcpt.status !== "success") throw new Error(`mint reverted: ${hash}`);
  // tokenId = ERC721 Transfer(0x0 -> us) log from the NPM contract.
  const xfer = rcpt.logs.find((l) => l.address.toLowerCase() === NPM.toLowerCase() && l.topics.length === 4);
  const tokenId = xfer ? BigInt(xfer.topics[3]).toString() : "unknown";
  console.log(`🚀 DEPLOYED pool=${pool} strategy=${strategy} position=${tokenId} tx=${hash}`);
  console.log(JSON.stringify({ success: true, pool, strategy, tokenId, tickLower, tickUpper, tx: hash }));
}

async function cmdPositions(account) {
  const n = await pub.readContract({ address: NPM, abi: npmAbi, functionName: "balanceOf", args: [account.address] });
  const out = [];
  for (let i = 0n; i < n; i++) {
    const id = await pub.readContract({ address: NPM, abi: npmAbi, functionName: "tokenOfOwnerByIndex", args: [account.address, i] });
    const p = await pub.readContract({ address: NPM, abi: npmAbi, functionName: "positions", args: [id] });
    out.push({
      tokenId: id.toString(), token0: p[2], token1: p[3], fee: Number(p[4]),
      tickLower: Number(p[5]), tickUpper: Number(p[6]), liquidity: p[7].toString(),
      owed0: p[10].toString(), owed1: p[11].toString(),
    });
  }
  console.log(JSON.stringify({ address: account.address, count: Number(n), positions: out }));
}

async function cmdCollect(wallet, account) {
  const id = BigInt(arg("id", "0"));
  if (id <= 0n) throw new Error("--id required");
  await send(wallet, {
    address: NPM, abi: npmAbi, functionName: "collect",
    args: [{ tokenId: id, recipient: account.address, amount0Max: maxUint128, amount1Max: maxUint128 }],
    account: wallet.account, chain,
  }, `collect #${id}`);
  console.log(JSON.stringify({ success: true, tokenId: id.toString() }));
}

async function cmdClose(wallet, account) {
  const id = BigInt(arg("id", "0"));
  if (id <= 0n) throw new Error("--id required");
  const p = await pub.readContract({ address: NPM, abi: npmAbi, functionName: "positions", args: [id] });
  const [token0, token1, liquidity] = [getAddress(p[2]), getAddress(p[3]), p[7]];
  const deadline = BigInt(Math.floor(Date.now() / 1000) + 120);

  if (liquidity > 0n) {
    await send(wallet, {
      address: NPM, abi: npmAbi, functionName: "decreaseLiquidity",
      args: [{ tokenId: id, liquidity, amount0Min: 0n, amount1Min: 0n, deadline }],
      account: wallet.account, chain,
    }, `decrease #${id}`);
  }
  await send(wallet, {
    address: NPM, abi: npmAbi, functionName: "collect",
    args: [{ tokenId: id, recipient: account.address, amount0Max: maxUint128, amount1Max: maxUint128 }],
    account: wallet.account, chain,
  }, `collect #${id}`);
  await send(wallet, { address: NPM, abi: npmAbi, functionName: "burn", args: [id], account: wallet.account, chain }, `burn #${id}`);

  // Swap the freed token side back to WETH unless told otherwise, mirroring
  // the Solana monitor's auto-swap-to-SOL on close.
  if (!hasFlag("no-swap-out") && !DRY_RUN) {
    const token = token0 === WETH ? token1 : token0;
    const fee = p[4];
    const bal = await pub.readContract({ address: token, abi: erc20Abi, functionName: "balanceOf", args: [account.address] });
    if (bal > 0n) {
      await ensureAllowance(wallet, account.address, token, ROUTER, bal);
      await send(wallet, {
        address: ROUTER, abi: routerAbi, functionName: "exactInputSingle",
        args: [{ tokenIn: token, tokenOut: WETH, fee, recipient: account.address, amountIn: bal, amountOutMinimum: 0n, sqrtPriceLimitX96: 0n }],
        account: wallet.account, chain,
      }, "swap token -> WETH");
    }
  }
  console.log(JSON.stringify({ success: true, closed: id.toString() }));
}

async function main() {
  const cmd = process.argv[2];
  const account = getAccount();
  const wallet = createWalletClient({ account, chain, transport: http(RPC_URL) });
  switch (cmd) {
    case "address": return cmdAddress(account);
    case "balance": return cmdBalance(account);
    case "wrap": return cmdWrap(wallet);
    case "quote": return cmdQuote();
    case "deploy": return cmdDeploy(wallet, account);
    case "positions": return cmdPositions(account);
    case "collect": return cmdCollect(wallet, account);
    case "close": return cmdClose(wallet, account);
    default:
      console.error("usage: uni_executor.js address|balance|wrap|quote|deploy|positions|collect|close [--flags]");
      process.exit(2);
  }
}

main().catch((e) => {
  console.log(JSON.stringify({ success: false, error: e.shortMessage || e.message }));
  process.exit(1);
});
