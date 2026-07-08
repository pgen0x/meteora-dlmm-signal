#!/usr/bin/env python3
import sys
import json
import time
import subprocess
import urllib.request
import urllib.parse
import os
import re
from local_indicators import check_local_indicators
from tz_util import local_time_str

# Configuration
MIN_TVL_USD = 10000
MIN_FEE_TVL_24H = 1.0
MIN_ORGANIC_SCORE = 60
MIN_MCAP_USD = 150000
MIN_HOLDERS = 100
DEFAULT_DEPLOY_SOL = 0.5

# Mode defaults (overridden by SOUL.md per-mode blocks)
MODE_DEFAULTS = {
    "casual": {
        "MIN_TVL_USD": 5000.0,
        "MIN_FEE_TVL_24H": 0.3,
        "MIN_MCAP_USD": 250000.0,
        "MIN_HOLDERS": 500,
        "TIMEFRAME": "30m",
        "MAX_POSITIONS": 2,
    },
    "multiday": {
        "MIN_TVL_USD": 50000.0,
        "MIN_FEE_TVL_24H": 1.0,
        "MIN_MCAP_USD": 1000000.0,
        "MIN_HOLDERS": 1000,
        "TIMEFRAME": "24h",
        "MAX_POSITIONS": 2,
    },
    # Fee-capture mode: small high-base-fee (1%+) pools with fast TVL turnover.
    # Signals come from the Go daemon's turnover screen (internal/meteora/screen.go);
    # the 30m-window fee_tvl_ratio floor 0.15 ~= 7.2%/day pace.
    "turnover": {
        "MIN_TVL_USD": 5000.0,
        "MIN_FEE_TVL_24H": 0.15,
        "MIN_MCAP_USD": 1000000.0,
        "MIN_HOLDERS": 500,
        "TIMEFRAME": "30m",
        "MAX_POSITIONS": 2,
    },
}

MIN_BINS_BELOW = 40
MAX_BINS_BELOW = 100

# Pre-deploy depth gate: refuse entry if SOL->base impact at our size exceeds this,
# since a thin pool means the eventual exit swap would strand the token. Matches executor default.
MAX_PRICE_IMPACT_PCT = 5.0

SOL_MINT = "So11111111111111111111111111111111111111112"

# Resolved from this file's own location (<profile>/skills/solana-dlmm/scripts/) so the
# script works whether it's a copy or a symlink into a Hermes profile — no install-time
# path rewrite needed.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROFILE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(SCRIPT_DIR)))
EXECUTOR_PATH = os.path.join(SCRIPT_DIR, "dlmm_executor.js")

def load_soul_dlmm_params(mode="multiday"):
    mode_def = MODE_DEFAULTS.get(mode, MODE_DEFAULTS["multiday"])
    params = {
        "MIN_TVL_USD": mode_def["MIN_TVL_USD"],
        "MIN_FEE_TVL_24H": mode_def["MIN_FEE_TVL_24H"],
        "MIN_ORGANIC_SCORE": float(MIN_ORGANIC_SCORE),
        "MIN_MCAP_USD": mode_def["MIN_MCAP_USD"],
        "MIN_HOLDERS": mode_def["MIN_HOLDERS"],
        "TIMEFRAME": mode_def["TIMEFRAME"],
        "MAX_POSITIONS": mode_def["MAX_POSITIONS"],
        "STRATEGY": "stage_aware",
        "INDICATORS_ENABLED": False,
        "INDICATORS_PRESET": "supertrend_or_rsi",
        "SLIPPAGE_BPS": 1000,
        "MODE": mode,
    }
    
    soul_path = os.path.join(PROFILE_DIR, "SOUL.md")
    if not os.path.exists(soul_path):
        return params
        
    try:
        with open(soul_path, "r", encoding="utf-8") as f:
            content = f.read()
            
        lines = content.splitlines()
        sec9_lines = []
        in_section = False
        for line in lines:
            if line.strip().startswith("## 9."):
                in_section = True
                continue
            elif in_section and line.strip().startswith("## "):
                break
            if in_section:
                sec9_lines.append(line.strip())
                
        mode_prefix = mode.capitalize()  # "Casual" or "Multiday"
        for line in sec9_lines:
            if not line.startswith("*"):
                continue
            if ":" not in line:
                continue
            parts = line.split(":", 1)
            name = parts[0].replace("*", "").strip()
            value_part = parts[1].strip()

            # Mode-specific numeric params (e.g. "Casual Min TVL", "Multiday Min Mcap")
            if name.startswith(mode_prefix):
                num_match = re.search(r'(-?\d[\d,.]*)', value_part)
                if not num_match:
                    continue
                val_str = num_match.group(1).replace(",", "")
                try:
                    val = float(val_str)
                except ValueError:
                    continue
                if "Min TVL" in name:
                    params["MIN_TVL_USD"] = val
                elif "Min Fee" in name:
                    params["MIN_FEE_TVL_24H"] = val
                elif "Min Mcap" in name or "Min Market Cap" in name:
                    params["MIN_MCAP_USD"] = val
                elif "Min Holders" in name:
                    params["MIN_HOLDERS"] = int(val)
                elif "Max Positions" in name:
                    params["MAX_POSITIONS"] = int(val)
                continue

            # Shared string/boolean params (no mode prefix)
            if "Timeframe" in name and not any(name.startswith(p) for p in ("Casual", "Multiday")):
                continue  # ignore legacy shared timeframe — mode drives it
            elif "Strategy" in name and not any(name.startswith(p) for p in ("Casual", "Multiday")):
                token = re.split(r'[\s(]', value_part)[0].strip().lower()
                params["STRATEGY"] = token
                continue
            elif "Indicators Enabled" in name:
                params["INDICATORS_ENABLED"] = "true" in value_part.lower()
                continue
            elif "Indicators Preset" in name:
                token = re.split(r'[\s(]', value_part)[0].strip()
                params["INDICATORS_PRESET"] = token
                continue

            num_match = re.search(r'(-?\d[\d,.]*)', value_part)
            if not num_match:
                continue
            val_str = num_match.group(1).replace(",", "")
            try:
                val = float(val_str)
            except ValueError:
                continue

            if "Minimum Base Organic Score" in name:
                params["MIN_ORGANIC_SCORE"] = val
            elif "Slippage" in name:
                params["SLIPPAGE_BPS"] = int(val)
    except Exception as e:
        print(f"Error parsing SOUL.md parameters: {e}")
        
    return params

def run_command(cmd, timeout=120):
    try:
        res = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return res.stdout.strip(), res.stderr.strip(), res.returncode
    except Exception as e:
        return "", str(e), -1

def run_command_json(cmd):
    out, err, code = run_command(cmd)
    if code != 0 or not out:
        return None, err or "No output from command execution"
    try:
        json_line = None
        for line in reversed(out.split('\n')):
            stripped = line.strip()
            if (stripped.startswith('{') and stripped.endswith('}')) or (stripped.startswith('[') and stripped.endswith(']')):
                json_line = stripped
                break
        if not json_line:
            return None, f"No JSON object or array found in output. Raw: {out}"
        return json.loads(json_line), None
    except Exception as e:
        return None, f"JSON parse error: {e}. Raw: {out}"

def get_wallet_sol_balance():
    if os.environ.get("DRY_RUN") == "true":
        return 10.0
    try:
        data, err = run_command_json(f"node {EXECUTOR_PATH} spl-balance SOL")
        if data and "balance" in data:
            return float(data.get("balance", 0.0))
    except Exception as e:
        print(f"Warning: Error fetching SOL balance via executor: {e}")
        
    try:
        out, _, _ = run_command("solana balance")
        if out:
            return float(out.split()[0])
    except:
        pass
    return 10.0 # Fallback for local testing

def get_open_positions():
    # Primary: Meteora Portfolio API (handles wide-range positions SDK misses)
    try:
        env_path = os.path.join(PROFILE_DIR, ".env")
        wallet = None
        with open(env_path) as f:
            for line in f:
                if line.startswith("SOLANA_PUBLIC_KEY="):
                    wallet = line.split("=", 1)[1].strip().strip('"\'')
                    break
        if wallet:
            url = f"https://dlmm.datapi.meteora.ag/portfolio/open?user={wallet}"
            req = urllib.request.Request(url, headers={"User-Agent": "dlmm-lp/1.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
            positions = []
            for pool_data in (data.get("pools") or []):
                pool_addr = pool_data.get("poolAddress")
                for pos_addr in (pool_data.get("listPositions") or []):
                    positions.append({"position": pos_addr, "pool": pool_addr})
            # Enrich with Redis metadata (base_mint etc)
            for pos in positions:
                meta_raw, _, _ = run_command(f"redis-cli get \"sol:dlmm:position:{pos['position']}\"")
                if meta_raw and meta_raw != "(nil)":
                    try:
                        meta = json.loads(meta_raw)
                        pos.update({k: meta[k] for k in ("base_mint", "base_symbol", "pair") if k in meta})
                    except Exception:
                        pass
            if positions:
                return positions
    except Exception as e:
        print(f"Warning: Portfolio API failed in get_open_positions: {e}")
    # Fallback: SDK
    data, err = run_command_json(f"node {EXECUTOR_PATH} positions")
    return data if isinstance(data, list) else []

def fetch_top_pools(timeframe="24h", page_size=50):
    url = f"https://pool-discovery-api.datapi.meteora.ag/pools?page_size={page_size}&timeframe={timeframe}&category=trending"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
            return data.get("data", [])
    except Exception as e:
        print(f"Error fetching pools: {e}")
        return []

# local indicators are imported and check_local_indicators is used directly below.

def check_smart_wallets_on_pool(pool_address):
    path = os.path.join(PROFILE_DIR, "smart_wallets.json")
    if not os.path.exists(path):
        return 0
    try:
        with open(path, "r") as f:
            data = json.load(f)
        wallets = data.get("wallets", [])
        present_count = 0
        # Check top 3 wallets for performance reasons
        for w in wallets[:3]:
            addr = w.get("address")
            if not addr:
                continue
            out, err, code = run_command(f"node {EXECUTOR_PATH} positions {addr}")
            if code == 0 and out:
                pos_list = json.loads(out)
                for pos in pos_list:
                    if pos.get("pool") == pool_address:
                        present_count += 1
                        break
        if present_count > 0:
            print(f"👥 Smart Wallets: found {present_count} tracked wallet(s) active in pool {pool_address[:8]}")
        return present_count
    except Exception as e:
        print(f"Warning: Failed to check smart wallets: {e}")
        return 0


def get_current_active_positions_count():
    out, _, _ = run_command("redis-cli scard sol:dlmm:active_positions")
    try:
        return int(out) if out else 0
    except:
        return 0

def get_active_positions_count_for_mode(mode):
    """Count only positions tagged with the given mode. Untagged positions count as multiday."""
    out, _, _ = run_command("redis-cli smembers sol:dlmm:active_positions")
    if not out or out == "(empty set)":
        return 0
    addresses = [a.strip() for a in out.strip().split('\n') if a.strip()]
    count = 0
    for addr in addresses:
        data_out, _, _ = run_command(f"redis-cli get \"sol:dlmm:position:{addr}\"")
        if data_out and data_out != "(nil)":
            try:
                d = json.loads(data_out)
                pos_mode = d.get("mode", "multiday")
                if pos_mode == mode:
                    count += 1
            except Exception:
                pass
        else:
            # No Redis metadata — treat as multiday (legacy positions)
            if mode == "multiday":
                count += 1
    return count


def reconcile_redis_vs_meteora():
    """Prune active_positions set and orphan keys against Meteora API truth. Grace: skip positions deployed <2h ago."""
    try:
        env_path = os.path.join(PROFILE_DIR, ".env")
        wallet = None
        with open(env_path) as f:
            for line in f:
                if line.startswith("SOLANA_PUBLIC_KEY="):
                    wallet = line.split("=", 1)[1].strip().strip('"\'')
                    break
        if not wallet:
            return
        url = f"https://dlmm.datapi.meteora.ag/portfolio/open?user={wallet}"
        req = urllib.request.Request(url, headers={"User-Agent": "dlmm-lp/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        on_chain = set()
        for pool_data in (data.get("pools") or []):
            for pos_addr in (pool_data.get("listPositions") or []):
                on_chain.add(pos_addr)

        out, _, _ = run_command("redis-cli smembers sol:dlmm:active_positions")
        active_set = set(a.strip() for a in out.strip().split('\n') if a.strip()) if out and out != "(empty set)" else set()
        now = int(time.time())

        # Remove from active set any position not on-chain (with grace period)
        for addr in active_set - on_chain:
            meta_raw, _, _ = run_command(f"redis-cli get \"sol:dlmm:position:{addr}\"")
            deployed_at = 0
            if meta_raw and meta_raw != "(nil)":
                try:
                    deployed_at = json.loads(meta_raw).get("deployed_at", 0)
                except Exception:
                    pass
            if now - deployed_at < 7200:
                print(f"[reconcile] {addr[:20]}... not on-chain but deployed <2h ago — skipping")
                continue
            print(f"[reconcile] Removing orphan from active set: {addr[:20]}...")
            run_command(f"redis-cli srem sol:dlmm:active_positions \"{addr}\"")
            for suffix in ("", ":oor_since", ":indicator_blocked_since", ":ai_hold_until"):
                run_command(f"redis-cli del \"sol:dlmm:position:{addr}{suffix}\"")

        # Clean orphan base/meta keys not in active set and not on-chain
        meta_suffixes = (":oor_since", ":indicator_blocked_since", ":ai_hold_until")
        keys_out, _, _ = run_command("redis-cli keys \"sol:dlmm:position:*\"")
        if keys_out and keys_out.strip() and keys_out.strip() != "(empty array)":
            for key in keys_out.strip().split('\n'):
                key = key.strip()
                if not key:
                    continue
                addr = key.replace("sol:dlmm:position:", "")
                if any(addr.endswith(s) for s in meta_suffixes):
                    base_addr = addr.rsplit(":", 1)[0]
                    if base_addr not in on_chain and base_addr not in active_set:
                        run_command(f"redis-cli del \"{key}\"")
                else:
                    if addr not in on_chain and addr not in active_set:
                        deployed_at = 0
                        meta_raw, _, _ = run_command(f"redis-cli get \"{key}\"")
                        if meta_raw and meta_raw != "(nil)":
                            try:
                                deployed_at = json.loads(meta_raw).get("deployed_at", 0)
                            except Exception:
                                pass
                        if now - deployed_at >= 7200:
                            run_command(f"redis-cli del \"{key}\"")
    except Exception as e:
        print(f"[reconcile] Warning: {e}")


def compute_deploy_amount(wallet_sol):
    reserve = 0.2
    pct = 0.45
    floor = 0.3
    ceil = 5.0
    deployable = max(0.0, wallet_sol - reserve)
    dynamic = deployable * pct
    if dynamic < floor:
        return max(0.1, round(dynamic, 2)) if wallet_sol - reserve >= 0.1 else 0.0
    result = min(ceil, dynamic)
    return round(result, 2)
def check_bin_coverage(pool, bins_below, bins_above):
    """Read-only: would deploying [active-bins_below, active+bins_above] need NEW
    Meteora bin-array init (~0.071 SOL each, non-refundable)? Returns the executor
    check-bins dict, or None if the check can't run. Callers FAIL OPEN on None — a
    transient RPC error must never drop a good pool; the executor's deploy-time guard
    is the final backstop. Spends nothing."""
    try:
        data, _ = run_command_json(f"node {EXECUTOR_PATH} check-bins {pool} {bins_below} {bins_above}")
        if data and data.get("success"):
            return data
        return None
    except Exception:
        return None


def search_assets_by_symbol(symbol):
    if not symbol:
        return []
    url = f"https://datapi.jup.ag/v1/assets/search?query={urllib.parse.quote(symbol)}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            return data if isinstance(data, list) else [data]
    except Exception as e:
        print(f"Error searching Jupiter assets for symbol {symbol}: {e}")
        return []

def find_rival_pool(mint):
    if not mint:
        return None
    url = f"https://dlmm.datapi.meteora.ag/pools?query={urllib.parse.quote(mint)}&sort_by=tvl:desc&filter_by=tvl%3E5000"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            pools = data.get("data", [])
            for p in pools:
                if p.get("token_x", {}).get("address") == mint or p.get("token_y", {}).get("address") == mint:
                    return p
    except Exception as e:
        print(f"Error finding rival pool for mint {mint}: {e}")
    return None

def check_pvp_conflict(base_symbol, base_mint):
    if not base_symbol or not base_mint:
        return False
    assets = search_assets_by_symbol(base_symbol)
    norm_symbol = base_symbol.strip().upper()
    
    # Filter for assets with same symbol but different mint
    rivals = []
    for asset in assets:
        asset_symbol = asset.get("symbol", "").strip().upper()
        if asset_symbol.startswith("$"):
            asset_symbol = asset_symbol[1:]
        clean_base = norm_symbol
        if clean_base.startswith("$"):
            clean_base = clean_base[1:]
            
        if asset_symbol == clean_base and asset.get("id") != base_mint:
            rivals.append(asset)
            
    # Check each rival
    for rival in rivals[:2]:
        holders = int(rival.get("holderCount", 0) or 0)
        if holders < 500:
            continue
        rival_mint = rival.get("id")
        rival_pool = find_rival_pool(rival_mint)
        if rival_pool:
            print(f"⚠️ PvP conflict detected: symbol {base_symbol} has active rival token {rival_mint} with {holders} holders and active DLMM pool")
            return True
            
    return False

def get_price_impact_sol_to_token(base_mint, sol_amount):
    """Jupiter quote SOL->base for sol_amount SOL; returns price impact percent, or None.
    Proxy for pool depth at our position size: high impact => thin pool => costly/stranded exit.
    Same Jupiter host the executor uses (api.jup.ag/swap/v1/quote)."""
    if not base_mint or os.environ.get("DRY_RUN") == "true":
        return None
    try:
        amount_raw = int(sol_amount * 1e9)  # SOL = 9 decimals
        url = (f"https://api.jup.ag/swap/v1/quote?inputMint={SOL_MINT}"
               f"&outputMint={urllib.parse.quote(base_mint)}&amount={amount_raw}&slippageBps=1000")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        pi = data.get("priceImpactPct")
        if pi is None or not data.get("outAmount"):
            return None
        return float(pi) * 100
    except Exception as e:
        print(f"Warning: price impact fetch failed for {base_mint[:8]}: {e}")
        return None

def fetch_live_fee_tvl(pool_address, timeframe="24h"):
    """Re-query Meteora for this pool's CURRENT fee/TVL ratio (percent) at deploy time.
    Apples-to-apples with the screened Meteora value. Returns None on failure.
    dlmm.datapi returns fee_tvl_ratio as a {timeframe: float} dict."""
    if not pool_address:
        return None
    url = f"https://dlmm.datapi.meteora.ag/pools?query={urllib.parse.quote(pool_address)}&sort_by=tvl:desc"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        pools = data.get("data", [])
        if not pools:
            return None
        ratio = pools[0].get("fee_tvl_ratio")
        if isinstance(ratio, dict):
            # Prefer requested timeframe, fall back to longer windows.
            for tf in (timeframe, "24h", "12h", "1h"):
                if ratio.get(tf) is not None:
                    return float(ratio[tf])
            return None
        return float(ratio) if ratio is not None else None
    except Exception as e:
        print(f"Warning: live fee/TVL fetch failed for {pool_address[:8]}: {e}")
        return None

# Entry trend gates (multi-timeframe). A token can show positive h1 (dead-cat bounce)
# while bleeding on the higher timeframes — LPing into that = a falling knife.
# These reject sustained downtrends before ranking. Hardcoded like the m5/h1 screen below.
MAX_ENTRY_H6_DROP = -12.0    # reject if 6h price change below this
MAX_ENTRY_H24_DROP = -25.0   # reject if 24h price change below this

def get_momentum(mint):
    """Returns (m5_pct, h1_pct, h6_pct, h24_pct) price change from DexScreener, or
    (None, None, None, None) on failure. Screens ALL candidates, not just the winner."""
    if not mint or os.environ.get("DRY_RUN") == "true":
        return None, None, None, None
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())
        pairs = data.get("pairs") or []
        if not pairs:
            return None, None, None, None
        pc = pairs[0].get("priceChange") or {}
        m5 = float(pc.get("m5", 0) or 0)
        h1 = float(pc.get("h1", 0) or 0)
        h6 = float(pc.get("h6", 0) or 0)
        h24 = float(pc.get("h24", 0) or 0)
        return m5, h1, h6, h24
    except Exception as e:
        print(f"Warning: momentum fetch failed for {mint[:8]}: {e}")
        return None, None, None, None

TIMEFRAME_SCREENING_SCALES = {
    "5m":   { "minFeeActiveTvlRatio": 0.02, "minVolume": 500 },
    "30m":  { "minFeeActiveTvlRatio": 0.15, "minVolume": 2000 },
    "1h":   { "minFeeActiveTvlRatio": 0.2,  "minVolume": 10000 },
    "2h":   { "minFeeActiveTvlRatio": 0.4,  "minVolume": 20000 },
    "4h":   { "minFeeActiveTvlRatio": 0.8,  "minVolume": 40000 },
    "12h":  { "minFeeActiveTvlRatio": 1.0,  "minVolume": 80000 },
    "24h":  { "minFeeActiveTvlRatio": 1.0,  "minVolume": 500000 }
}

def get_scaled_thresholds(timeframe, min_fee_tvl):
    scale = TIMEFRAME_SCREENING_SCALES.get(timeframe)
    if scale:
        scaled_fee = max(min_fee_tvl, scale["minFeeActiveTvlRatio"])
        scaled_vol = scale["minVolume"]
        return scaled_fee, scaled_vol
    return min_fee_tvl, 10000

# Capital-stage strategy framework: strategy + bin width scale with position size.
# Stage 1 (<1 SOL): tight single-side SOL spot, 15-30 bins — capture chop, fast gains.
# Stage 2 (1-10 SOL): volatility-physics symmetric spot — balanced in-range time (legacy default).
# Stage 3 (10+ SOL): wide single-side SOL spot — deep downside catch.
STAGE1_MAX_SOL = 1.0
STAGE2_MAX_SOL = 10.0

def select_stage_strategy(deploy_sol, volatility, bin_step, mode=None):
    """Return (stage_label, bins_below, bins_above, strategy_type) for the capital stage.
    SOL-only deploy (no pre-swap). Maps deploy size -> the strategy that fits it.

    Bin shape: spot spreads liquidity evenly; bid_ask weights it toward the
    range edges. For a SOL-only deploy every strategy fills bid-side bins
    below the active price, so bid_ask concentrates size deeper into the dip
    — bigger fills and more fees on the wicks we are positioned to catch."""
    bin_step_pct = max(bin_step, 1) / 100.0
    if mode == "turnover":
        # Fee-capture pools oscillate hard around the active bin; edge-weighted
        # liquidity (bid_ask) earns more per swing than flat spot at equal width.
        phys = int((volatility * 1.2) / bin_step_pct) if bin_step_pct > 0 else 20
        bins = max(15, min(30, phys or 20))
        return ("turnover_tight_bidask", bins, bins, "bid_ask")
    if deploy_sol < STAGE1_MAX_SOL:
        phys = int((volatility * 1.2) / bin_step_pct) if bin_step_pct > 0 else 20
        bins = max(15, min(30, phys or 20))
        return ("stage1_tight_spot", bins, bins, "spot")
    elif deploy_sol < STAGE2_MAX_SOL:
        phys = int((volatility * 2.5) / bin_step_pct)
        lin = int(MIN_BINS_BELOW + (volatility / 5.0) * (MAX_BINS_BELOW - MIN_BINS_BELOW))
        bins = max(MIN_BINS_BELOW, min(MAX_BINS_BELOW, max(phys, lin // 2)))
        return ("stage2_medium_spot", bins, bins, "spot")
    else:
        # Wide single-side dip catch: bid_ask stacks the deep bins, so a hard
        # flush fills into size instead of the thin tail spot would leave there.
        return ("stage3_wide_bidask", MAX_BINS_BELOW, 0, "bid_ask")

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--analyze-only", action="store_true", help="Screen pools, print all candidates JSON, exit without deploying")
    parser.add_argument("--strategy", type=str, default=None, help="Override SOUL.md strategy (spot, custom_ratio_spot, single_sided_reseed, fee_compounding, partial_harvest)")
    parser.add_argument("--pool", type=str, default=None, help="Deploy a specific pool address instead of auto-selecting winner")
    parser.add_argument("--from-signal", dest="from_signal", type=str, default=None, help="JSON of a pre-screened candidate record from the mdtb signal daemon. Skips discovery+screen and deploys this exact pool; live gates (holding/cooldown/momentum/rent) still run.")
    parser.add_argument("--mode", type=str, default="multiday", choices=["casual", "multiday", "turnover"], help="Pipeline mode: casual (30m, 2-6h plays), multiday (24h, 24h+ holds) or turnover (30m, high-fee fee-capture plays)")
    cli = parser.parse_args()

    mode = cli.mode
    print(f"🔍 Starting DLMM Ingestion Pipeline [{mode.upper()} mode]")

    params = load_soul_dlmm_params(mode=mode)
    min_tvl = params["MIN_TVL_USD"]
    min_fee_tvl = params["MIN_FEE_TVL_24H"]
    min_organic = params["MIN_ORGANIC_SCORE"]
    min_mcap = params["MIN_MCAP_USD"]
    min_holders = params["MIN_HOLDERS"]
    timeframe = params.get("TIMEFRAME", "24h")
    max_positions = params.get("MAX_POSITIONS", 2)

    scaled_min_fee, scaled_min_vol = get_scaled_thresholds(timeframe, min_fee_tvl)
    print(f"Mode={mode} Timeframe={timeframe} | TVL>=${min_tvl:,.0f} Fee/TVL>={scaled_min_fee:.2f}% Organic>={min_organic:.0f} Mcap>=${min_mcap:,.0f} Holders>={min_holders} MaxPos={max_positions}")

    # 1. Hard checks — reconcile Redis vs Meteora API before slot count
    reconcile_redis_vs_meteora()

    # Mode-isolated slot count (casual and multiday each have their own budget)
    # Skip slot check on --analyze-only so screening works even when slots are full
    if not cli.analyze_only:
        active_count = get_active_positions_count_for_mode(mode)
        if active_count >= max_positions:
            print(f"Aborting: Max {mode} positions reached ({active_count}/{max_positions})")
            sys.exit(0)
    else:
        active_count = get_active_positions_count_for_mode(mode)
        print(f"Slots: {active_count}/{max_positions} {mode} positions active (analyze-only, not blocking)")

    sol_balance = get_wallet_sol_balance()
    # Hard gate: abort entire pipeline (including analyze-only) if wallet too thin to deploy anything useful
    if sol_balance < 0.25 and os.environ.get("DRY_RUN") != "true":
        print(f"[SKIP] Wallet {sol_balance:.3f} SOL < 0.25 SOL minimum — aborting pipeline (no SOL to deploy)")
        sys.exit(0)
    deploy_sol = compute_deploy_amount(sol_balance)
    if (deploy_sol <= 0 or deploy_sol < 0.10) and not cli.analyze_only:
        print(f"Aborting: deploy amount {deploy_sol:.3f} SOL below 0.10 SOL minimum (wallet {sol_balance:.3f} SOL)")
        sys.exit(0)
    min_required = deploy_sol + 0.05
    if sol_balance < min_required and not cli.analyze_only:
        print(f"Aborting: Insufficient SOL balance ({sol_balance:.3f} SOL < {min_required:.3f} SOL required)")
        sys.exit(0)

    # 2. Fetch candidates
    if cli.from_signal:
        # mdtb signal daemon already discovered + screened this pool; deploy the
        # forwarded record directly. Skipping our own discovery/screen removes the
        # divergence between two independent trending snapshots that used to cause
        # "--pool ... not found in valid candidates. Aborting." The live gates below
        # (open positions, cooldown, momentum, bin-array rent) still run.
        try:
            signal_record = json.loads(cli.from_signal)
        except Exception as e:
            print(f"Aborting: --from-signal is not valid JSON: {e}")
            sys.exit(1)
        # The record must be one payload element (docs/SIGNAL_SCHEMA.md), not the
        # whole signal or its payload array. Validate the keys the pipeline
        # accesses unconditionally so a malformed record aborts here with a clear
        # message instead of a KeyError deep in ranking/deploy.
        if isinstance(signal_record, list):
            print("Aborting: --from-signal got an array; pass ONE payload element (a single pool record)")
            sys.exit(1)
        required = ("pool", "name", "base_mint", "base_symbol", "tvl", "volatility", "score")
        missing = [k for k in required if k not in signal_record]
        if missing:
            print(f"Aborting: --from-signal record missing required fields: {', '.join(missing)} (see docs/SIGNAL_SCHEMA.md)")
            sys.exit(1)
        candidates = [signal_record]
        print(f"Using signalled candidate {candidates[0].get('name')} ({candidates[0].get('pool')}) — discovery/screen skipped")
        pools = []
    else:
        pools = fetch_top_pools(timeframe)
        if not pools:
            print("No candidates fetched from Meteora Pool Discovery API")
            sys.exit(0)
        candidates = []

    # 3. Filter candidates (loop no-ops for --from-signal since pools == [])
    for p in pools:
        token_x = p.get("token_x", {})
        token_y = p.get("token_y", {})
        
        # Verify which token is SOL and track orientation.
        # sol_is_x drives which amount slot SOL occupies at deploy (executor: amountX->tokenX).
        if token_y.get("address") == SOL_MINT:
            base_token = token_x
            quote_token = token_y
            sol_is_x = False
        elif token_x.get("address") == SOL_MINT:
            base_token = token_y
            quote_token = token_x
            sol_is_x = True
        else:
            continue  # non-SOL pool
            
        pool_address = p.get("pool_address")
        pool_name = p.get("name", "Unknown")
        
        tvl = float(p.get("tvl", 0))
        fee_tvl_ratio = float(p.get("fee_tvl_ratio", 0))
        fee_active_tvl_ratio = float(p.get("fee_active_tvl_ratio", 0))
        fee_tvl_ratio_change_pct = float(p.get("fee_tvl_ratio_change_pct", 0))
        volatility = float(p.get("volatility", 0))
        bin_step = int((p.get("dlmm_params") or {}).get("bin_step", 0))
        
        base_organic = float(base_token.get("organic_score", 0))
        base_mcap = float(base_token.get("market_cap", 0))
        base_holders = int(base_token.get("holders", 0))
        
        if tvl < min_tvl:
            continue
        if fee_tvl_ratio < scaled_min_fee:
            continue
        daily_fee_usd = tvl * fee_tvl_ratio / 100.0
        # Absolute fee floor scales with timeframe: 30m pools are naturally small-TVL
        min_daily_fee = 20.0 if timeframe in ("5m", "30m", "1h") else 150.0
        if daily_fee_usd < min_daily_fee:
            print(f"Skipping {pool_name} - absolute daily fees ${daily_fee_usd:.0f} too low (<${min_daily_fee:.0f}/day)")
            continue
        if volatility <= 0:
            continue
        if volatility > 15:
            print(f"Skipping {pool_name} - volatility {volatility:.2f} too high (>15), IL risk exceeds fee capture")
            continue
        if base_organic < min_organic:
            continue
        if base_mcap < min_mcap:
            continue
        if base_holders < min_holders:
            continue
        if fee_tvl_ratio_change_pct < -40.0:
            print(f"Skipping {pool_name} - yield declining {fee_tvl_ratio_change_pct:.0f}% (fee/TVL falling fast)")
            continue

        # Supply concentration safety gates
        top_holders_pct = float(base_token.get("top_holders_pct", 0) or 0)
        dev_balance_pct = float(base_token.get("dev_balance_pct", 0) or 0)
        if top_holders_pct > 60.0:
            print(f"Skipping {pool_name} - Base token top 10 holders own {top_holders_pct:.1f}% (>60%)")
            continue
        if dev_balance_pct > 20.0:
            print(f"Skipping {pool_name} - Base token dev owns {dev_balance_pct:.1f}% (>20%)")
            continue

        # Critical warnings and authority gates
        has_freeze = base_token.get("has_freeze_authority", False)
        has_mint = base_token.get("has_mint_authority", False)
        if has_freeze:
            print(f"Skipping {pool_name} - Base token has freeze authority enabled")
            continue
        if has_mint:
            print(f"Skipping {pool_name} - Base token has mint authority enabled")
            continue

        # verified + jupshield gates (fail open if field absent — API may not always return them)
        is_verified = base_token.get("verified", True)
        if is_verified is False:
            print(f"Skipping {pool_name} - Base token not verified")
            continue
        jup_shield = base_token.get("jupshield_verified", base_token.get("jup_shield", True))
        if jup_shield is False:
            print(f"Skipping {pool_name} - Base token failed Jupiter shield")
            continue

        warnings = base_token.get("warnings", [])
        has_crit = False
        for w in warnings:
            if w.get("severity") in ["critical", "warning"]:
                has_crit = True
                print(f"Skipping {pool_name} - Base token has critical warning: {w.get('message')}")
                break
        if has_crit:
            continue
            
        # Check smart wallets on this pool to boost score
        smart_wallets_count = check_smart_wallets_on_pool(pool_address)
        # Base score: organic + active-TVL yield + smart-wallet presence, minus volatility (IL risk).
        # fee_active_tvl_ratio used over fee_tvl_ratio: measures fees vs only active liquidity near
        # current bin — more accurate signal of real yield per capital at risk.
        # Momentum term added later in the valid_candidates loop once m5/h1 are fetched.
        score = base_organic + (fee_active_tvl_ratio * 10) + (smart_wallets_count * 15) - (volatility * 1.5)
        if fee_tvl_ratio_change_pct > 30:
            score += 10  # yield accelerating — pool gaining traction

        candidates.append({
            "pool": pool_address,
            "name": pool_name,
            "base_symbol": base_token.get("symbol"),
            "base_mint": base_token.get("address"),
            "tvl": tvl,
            "fee_tvl_ratio": fee_tvl_ratio,
            "fee_active_tvl_ratio": fee_active_tvl_ratio,
            "fee_tvl_ratio_change_pct": fee_tvl_ratio_change_pct,
            "volatility": volatility,
            "bin_step": bin_step,
            "organic_score": base_organic,
            "mcap": base_mcap,
            "holders": base_holders,
            "sol_is_x": sol_is_x,
            "score": score
        })

    candidates.sort(key=lambda x: x["score"], reverse=True)
    print(f"Surviving candidates: {len(candidates)}")
    for c in candidates[:5]:
        print(f"- {c['name']} (Score: {c['score']:.1f}, TVL: ${c['tvl']:,.0f}, Vol: {c['volatility']:.2f})")

    if not candidates:
        print("No pools survived safety filters.")
        sys.exit(0)

    # 4. Check already open positions — filter valid candidates
    open_pos = get_open_positions()
    open_pools = {op["pool"] for op in open_pos}
    open_base_mints = {op.get("base_mint") for op in open_pos if op.get("base_mint")}
    open_base_symbols = {op.get("base_symbol", "").upper() for op in open_pos if op.get("base_symbol")}

    valid_candidates = []
    for c in candidates:
        if c["pool"] in open_pools:
            print(f"Skipping {c['name']} - already have an active position in this pool")
            continue
        if c["base_mint"] and c["base_mint"] in open_base_mints:
            print(f"Skipping {c['name']} - already exposed to token {c['base_symbol']} (mint match)")
            continue
        if c["base_symbol"].upper() in open_base_symbols:
            print(f"Skipping {c['name']} - already exposed to token {c['base_symbol']} (symbol match)")
            continue
        if check_pvp_conflict(c["base_symbol"], c["base_mint"]):
            print(f"Skipping {c['name']} - PvP symbol conflict detected")
            continue
        cooldown_val, _, _ = run_command(f"redis-cli get \"sol:dlmm:cooldown:{c['base_symbol'].upper()}\"")
        if cooldown_val and cooldown_val != "(nil)":
            ttl_out, _, _ = run_command(f"redis-cli ttl \"sol:dlmm:cooldown:{c['base_symbol'].upper()}\"")
            ttl_mins = int(ttl_out) // 60 if ttl_out and ttl_out.lstrip("-").isdigit() else "?"
            print(f"Skipping {c['name']} - re-entry cooldown active ({ttl_mins}m remaining, reason: {cooldown_val[:60]})")
            continue
        # Pool-level cooldown (repeat-deploy churn guard, set post-deploy below).
        pool_cd, _, _ = run_command(f"redis-cli get \"sol:dlmm:cooldown:pool:{c['pool']}\"")
        if pool_cd and pool_cd != "(nil)":
            print(f"Skipping {c['name']} - pool cooldown active (reason: {pool_cd[:60]})")
            continue
        # Pool memory: closes on this exact pool are journaled to
        # sol:dlmm:history:pool:<pool> by the monitor. Two or more past closes
        # that net out negative = this pool has already cost us — hard skip.
        # No history (fresh pool) passes untouched.
        hist_raw, _, _ = run_command(f"redis-cli lrange \"sol:dlmm:history:pool:{c['pool']}\" 0 9")
        if hist_raw and hist_raw != "(nil)":
            past_pnls = []
            for line in hist_raw.splitlines():
                try:
                    past_pnls.append(float(json.loads(line).get("pnl_pct", 0)))
                except (ValueError, json.JSONDecodeError):
                    continue
            if len(past_pnls) >= 2 and sum(past_pnls) < 0:
                print(f"Skipping {c['name']} - pool memory: {len(past_pnls)} past closes net {sum(past_pnls):+.1f}% PnL")
                continue
        # Momentum screen across ALL candidates (not just deploy winner).
        # Filters dumping tokens before ranking so the winner isn't a falling knife.
        m5, h1, h6, h24 = get_momentum(c["base_mint"])
        c["m5"] = m5
        c["h1"] = h1
        c["h6"] = h6
        c["h24"] = h24
        if m5 is not None and m5 < -5.0:
            print(f"Skipping {c['name']} - dumping {m5:.1f}% in 5m (momentum screen)")
            continue
        if h1 is not None and h1 < -15.0:
            print(f"Skipping {c['name']} - 1h trend {h1:.1f}% < -15% (momentum screen)")
            continue
        # Multi-timeframe trend gate: reject sustained downtrends even when h1 looks positive
        # (dead-cat bounce). This is the gate that would have rejected the Joby -30% h24 entry.
        if h6 is not None and h6 < MAX_ENTRY_H6_DROP:
            print(f"Skipping {c['name']} - 6h trend {h6:.1f}% < {MAX_ENTRY_H6_DROP}% (downtrend gate)")
            continue
        if h24 is not None and h24 < MAX_ENTRY_H24_DROP:
            print(f"Skipping {c['name']} - 24h trend {h24:.1f}% < {MAX_ENTRY_H24_DROP}% (downtrend gate)")
            continue
        # Momentum term: reward up-trend, penalize weak. Clamped so it tunes ranking,
        # never dominates organic/yield. h1 weighted heavier than noisy m5.
        mom_adj = 0.0
        if h1 is not None:
            mom_adj += max(-10.0, min(10.0, h1))
        if m5 is not None:
            mom_adj += max(-5.0, min(5.0, m5)) * 0.5
        c["score"] += mom_adj
        c["momentum_adj"] = round(mom_adj, 1)
        valid_candidates.append(c)

    if not valid_candidates:
        print("No candidates available for deployment (already exposed to all winners).")
        sys.exit(0)

    # Re-rank after momentum adjustment so winner / analyze-only reflect final score.
    valid_candidates.sort(key=lambda x: x["score"], reverse=True)
    print("Re-ranked valid candidates (post-momentum):")
    for c in valid_candidates[:5]:
        print(f"- {c['name']} (Score: {c['score']:.1f}, Vol: {c['volatility']:.2f}, "
              f"m5: {c.get('m5')}, h1: {c.get('h1')}, mom_adj: {c.get('momentum_adj', 0)})")

    # --analyze-only: output valid candidates for AI review, then exit.
    # Candidates whose deploy range would require NEW bin-array init (non-refundable
    # rent) are excluded here so the AI only ever picks deployable, rent-free pools.
    # Ranking/scoring is untouched — this is a pure deployability filter on top of it,
    # so the best *deployable* candidate still wins. Fails OPEN: a pool is dropped only
    # when the read-only check positively confirms it needs init.
    if cli.analyze_only:
        soul_strat = params.get("STRATEGY", "spot")
        bins_by_candidate = {}
        deployable_candidates = []
        for c in valid_candidates:
            if soul_strat == "stage_aware":
                _, bb, _, _ = select_stage_strategy(deploy_sol, c["volatility"], c.get("bin_step", 100), mode=mode)
            else:
                c_bin_step_pct = max(c.get("bin_step", 100), 1) / 100.0
                c_phys = int((c["volatility"] * 2.5) / c_bin_step_pct)
                c_lin = int(MIN_BINS_BELOW + (c["volatility"] / 5.0) * (MAX_BINS_BELOW - MIN_BINS_BELOW))
                bb = max(MIN_BINS_BELOW, min(MAX_BINS_BELOW, max(c_phys, c_lin // 2)))
            bins_by_candidate[c["pool"]] = bb
            # Probe a symmetric range (widest the strategy would use) — conservative superset.
            cov = check_bin_coverage(c["pool"], bb, bb)
            if cov is not None and not cov.get("deployable", True):
                print(f"Skipping {c['name']} - deploy range needs {cov.get('missing', 0)} new bin-array init "
                      f"(~{cov.get('totalFee', 0):.4f} SOL non-refundable rent) — excluded from AI pick")
                continue
            if cov is not None:
                c["bin_coverage_ok"] = True
            deployable_candidates.append(c)

        if not deployable_candidates:
            print("No candidates deployable without new bin-array init (all ranges would incur non-refundable rent).")
            sys.exit(0)

        # Build bins_rationale so AI understands what drove each bin count
        bins_rationale = {}
        for c in deployable_candidates:
            bb = bins_by_candidate[c["pool"]]
            bsp = max(c.get("bin_step", 100), 1) / 100.0
            phys = int((c["volatility"] * 2.5) / bsp)
            lin = int(MIN_BINS_BELOW + (c["volatility"] / 5.0) * (MAX_BINS_BELOW - MIN_BINS_BELOW))
            method = "floor" if bb == MIN_BINS_BELOW else ("physics" if phys >= lin // 2 else "linear")
            bins_rationale[c["pool"]] = {
                "bins": bb,
                "coverage_pct": round(bb * bsp, 1),
                "method": method,
                "bin_step": c.get("bin_step", 100),
            }

        print("ANALYZE_ONLY_OUTPUT:" + json.dumps({
            "candidates": deployable_candidates,
            "deploy_sol": deploy_sol,
            "soul_strategy": params.get("STRATEGY", "spot"),
            "bins_by_pool": {c["pool"]: bins_by_candidate[c["pool"]] for c in deployable_candidates},
            "bins_rationale": bins_rationale,
        }))
        sys.exit(0)

    # --pool override: AI specified which pool to deploy
    winner = None
    if cli.pool:
        for c in valid_candidates:
            if c["pool"] == cli.pool:
                winner = c
                break
        if not winner:
            print(f"Error: --pool {cli.pool} not found in valid candidates. Aborting.")
            sys.exit(1)
    else:
        # Auto-pick: take the highest-scored candidate that is deployable rent-free.
        # Skip any whose range needs new bin-array init (non-refundable). Fail OPEN.
        winner = None
        for c in valid_candidates:
            v = c["volatility"]
            bsp = max(c.get("bin_step", 100), 1) / 100.0
            bp = int((v * 2.5) / bsp)
            bl = int(MIN_BINS_BELOW + (v / 5.0) * (MAX_BINS_BELOW - MIN_BINS_BELOW))
            bb = max(MIN_BINS_BELOW, min(MAX_BINS_BELOW, max(bp, bl // 2)))
            cov = check_bin_coverage(c["pool"], bb, bb)
            if cov is not None and not cov.get("deployable", True):
                print(f"Skipping {c['name']} - deploy range needs {cov.get('missing', 0)} new bin-array init "
                      f"(~{cov.get('totalFee', 0):.4f} SOL non-refundable rent)")
                continue
            winner = c
            break
        if not winner:
            print("No candidates deployable without new bin-array init (all ranges would incur non-refundable rent).")
            sys.exit(0)

    # 5. Indicators Check
    if params.get("INDICATORS_ENABLED"):
        preset = params.get("INDICATORS_PRESET", "supertrend_or_rsi")
        confirmed = check_local_indicators(winner["pool"], winner["base_mint"], "entry", preset, timeframe)
        if confirmed is False:
            print(f"Aborting deploy: entry timing check ({preset}) rejected for base token {winner['base_symbol']}.")
            sys.exit(0)
        elif confirmed is None:
            print(f"Entry timing: indicator data unavailable for {winner['base_symbol']} — proceeding on other gates (fail-open).")

    # 6. Deploy Position
    print(f"\n🚀 WINNING CANDIDATE: {winner['name']} ({winner['pool']})")

    vol = winner["volatility"]
    bin_step = winner.get("bin_step", 100)
    # Physics-based: cover 2.5x daily volatility in price range
    # bin_step is in bps (e.g. 100 = 1% per bin); higher bin_step = fewer bins for same coverage
    bin_step_pct = max(bin_step, 1) / 100.0
    bins_physics = int((vol * 2.5) / bin_step_pct)
    bins_linear = int(MIN_BINS_BELOW + (vol / 5.0) * (MAX_BINS_BELOW - MIN_BINS_BELOW))
    bins_below = max(MIN_BINS_BELOW, min(MAX_BINS_BELOW, max(bins_physics, bins_linear // 2)))

    strategy = cli.strategy if cli.strategy else params.get("STRATEGY", "spot")
    slippage_bps = params.get("SLIPPAGE_BPS", 1000)
    
    # SOL goes in the slot matching pool orientation (executor: amountX->tokenX).
    sol_is_x = winner.get("sol_is_x", False)
    if sol_is_x:
        amount_x = deploy_sol
        amount_y = 0
    else:
        amount_x = 0
        amount_y = deploy_sol
    bins_above = 0
    strategy_type = "spot"

    if strategy == "single_sided_reseed":
        # Swaps SOL for base token first to deploy token-only Bid-Ask
        print(f"Strategy: single_sided_reseed (token-only Bid-Ask). Swapping {deploy_sol} SOL to base token {winner['base_symbol']} first...")
        swap_cmd = f"node {EXECUTOR_PATH} swap SOL {winner['base_mint']} {deploy_sol}"
        swap_res, swap_err = run_command_json(swap_cmd)
        if not swap_res or not swap_res.get("success"):
            print(f"Pre-LP Swap failed: {swap_err or swap_res.get('error') if swap_res else 'No response'}. Aborting deploy.")
            sys.exit(1)
        
        # Wait for confirmation and verify balance
        time.sleep(5)
        bal_data, bal_err = run_command_json(f"node {EXECUTOR_PATH} spl-balance {winner['base_mint']}")
        if not bal_data or bal_data.get("balance", 0) <= 0:
            print("Failed to acquire base token balance for Bid-Ask LP deployment. Aborting.")
            sys.exit(1)
            
        base_bal = float(bal_data.get("balance", 0))
        # Base token occupies the slot opposite SOL (base is tokenY when sol_is_x).
        if sol_is_x:
            amount_x = 0
            amount_y = base_bal
        else:
            amount_x = base_bal
            amount_y = 0
        strategy_type = "bid_ask"
        print(f"Acquired {base_bal} {winner['base_symbol']}. Ready to deploy token-only Bid-Ask.")
        
    elif strategy == "custom_ratio_spot":
        # Symmetric range: equal bins above and below for balanced in-range time
        # bins_below already set by volatility; bins_above mirrors it for symmetric coverage
        bins_above = bins_below
        strategy_type = "spot"
        print(f"Strategy: custom_ratio_spot (symmetric Spot). bins_below: {bins_below}, bins_above: {bins_above}.")

    elif strategy == "stage_aware":
        # Capital-stage selection: strategy + bin width scale with deploy size (SOL-only).
        stage_label, bins_below, bins_above, strategy_type = select_stage_strategy(deploy_sol, vol, bin_step, mode=mode)
        print(f"Strategy: stage_aware -> {stage_label} ({deploy_sol} SOL). "
              f"bins_below: {bins_below}, bins_above: {bins_above}, type: {strategy_type}.")

    # Pre-deploy checks: momentum gate + fee/TVL freshness + depth/exit-liquidity gate
    base_mint = winner.get("base_mint", "")
    if not base_mint:
        print(f"Aborting deploy: {winner['name']} has no base_mint resolved — cannot guarantee auto-swap on exit.")
        sys.exit(1)
    if base_mint and os.environ.get("DRY_RUN") != "true":
        # B. Momentum gate: abort if 5m price < -5% (dumping token)
        try:
            dex_url = f"https://api.dexscreener.com/latest/dex/tokens/{base_mint}"
            req = urllib.request.Request(dex_url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                dex_data = json.loads(resp.read())
            pairs = dex_data.get("pairs") or []
            if pairs:
                price_m5 = float((pairs[0].get("priceChange") or {}).get("m5", 0) or 0)
                print(f"Pre-deploy momentum check: 5m price change = {price_m5:+.2f}%")
                if price_m5 < -5.0:
                    print(f"Aborting deploy: {winner['name']} dumping {price_m5:.2f}% in last 5m — momentum gate triggered")
                    sys.exit(0)
        except Exception as e:
            print(f"Warning: pre-deploy momentum check failed ({e}) — proceeding with deploy")

        # C. Fee/TVL freshness: re-query Meteora for the pool's LIVE fee/TVL and
        # abort if it dropped >50% since screening. Apples-to-apples (both Meteora 24h),
        # replacing the old DexScreener volume proxy that systematically false-aborted.
        screened_fee_tvl = winner["fee_tvl_ratio"]
        live_fee_tvl = fetch_live_fee_tvl(winner["pool"], timeframe)
        if live_fee_tvl is not None and screened_fee_tvl > 0:
            drop_pct = (screened_fee_tvl - live_fee_tvl) / screened_fee_tvl * 100
            print(f"Fee/TVL freshness: screened={screened_fee_tvl:.2f}% live={live_fee_tvl:.2f}% (drop={drop_pct:.1f}%)")
            if drop_pct > 50:
                print(f"Aborting deploy: fee/TVL dropped {drop_pct:.1f}% since screening — pool yield degraded")
                sys.exit(0)
        else:
            print("Fee/TVL freshness: live re-query unavailable — proceeding on screened value")

        # D. Depth / exit-liquidity gate: refuse entry if pool too thin to exit cleanly at our size.
        impact_pct = get_price_impact_sol_to_token(base_mint, deploy_sol)
        if impact_pct is not None:
            print(f"Pre-deploy depth check: SOL->{winner['base_symbol']} price impact = {impact_pct:.2f}% at {deploy_sol} SOL")
            if impact_pct > MAX_PRICE_IMPACT_PCT:
                print(f"Aborting deploy: price impact {impact_pct:.2f}% > {MAX_PRICE_IMPACT_PCT}% — pool too thin, exit would strand token")
                sys.exit(0)
        else:
            print("Pre-deploy depth check: Jupiter quote unavailable — proceeding without depth gate")

    deploy_cmd = f"node {EXECUTOR_PATH} deploy {winner['pool']} {amount_x} {amount_y} {bins_below} {bins_above} {strategy_type} {slippage_bps}"
    print(f"Running deploy: {deploy_cmd}")
    res, err = run_command_json(deploy_cmd)
    
    if not res:
        print(f"Deployment failed: {err}")
        sys.exit(1)

    if not res.get("success"):
        print(f"Deployment failed: {res.get('error')}")
        # Wide-range deploy can mint a position NFT before add-liquidity fails. If the
        # executor could not clean it up (orphan=true), it returns the stranded position
        # address. Register it in Redis so the monitor reconciliation loop reclaims (closes)
        # it next cycle instead of leaving an untracked 0-deposit zombie on-chain.
        orphan_pos = res.get("position") if res.get("orphan") else None
        if orphan_pos:
            run_command(f"redis-cli sadd \"sol:dlmm:active_positions\" \"{orphan_pos}\"")
            run_command(
                "redis-cli set \"sol:dlmm:position:%s\" '%s'" % (
                    orphan_pos,
                    json.dumps({
                        "pool": winner["pool"],
                        "pair": winner["name"],
                        "base_mint": winner["base_mint"],
                        "base_symbol": winner["base_symbol"],
                        "orphan": True,
                        "size_sol": 0.0,
                        "deployed_at": int(time.time()),
                        "note": "add-liquidity failed; empty NFT stranded — monitor must reclaim"
                    })
                )
            )
            print(f"⚠️ Orphan empty position {orphan_pos} registered in Redis for monitor reclaim.")
        sys.exit(1)

    position_address = res.get("position", "DRY_RUN_POSITION")
    tx_hash = res.get("txHash", "DRY_RUN_TX_HASH")
    
    active_bin = bins_below
    active_price = 0.0
    ab_data, ab_err = run_command_json(f"node {EXECUTOR_PATH} active-bin {winner['pool']}")
    if ab_data:
        active_bin = ab_data.get("binId")
        active_price = float(ab_data.get("price", 0.0))

    ts = int(time.time())
    tracking_data = {
        "pool": winner["pool"],
        "pair": winner["name"],
        "base_mint": winner["base_mint"],
        "base_symbol": winner["base_symbol"],
        "entry_price": active_price,
        "entry_bin": active_bin,
        "bins_below": bins_below,
        "bins_above": bins_above,
        "size_sol": deploy_sol,
        "deployed_at": ts,
        "tx_hash": tx_hash,
        "strategy": strategy,
        "amount_x": amount_x,
        "amount_y": amount_y,
        "mode": mode,
        # Entry-time signal snapshot — the monitor copies this into the close
        # journal so dlmm_weights.py can learn which signals predict winners.
        "signal": {k: winner.get(k) for k in (
            "score", "organic_score", "fee_tvl_ratio", "fee_active_tvl_ratio",
            "volatility", "mcap", "holders", "tvl", "fee_pct",
            "volume_tvl_ratio", "swap_count", "unique_traders",
            "bot_holders_pct", "global_fees_sol",
        ) if winner.get(k) is not None},
    }

    is_dry_run = res.get("dryRun") or (res.get("dry_run") == True)
    if not is_dry_run:
        run_command(f"redis-cli set \"sol:dlmm:position:{position_address}\" '{json.dumps(tracking_data)}'")
        run_command(f"redis-cli sadd \"sol:dlmm:active_positions\" \"{position_address}\"")
        print(f"Position saved to Redis: sol:dlmm:position:{position_address}")
        # Repeat-deploy churn guard: 3+ deploys into the same pool inside 24h
        # means we keep round-tripping it (deploy -> close -> re-signal) — put
        # the POOL on a 12h cooldown, independent of the symbol cooldown.
        deploys_key = f"sol:dlmm:deploys:{winner['pool']}"
        count_out, _, _ = run_command(f"redis-cli incr \"{deploys_key}\"")
        run_command(f"redis-cli expire \"{deploys_key}\" 86400")
        try:
            if int(count_out) >= 3:
                run_command(f"redis-cli set \"sol:dlmm:cooldown:pool:{winner['pool']}\" \"repeat-deploy churn ({count_out} deploys in 24h)\" ex 43200")
                print(f"🚫 Pool cooldown set: {count_out} deploys into {winner['name']} within 24h")
        except (ValueError, TypeError):
            pass

    ts_str = local_time_str()
    status_label = "🧪 DRY RUN DEPLOY" if is_dry_run else "🚀 DEPLOYED"
    report = f"""{status_label} — {ts_str}
{winner['name']} {position_address}
Pool | {winner['pool']}
Metric | Value
Strategy | {strategy}
Position Size | {deploy_sol} SOL
Entry Bin | {active_bin}
Bins Below | {bins_below}
Bins Above | {bins_above}
Entry Price | {active_price:.8f}
Fee/TVL (24h) | {winner['fee_tvl_ratio']:.2f}% ({timeframe})
TVL | ${winner['tvl']:,.0f}
Volatility | {winner['volatility']:.2f}
Organic Score | {winner['organic_score']:.0f}
Mcap | ${winner['mcap']:,.0f}
Holders | {winner['holders']:,}
TX | https://solscan.io/tx/{tx_hash}
"""
    print(report)

if __name__ == "__main__":
    main()
