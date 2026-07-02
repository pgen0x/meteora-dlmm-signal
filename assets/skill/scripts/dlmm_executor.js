const { Connection, Keypair, PublicKey, sendAndConfirmTransaction, VersionedTransaction } = require("@solana/web3.js");
const DLMM = require("@meteora-ag/dlmm");
const { StrategyType } = require("@meteora-ag/dlmm");
const BN = require("bn.js");
const bs58 = require("bs58");
const dotenv = require("dotenv");
const fs = require("fs");
const path = require("path");

// Resolved from this file's own location (<profile>/skills/solana-dlmm/scripts/) so the
// script works whether it's a copy or a symlink into a Hermes profile — no install-time
// path rewrite needed.
const PROFILE_DIR = path.dirname(path.dirname(path.dirname(__dirname)));

// Load environment variables
const profileEnvPath = path.join(PROFILE_DIR, ".env");
const legacyEnvPath = path.join(PROFILE_DIR, ".env");

if (fs.existsSync(profileEnvPath)) {
  dotenv.config({ path: profileEnvPath });
}
if (fs.existsSync(legacyEnvPath)) {
  const legacyEnv = dotenv.parse(fs.readFileSync(legacyEnvPath));
  for (const k in legacyEnv) {
    if (!process.env[k]) process.env[k] = legacyEnv[k];
  }
}

// Comma-separated list of RPC endpoints, tried in order with failover on error.
// Set SOLANA_RPC_URLS in <profile>/.env — put your own Helius/QuickNode/etc. keys
// there, never hardcode them here (this repo is public).
const RPC_URLS = (process.env.SOLANA_RPC_URLS || "https://api.mainnet-beta.solana.com")
  .split(",")
  .map((s) => s.trim())
  .filter(Boolean);

let currentRpcIndex = 0;

function getWallet() {
  const pk = process.env.SOLANA_PRIVATE_KEY || process.env.WALLET_PRIVATE_KEY;
  if (!pk) {
    throw new Error("Solana private key not found in environment (checked SOLANA_PRIVATE_KEY and WALLET_PRIVATE_KEY)");
  }
  try {
    return Keypair.fromSecretKey(bs58.decode(pk.trim()));
  } catch (err) {
    // Try raw bytes parse if base58 fails
    try {
      return Keypair.fromSecretKey(Uint8Array.from(JSON.parse(pk)));
    } catch {
      throw new Error(`Failed to decode private key: ${err.message}`);
    }
  }
}

async function runWithFailover(fn) {
  let attempts = 0;
  while (attempts < RPC_URLS.length) {
    const rpcUrl = RPC_URLS[currentRpcIndex];
    try {
      const connection = new Connection(rpcUrl, "confirmed");
      return await fn(connection);
    } catch (err) {
      console.warn(`[RPC WARN] Failed execution on RPC #${currentRpcIndex}: ${err.message}`);
      currentRpcIndex = (currentRpcIndex + 1) % RPC_URLS.length;
      attempts++;
    }
  }
  throw new Error(`All ${RPC_URLS.length} RPC endpoints failed to execute the command.`);
}

async function getActiveBin(poolAddressStr) {
  return await runWithFailover(async (connection) => {
    const pool = await DLMM.create(connection, new PublicKey(poolAddressStr));
    const activeBin = await pool.getActiveBin();
    const price = pool.fromPricePerLamport(Number(activeBin.price));
    return {
      binId: activeBin.binId,
      price: price,
      pricePerLamport: activeBin.price.toString()
    };
  });
}

async function getTokenDecimals(connection, mintPubKey) {
  if (mintPubKey.toString() === "So11111111111111111111111111111111111111112") {
    return 9;
  }
  try {
    const info = await connection.getParsedAccountInfo(mintPubKey);
    return info.value?.data?.parsed?.info?.decimals ?? 9;
  } catch (err) {
    console.warn(`Failed to get decimals for ${mintPubKey.toString()}, defaulting to 9: ${err.message}`);
    return 9;
  }
}

function getDlmmProgramId() {
  return new PublicKey("LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo");
}

function formatSolFee(value) {
  const number = Number(value ?? 0);
  return Number.isFinite(number) ? number.toFixed(8).replace(/0+$/, "").replace(/\.$/, "") : "unknown";
}

// Read-only inspection of whether a price range can be deployed into without
// paying to initialize new Meteora bin arrays / bitmap extension. Spends nothing.
// Returns { deployable, missing, totalFee, needsBitmap, bitmapFee, missingSample }.
// Throws only if the SDK helpers are unavailable (cannot verify -> caller decides).
async function inspectBinArrayCoverage(connection, pool, minBinId, maxBinId) {
  const getBinArrayKeysCoverage = DLMM.getBinArrayKeysCoverage;
  const getBinArrayIndexesCoverage = DLMM.getBinArrayIndexesCoverage;
  const deriveBinArrayBitmapExtension = DLMM.deriveBinArrayBitmapExtension;
  const isOverflowDefaultBinArrayBitmap = DLMM.isOverflowDefaultBinArrayBitmap;
  const BIN_ARRAY_FEE = Number(DLMM.BIN_ARRAY_FEE ?? 0.07143744);
  const BIN_ARRAY_BITMAP_FEE = Number(DLMM.BIN_ARRAY_BITMAP_FEE ?? 0.01180416);

  if (!getBinArrayKeysCoverage || !getBinArrayIndexesCoverage) {
    throw new Error("Cannot verify Meteora bin-array initialization risk; SDK helpers unavailable.");
  }

  const programId = getDlmmProgramId();
  const poolPubkey = pool.pubkey;
  const lower = new BN(Math.min(minBinId, maxBinId));
  const upper = new BN(Math.max(minBinId, maxBinId));
  const indexes = getBinArrayIndexesCoverage(lower, upper);
  const keys = getBinArrayKeysCoverage(lower, upper, poolPubkey, programId);
  const accounts = await connection.getMultipleAccountsInfo(keys, "confirmed");
  const missing = accounts
    .map((account, index) => account ? null : {
      index: indexes[index]?.toString?.() ?? String(index),
      address: keys[index].toString(),
    })
    .filter(Boolean);

  let needsBitmap = false;
  if (deriveBinArrayBitmapExtension && isOverflowDefaultBinArrayBitmap) {
    const overflow = indexes.some((index) => isOverflowDefaultBinArrayBitmap(index));
    if (overflow) {
      const [bitmapExtension] = deriveBinArrayBitmapExtension(poolPubkey, programId);
      const account = await connection.getAccountInfo(bitmapExtension, "confirmed");
      needsBitmap = !account;
    }
  }

  const totalFee = missing.length * BIN_ARRAY_FEE + (needsBitmap ? BIN_ARRAY_BITMAP_FEE : 0);
  return {
    deployable: missing.length === 0 && !needsBitmap,
    missing: missing.length,
    totalFee,
    binArrayFee: BIN_ARRAY_FEE,
    needsBitmap,
    bitmapFee: BIN_ARRAY_BITMAP_FEE,
    missingSample: missing.slice(0, 3).map((e) => `${e.index}:${e.address.slice(0, 8)}`),
  };
}

async function assertRangeDoesNotRequireBinArrayInitialization(connection, pool, minBinId, maxBinId) {
  const cov = await inspectBinArrayCoverage(connection, pool, minBinId, maxBinId);
  if (cov.missing > 0) {
    const sample = cov.missingSample.join(", ");
    throw new Error(
      `Deploy skipped: selected range requires ${cov.missing} missing Meteora bin-array initialization(s) ` +
      `(~${formatSolFee(cov.missing * cov.binArrayFee)} SOL non-refundable pool rent; ${formatSolFee(cov.binArrayFee)} SOL each). ` +
      `Missing indexes: ${sample}${cov.missing > 3 ? ", ..." : ""}. Pick an already-initialized range/pool.`
    );
  }
  if (cov.needsBitmap) {
    throw new Error(
      `Deploy skipped: selected range requires Meteora bin-array bitmap extension initialization ` +
      `(~${formatSolFee(cov.bitmapFee)} SOL non-refundable pool rent). Pick a closer initialized range/pool.`
    );
  }
}

// Read-only: resolve a pool's active bin, then report bin-array coverage for the
// range [activeBin - binsBelow, activeBin + binsAbove]. No spend.
async function checkBinCoverage(poolAddressStr, binsBelow, binsAbove) {
  return await runWithFailover(async (connection) => {
    const pool = await DLMM.create(connection, new PublicKey(poolAddressStr));
    const activeBin = await pool.getActiveBin();
    const minBinId = activeBin.binId - binsBelow;
    const maxBinId = activeBin.binId + binsAbove;
    const cov = await inspectBinArrayCoverage(connection, pool, minBinId, maxBinId);
    return { success: true, pool: poolAddressStr, activeBin: activeBin.binId, minBinId, maxBinId, ...cov };
  });
}

// Close a freshly-minted empty position (no/partial liquidity) to refund rent.
// Used by deployPosition when phase-2 add-liquidity fails after the NFT is minted.
async function cleanupEmptyPosition(connection, pool, wallet, positionPubKey, minBinId, maxBinId) {
  try {
    // Handles the case where phase-2 added partial liquidity before failing.
    const closeTx = await pool.removeLiquidity({
      user: wallet.publicKey,
      position: positionPubKey,
      fromBinId: minBinId,
      toBinId: maxBinId,
      bps: new BN(10000), // 100% — removes any partial liquidity and closes the NFT
      shouldClaimAndClose: true
    });
    for (const tx of Array.isArray(closeTx) ? closeTx : [closeTx]) {
      await sendAndConfirmTransaction(connection, tx, [wallet]);
    }
  } catch (rmErr) {
    // No liquidity to remove (NFT minted but add never landed) — close the empty NFT directly.
    console.warn(`[DLMM] removeLiquidity during cleanup failed (${rmErr.message}); using closePositionIfEmpty.`);
    const positionData = await pool.getPosition(positionPubKey);
    const emptyTx = await pool.closePositionIfEmpty({ owner: wallet.publicKey, position: positionData });
    for (const tx of Array.isArray(emptyTx) ? emptyTx : [emptyTx]) {
      await sendAndConfirmTransaction(connection, tx, [wallet]);
    }
  }
}

async function deployPosition(poolAddressStr, amountX, amountY, binsBelow, binsAbove, strategyTypeStr = "spot", slippageBps = 1000) {
  return await runWithFailover(async (connection) => {
    const wallet = getWallet();
    const pool = await DLMM.create(connection, new PublicKey(poolAddressStr));
    const activeBin = await pool.getActiveBin();
    
    const minBinId = activeBin.binId - binsBelow;
    const maxBinId = activeBin.binId + binsAbove;
    
    const tokenXDecimals = await getTokenDecimals(connection, pool.lbPair.tokenXMint);
    const tokenYDecimals = await getTokenDecimals(connection, pool.lbPair.tokenYMint);
    
    const totalXLamports = new BN(Math.floor(amountX * Math.pow(10, tokenXDecimals)));
    const totalYLamports = new BN(Math.floor(amountY * Math.pow(10, tokenYDecimals)));

    const strategyMap = {
      spot: StrategyType.Spot,
      curve: StrategyType.Curve,
      bid_ask: StrategyType.BidAsk,
    };
    const strategyType = strategyMap[strategyTypeStr.toLowerCase()] ?? StrategyType.Spot;

    console.log(`[DLMM] Deploying ${amountX} X and ${amountY} Y in pool ${poolAddressStr} across bins ${minBinId} to ${maxBinId} (strategy: ${strategyTypeStr})`);
    
    if (process.env.DRY_RUN === "true") {
      console.log("[DRY RUN] Would deploy position");
      return { success: true, dryRun: true, position: "DRY_RUN_POSITION_ADDR" };
    }

    await assertRangeDoesNotRequireBinArrayInitialization(connection, pool, minBinId, maxBinId);

    const newPosition = Keypair.generate();
    const totalBins = binsBelow + binsAbove;
    const isWideRange = totalBins > 69;
    const txHashes = [];

    if (isWideRange) {
      console.log(`[DLMM] Range exceeds 69 bins (${totalBins} bins). Executing chunked wide range deployment...`);
      // Wide-range deploy is two non-atomic phases: (1) mint empty position NFT,
      // (2) add liquidity. If phase 2 fails after phase 1, an empty 0-deposit NFT is
      // stranded on-chain. We track whether the NFT was minted: once it is, ANY later
      // failure is handled by cleanup-then-return (NOT throw), so runWithFailover never
      // re-runs phase 1 and mints duplicate orphans. Only a failure BEFORE the mint is
      // rethrown for legitimate RPC failover.
      let minted = false;
      try {
        // Phase 1: Create empty position
        const createTxs = await pool.createExtendedEmptyPosition(
          minBinId,
          maxBinId,
          newPosition.publicKey,
          wallet.publicKey
        );
        const createTxArray = Array.isArray(createTxs) ? createTxs : [createTxs];
        for (let i = 0; i < createTxArray.length; i++) {
          const signers = i === 0 ? [wallet, newPosition] : [wallet];
          const txHash = await sendAndConfirmTransaction(connection, createTxArray[i], signers);
          txHashes.push(txHash);
          minted = true; // first create tx confirmed → NFT exists on-chain
          console.log(`[DLMM] Create tx ${i + 1}/${createTxArray.length}: ${txHash}`);
        }

        // Phase 2: Add liquidity chunkable
        const addTxs = await pool.addLiquidityByStrategyChunkable({
          positionPubKey: newPosition.publicKey,
          user: wallet.publicKey,
          totalXAmount: totalXLamports,
          totalYAmount: totalYLamports,
          strategy: { minBinId, maxBinId, strategyType },
          slippage: slippageBps
        });
        const addTxArray = Array.isArray(addTxs) ? addTxs : [addTxs];
        for (let i = 0; i < addTxArray.length; i++) {
          const txHash = await sendAndConfirmTransaction(connection, addTxArray[i], [wallet]);
          txHashes.push(txHash);
          console.log(`[DLMM] Add liquidity tx ${i + 1}/${addTxArray.length}: ${txHash}`);
        }
      } catch (deployErr) {
        if (!minted) {
          // NFT never minted — safe to fail over / retry from scratch, no orphan.
          throw deployErr;
        }
        // NFT minted but deploy did not complete. Do NOT throw (would trigger
        // runWithFailover to re-mint a second orphan). Clean up the empty position.
        console.warn(`[DLMM] Deploy failed after position ${newPosition.publicKey.toString()} minted: ${deployErr.message}. Cleaning up empty position...`);
        try {
          await cleanupEmptyPosition(connection, pool, wallet, newPosition.publicKey, minBinId, maxBinId);
          console.warn(`[DLMM] Cleaned up empty position ${newPosition.publicKey.toString()} (rent refunded).`);
          return {
            success: false,
            error: `wide-range add-liquidity failed; empty position cleaned: ${deployErr.message}`,
            cleaned: true,
            position: newPosition.publicKey.toString(),
            txHashes
          };
        } catch (cleanErr) {
          // Cleanup also failed — surface the orphan so the caller registers it in
          // Redis and the monitor reconciliation loop can reclaim it next cycle.
          console.error(`[DLMM] Cleanup of empty position ${newPosition.publicKey.toString()} FAILED: ${cleanErr.message}. Returning orphan for monitor reclaim.`);
          return {
            success: false,
            error: `wide-range add-liquidity failed AND cleanup failed: ${deployErr.message} / ${cleanErr.message}`,
            orphan: true,
            position: newPosition.publicKey.toString(),
            pool: poolAddressStr,
            txHashes
          };
        }
      }
    } else {
      // Standard Path (<= 69 bins)
      const tx = await pool.initializePositionAndAddLiquidityByStrategy({
        positionPubKey: newPosition.publicKey,
        user: wallet.publicKey,
        totalXAmount: totalXLamports,
        totalYAmount: totalYLamports,
        strategy: { maxBinId, minBinId, strategyType },
        slippage: slippageBps
      });
      const txHash = await sendAndConfirmTransaction(connection, tx, [wallet, newPosition]);
      txHashes.push(txHash);
    }

    return {
      success: true,
      position: newPosition.publicKey.toString(),
      txHash: txHashes[0],
      txHashes
    };
  });
}


async function findPoolForPosition(connection, wallet, positionAddressStr) {
  // Try SDK first
  const allPositions = await DLMM.getAllLbPairPositionsByUser(connection, wallet.publicKey);
  for (const [lbPairKey, posData] of Object.entries(allPositions)) {
    const found = posData.lbPairPositionsData.find(p => p.publicKey.toString() === positionAddressStr);
    if (found) {
      const pool = await DLMM.create(connection, new PublicKey(lbPairKey));
      return { pool, positionData: found, poolAddressStr: lbPairKey };
    }
  }
  // SDK returned empty — fall back to Meteora Portfolio API to get pool address
  const walletAddr = wallet.publicKey.toString();
  const apiUrl = `https://dlmm.datapi.meteora.ag/portfolio/open?user=${walletAddr}`;
  let poolAddressStr = null;
  try {
    const res = await fetch(apiUrl);
    if (res.ok) {
      const data = await res.json();
      for (const poolData of (data.pools || [])) {
        if ((poolData.listPositions || []).includes(positionAddressStr)) {
          poolAddressStr = poolData.poolAddress;
          break;
        }
      }
    }
  } catch (err) {
    console.warn(`[DLMM] Portfolio API lookup failed: ${err.message}`);
  }
  if (!poolAddressStr) {
    return null;
  }
  // Create pool directly and fetch position via targeted query
  const pool = await DLMM.create(connection, new PublicKey(poolAddressStr));
  const userPositions = await pool.getPositionsByUserAndLbPair(wallet.publicKey);
  const found = userPositions.userPositions.find(p => p.publicKey.toString() === positionAddressStr);
  if (!found) {
    return null;
  }
  return { pool, positionData: found, poolAddressStr };
}

async function claimFees(positionAddressStr) {
  if (process.env.DRY_RUN === "true" && (positionAddressStr.includes("DRY_RUN") || positionAddressStr.length < 32)) {
    console.warn(JSON.stringify({ success: true, dryRun: true }));
    return { success: true, dryRun: true };
  }
  return await runWithFailover(async (connection) => {
    const wallet = getWallet();

    const found = await findPoolForPosition(connection, wallet, positionAddressStr);
    if (!found) {
      throw new Error(`Position ${positionAddressStr} not found for user ${wallet.publicKey.toString()}`);
    }
    const { pool, positionData } = found;

    if (process.env.DRY_RUN === "true") {
      console.log(`[DRY RUN] Would claim fees for ${positionAddressStr}`);
      return { success: true, dryRun: true };
    }

    const txs = await pool.claimSwapFee({
      owner: wallet.publicKey,
      position: positionData
    });

    const txHashes = [];
    for (const tx of txs) {
      const txHash = await sendAndConfirmTransaction(connection, tx, [wallet]);
      txHashes.push(txHash);
    }
    return {
      success: true,
      txHashes
    };
  });
}

async function closePosition(positionAddressStr) {
  if (process.env.DRY_RUN === "true" && (positionAddressStr.includes("DRY_RUN") || positionAddressStr.length < 32)) {
    console.warn(JSON.stringify({ success: true, dryRun: true, txHashes: ["DRY_RUN_TX_HASH"] }));
    return { success: true, dryRun: true, txHashes: ["DRY_RUN_TX_HASH"] };
  }
  return await runWithFailover(async (connection) => {
    const wallet = getWallet();

    const foundPos = await findPoolForPosition(connection, wallet, positionAddressStr);
    if (!foundPos) {
      throw new Error(`Position ${positionAddressStr} not found for user ${wallet.publicKey.toString()}`);
    }
    const { pool, positionData, poolAddressStr } = foundPos;

    if (process.env.DRY_RUN === "true") {
      console.log(`[DRY RUN] Would close position ${positionAddressStr}`);
      return { success: true, dryRun: true };
    }

    // Step 1: Claim any swap fees first to clear state
    try {
      const claimTxs = await pool.claimSwapFee({
        owner: wallet.publicKey,
        position: positionData
      });
      for (const tx of claimTxs) {
        await sendAndConfirmTransaction(connection, tx, [wallet]);
      }
    } catch (err) {
      console.warn(`[DLMM] Fee claim during close warning: ${err.message}`);
    }

    // Step 2: Remove all liquidity and close position
    const lowerBin = positionData.positionData.lowerBinId;
    const upperBin = positionData.positionData.upperBinId;
    
    console.log(`[DLMM] Withdrawing liquidity from bins ${lowerBin} to ${upperBin}`);

    const txHashes = [];
    try {
      const closeTx = await pool.removeLiquidity({
        user: wallet.publicKey,
        position: positionData.publicKey,
        fromBinId: lowerBin,
        toBinId: upperBin,
        bps: new BN(10000), // 100%
        shouldClaimAndClose: true
      });
      for (const tx of Array.isArray(closeTx) ? closeTx : [closeTx]) {
        const txHash = await sendAndConfirmTransaction(connection, tx, [wallet]);
        txHashes.push(txHash);
      }
    } catch (rmErr) {
      // Empty positions (0-deposit zombie NFTs from a failed wide-range deploy) have no
      // liquidity to remove — removeLiquidity throws. Fall back to closePositionIfEmpty,
      // which closes the NFT account directly and refunds rent.
      console.warn(`[DLMM] removeLiquidity failed (${rmErr.message}); attempting closePositionIfEmpty for empty position ${positionAddressStr}`);
      const emptyTx = await pool.closePositionIfEmpty({ owner: wallet.publicKey, position: positionData });
      for (const tx of Array.isArray(emptyTx) ? emptyTx : [emptyTx]) {
        const txHash = await sendAndConfirmTransaction(connection, tx, [wallet]);
        txHashes.push(txHash);
      }
    }

    return {
      success: true,
      txHashes
    };
  });
}

async function getPositions(walletAddressStr) {
  return await runWithFailover(async (connection) => {
    const targetWallet = walletAddressStr ? new PublicKey(walletAddressStr) : getWallet().publicKey;
    const allPositions = await DLMM.getAllLbPairPositionsByUser(connection, targetWallet);
    
    const result = [];
    for (const [lbPairKey, posData] of Object.entries(allPositions)) {
      const pool = await DLMM.create(connection, new PublicKey(lbPairKey));
      const activeBin = await pool.getActiveBin();
      
      for (const pos of posData.lbPairPositionsData) {
        const data = pos.positionData;
        const lowerBinId = data.lowerBinId;
        const upperBinId = data.upperBinId;
        const inRange = activeBin.binId >= lowerBinId && activeBin.binId <= upperBinId;
        
        result.push({
          position: pos.publicKey.toString(),
          pool: lbPairKey,
          tokenX: pool.tokenX.symbol,
          tokenY: pool.tokenY.symbol,
          lower_bin: lowerBinId,
          upper_bin: upperBinId,
          active_bin: activeBin.binId,
          in_range: inRange,
          feeX: pos.feeX.toString(),
          feeY: pos.feeY.toString()
        });
      }
    }
    return result;
  });
}

function normalizeMint(mint) {
  if (!mint) return mint;
  const SOL_MINT = "So11111111111111111111111111111111111111112";
  if (
    mint === "SOL" || 
    mint === "native" || 
    /^So1+$/.test(mint) || 
    (mint.length >= 32 && mint.length <= 44 && mint.startsWith("So1") && mint !== SOL_MINT)
  ) {
    return SOL_MINT;
  }
  return mint;
}

async function getPositionPnl(poolAddressStr, positionAddressStr) {
  const walletAddress = getWallet().publicKey.toString();
  const url = `https://dlmm.datapi.meteora.ag/positions/${poolAddressStr}/pnl?user=${walletAddress}&status=open&pageSize=100&page=1`;
  const res = await fetch(url);
  if (!res.ok) {
    throw new Error(`Meteora PnL API error: ${res.status}`);
  }
  const data = await res.json();
  const positions = data.positions || data.data || [];
  const found = positions.find(p => (p.positionAddress || p.address || p.position) === positionAddressStr);
  if (!found) {
    throw new Error(`Position ${positionAddressStr} not found in Meteora PnL API`);
  }
  
  const deposit = parseFloat(found.allTimeDeposits?.total?.sol || 0);
  const balancesSol = parseFloat(found.unrealizedPnl?.balancesSol || 0);
  const unclaimedFeeSol = parseFloat(found.unrealizedPnl?.unclaimedFeeTokenX?.amountSol || 0) + 
                           parseFloat(found.unrealizedPnl?.unclaimedFeeTokenY?.amountSol || 0);
  const withdrawalsSol = parseFloat(found.allTimeWithdrawals?.total?.sol || 0);
  const feesClaimedSol = parseFloat(found.allTimeFees?.total?.sol || 0);
  
  let derivedPnlPct = 0;
  if (deposit > 0) {
    derivedPnlPct = ((balancesSol + unclaimedFeeSol + withdrawalsSol + feesClaimedSol - deposit) / deposit) * 100;
  }
  
  return {
    success: true,
    pnl_pct: found.pnlSolPctChange != null ? parseFloat(found.pnlSolPctChange) : derivedPnlPct,
    derived_pnl_pct: derivedPnlPct,
    current_value_sol: balancesSol,
    unclaimed_fees_sol: unclaimedFeeSol,
    in_range: !found.isOutOfRange,
    active_bin: found.poolActiveBinId ?? null,
    lower_bin: found.lowerBinId ?? null,
    upper_bin: found.upperBinId ?? null,
    fee_per_tvl_24h: found.feePerTvl24h ? parseFloat(found.feePerTvl24h) : 0
  };
}

async function getSplBalance(tokenMintStr) {
  return await runWithFailover(async (connection) => {
    const wallet = getWallet();
    const token_mint = normalizeMint(tokenMintStr);
    if (token_mint === "So11111111111111111111111111111111111111112") {
      const balance = await connection.getBalance(wallet.publicKey);
      return {
        balance: balance / 1e9,
        decimals: 9
      };
    }
    const mintPubKey = new PublicKey(token_mint);
    const accounts = await connection.getParsedTokenAccountsByOwner(wallet.publicKey, {
      mint: mintPubKey
    });
    if (accounts.value.length === 0) {
      return { balance: 0, decimals: 0 };
    }
    let totalBalance = 0;
    let decimals = 0;
    for (const acc of accounts.value) {
      const tokenAmount = acc.account.data.parsed.info.tokenAmount;
      totalBalance += tokenAmount.uiAmount || 0;
      decimals = tokenAmount.decimals || 0;
    }
    return {
      balance: totalBalance,
      decimals: decimals
    };
  });
}

async function swapToken(inputMintStr, outputMintStr, amountFloat, maxPriceImpactPct = 5, slippageBps = 100) {
  const input_mint = normalizeMint(inputMintStr);
  const output_mint = normalizeMint(outputMintStr);

  if (process.env.DRY_RUN === "true") {
    console.warn(`[DRY RUN] Would swap ${amountFloat} of ${input_mint} to ${output_mint}`);
    return { success: true, dryRun: true, txHash: "DRY_RUN_SWAP_TX_HASH", inputAmount: "0", outputAmount: "0" };
  }

  // Fetch quote outside runWithFailover as it's a HTTP call to Jupiter, then execute/send via standard RPC rotation
  let quoteResponse;
  try {
    // 1. Get input decimals
    let decimals = 9;
    if (input_mint !== "So11111111111111111111111111111111111111112") {
      // We'll perform a quick connection just to fetch decimals
      await runWithFailover(async (connection) => {
        const mintInfo = await connection.getParsedAccountInfo(new PublicKey(input_mint));
        decimals = mintInfo.value?.data?.parsed?.info?.decimals ?? 9;
      });
    }
    const amountRaw = Math.floor(amountFloat * Math.pow(10, decimals)).toString();

    // 2. Fetch quote — retry with escalating slippage on failure (thin pools reject tight slippage)
    const slippageLadder = [slippageBps, slippageBps * 3, slippageBps * 8];
    let lastErr = null;
    for (const bps of slippageLadder) {
      try {
        const quoteUrl = `https://api.jup.ag/swap/v1/quote?inputMint=${input_mint}&outputMint=${output_mint}&amount=${amountRaw}&slippageBps=${bps}`;
        const quoteRes = await fetch(quoteUrl);
        if (!quoteRes.ok) {
          lastErr = `Jupiter quote API error: ${quoteRes.status} ${await quoteRes.text()}`;
          continue;
        }
        const q = await quoteRes.json();
        if (!q || !q.outAmount || q.outAmount === "0") {
          lastErr = `Jupiter returned empty quote at slippage ${bps}bps (no route/liquidity)`;
          continue;
        }
        quoteResponse = q;
        if (bps !== slippageBps) {
          console.warn(`[DLMM] Swap quote required elevated slippage ${bps}bps (thin liquidity)`);
        }
        break;
      } catch (e) {
        lastErr = e.message;
      }
    }
    if (!quoteResponse) {
      throw new Error(lastErr || "No Jupiter route found");
    }

    // 3. Price-impact guard — abort before signing if impact exceeds threshold (protects large exits on low-TVL pools)
    const impactPct = parseFloat(quoteResponse.priceImpactPct || "0") * 100;
    if (impactPct > maxPriceImpactPct) {
      return {
        success: false,
        aborted: true,
        reason: "price_impact_exceeded",
        priceImpactPct: impactPct,
        maxPriceImpactPct,
        error: `Swap aborted: price impact ${impactPct.toFixed(2)}% > max ${maxPriceImpactPct}%. Token left unswapped to avoid bad fill.`
      };
    }
    if (impactPct > 1) {
      console.warn(`[DLMM] Swap price impact ${impactPct.toFixed(2)}% (within ${maxPriceImpactPct}% limit)`);
    }
  } catch (err) {
    throw new Error(`Failed to fetch quote from Jupiter: ${err.message}`);
  }

  return await runWithFailover(async (connection) => {
    const wallet = getWallet();
    
    // 3. Fetch swap transaction
    const swapRes = await fetch("https://api.jup.ag/swap/v1/swap", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        quoteResponse,
        userPublicKey: wallet.publicKey.toString(),
        wrapAndUnwrapSol: true
      })
    });
    if (!swapRes.ok) {
      throw new Error(`Jupiter swap API error: ${swapRes.status} ${await swapRes.text()}`);
    }
    const { swapTransaction } = await swapRes.json();
    
    // 4. Deserialize and sign
    const swapTransactionBuf = Buffer.from(swapTransaction, "base64");
    const transaction = VersionedTransaction.deserialize(swapTransactionBuf);
    
    const { blockhash } = await connection.getLatestBlockhash("confirmed");
    transaction.message.recentBlockhash = blockhash;
    transaction.sign([wallet]);
    
    // 5. Send and confirm
    const rawTransaction = transaction.serialize();
    const txid = await connection.sendRawTransaction(rawTransaction, {
      skipPreflight: true,
      maxRetries: 2
    });
    
    const latestBlockHash = await connection.getLatestBlockhash();
    await connection.confirmTransaction({
      blockhash: latestBlockHash.blockhash,
      lastValidBlockHeight: latestBlockHash.lastValidBlockHeight,
      signature: txid
    }, "confirmed");
    
    return {
      success: true,
      txHash: txid,
      inputAmount: quoteResponse.inAmount,
      outputAmount: quoteResponse.outAmount
    };
  });
}

async function main() {
  const args = process.argv.slice(2);
  const command = args[0];
  
  if (!command) {
    console.error("No command provided. Exposing: active-bin, deploy, check-bins, claim, close, positions, pnl, spl-balance, swap");
    process.exit(1);
  }
  
  try {
    if (command === "active-bin") {
      const pool = args[1];
      if (!pool) throw new Error("Usage: active-bin <pool_address>");
      const res = await getActiveBin(pool);
      console.log(JSON.stringify(res));
    } else if (command === "deploy") {
      const pool = args[1];
      let amountX = 0;
      let amountY = 0;
      let binsBelow = 0;
      let binsAbove = 0;
      let strategyType = "spot";
      let slippageBps = 1000;

      if (!pool) {
        throw new Error("Usage: deploy <pool_address> <amount_x> <amount_y> <bins_below> <bins_above> [strategy_type] [slippage_bps]\n   Or: deploy <pool_address> <amount_sol> <bins_below> [bins_above]");
      }

      // Auto-detect format by checking if 3rd arg is integer (e.g. bins_below in old format)
      const val2 = parseFloat(args[2]);
      const val3 = parseFloat(args[3]);
      const val3IsInt = Number.isInteger(val3);

      if (args.length <= 5 && val3IsInt) {
        // Old format: deploy <pool_address> <amount_sol> <bins_below> [bins_above]
        amountX = 0;
        amountY = val2;
        binsBelow = parseInt(args[3]);
        binsAbove = parseInt(args[4] || "0");
      } else {
        // New format: deploy <pool_address> <amount_x> <amount_y> <bins_below> <bins_above> [strategy_type] [slippage_bps]
        amountX = val2;
        amountY = val3;
        binsBelow = parseInt(args[4]);
        binsAbove = parseInt(args[5] || "0");
        strategyType = args[6] || "spot";
        slippageBps = parseInt(args[7] || "1000");
      }

      if (isNaN(amountX) || isNaN(amountY) || isNaN(binsBelow)) {
        throw new Error("Invalid numeric arguments for deploy command.");
      }

      const res = await deployPosition(pool, amountX, amountY, binsBelow, binsAbove, strategyType, slippageBps);
      console.log(JSON.stringify(res));
    } else if (command === "check-bins") {
      const pool = args[1];
      const binsBelow = parseInt(args[2]);
      const binsAbove = parseInt(args[3] || "0");
      if (!pool || isNaN(binsBelow)) throw new Error("Usage: check-bins <pool_address> <bins_below> [bins_above]");
      const res = await checkBinCoverage(pool, binsBelow, binsAbove);
      console.log(JSON.stringify(res));
    } else if (command === "claim") {
      const position = args[1];
      if (!position) throw new Error("Usage: claim <position_address>");
      const res = await claimFees(position);
      console.log(JSON.stringify(res));
    } else if (command === "close") {
      const position = args[1];
      if (!position) throw new Error("Usage: close <position_address>");
      // CHOKEPOINT: the raw close primitive must not be reachable directly. Every legitimate close
      // flows through dlmm_monitor.py (auto-rules or guarded --override-close), which sets
      // DLMM_CLOSE_AUTH=1 only after the health policy has been applied. A direct
      // `node dlmm_executor.js close <addr>` (gateway agent, shell, stray script) has no token and
      // is refused, so no actor can bypass the GUARD by calling the executor directly.
      // Escape hatches: --force argv flag (explicit manual intent) or DRY_RUN.
      const closeAuthorized =
        process.env.DLMM_CLOSE_AUTH === "1" ||
        process.env.DRY_RUN === "true" ||
        args.includes("--force");
      if (!closeAuthorized) {
        console.error(JSON.stringify({
          success: false,
          error: "CLOSE REFUSED: raw executor close is not authorized. Closes must go through dlmm_monitor.py (which applies the health GUARD and sets DLMM_CLOSE_AUTH). Pass --force for explicit manual override.",
        }));
        process.exit(3);
      }
      const res = await closePosition(position);
      console.log(JSON.stringify(res));
    } else if (command === "positions") {
      const wallet = args[1];
      const res = await getPositions(wallet);
      console.log(JSON.stringify(res));
    } else if (command === "pnl") {
      const pool = args[1];
      const position = args[2];
      if (!pool || !position) throw new Error("Usage: pnl <pool_address> <position_address>");
      const res = await getPositionPnl(pool, position);
      console.log(JSON.stringify(res));
    } else if (command === "spl-balance") {
      const token = args[1];
      if (!token) throw new Error("Usage: spl-balance <token_mint>");
      const res = await getSplBalance(token);
      console.log(JSON.stringify(res));
    } else if (command === "list-tokens") {
      const TOKEN_PROGRAM_ID = new PublicKey("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA");
      const TOKEN_2022_PROGRAM_ID = new PublicKey("TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb");
      const listWallet = getWallet();
      const tokens = await runWithFailover(async (connection) => {
        const [v1Accounts, v2Accounts] = await Promise.all([
          connection.getParsedTokenAccountsByOwner(listWallet.publicKey, { programId: TOKEN_PROGRAM_ID }),
          connection.getParsedTokenAccountsByOwner(listWallet.publicKey, { programId: TOKEN_2022_PROGRAM_ID }),
        ]);
        return [...v1Accounts.value, ...v2Accounts.value]
          .map(acc => {
            const info = acc.account.data.parsed.info;
            return {
              mint: info.mint,
              balance: info.tokenAmount.uiAmount || 0,
              decimals: info.tokenAmount.decimals || 0,
            };
          })
          .filter(t => t.balance > 0);
      });
      console.log(JSON.stringify({ success: true, tokens }));
    } else if (command === "swap") {
      const input = args[1];
      const output = args[2];
      const amount = parseFloat(args[3]);
      const maxImpact = args[4] != null ? parseFloat(args[4]) : 5;
      const slipBps = args[5] != null ? parseInt(args[5]) : 100;
      if (!input || !output || isNaN(amount)) {
        throw new Error("Usage: swap <input_mint> <output_mint> <amount> [max_price_impact_pct] [slippage_bps]");
      }
      const res = await swapToken(input, output, amount, maxImpact, slipBps);
      console.log(JSON.stringify(res));
    } else {
      throw new Error(`Unknown command: ${command}`);
    }
  } catch (err) {
    console.error(JSON.stringify({ success: false, error: err.message }));
    process.exit(1);
  }
}

main();
