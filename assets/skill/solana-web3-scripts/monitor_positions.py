#!/usr/bin/env python3
"""
monitor_positions.py — Hybrid Solana Position Monitor.

Phase 1 (this script): Gather data for ALL tracked positions.
  - Fetches current prices from DexScreener.
  - Computes P&L%, trailing drawdown, position age.
  - AUTO-EXECUTES hard stop-loss (-25% from entry) — no LLM needed.
  - Outputs structured JSON report for LLM to make trailing stop decisions.

Phase 2 (LLM agent): Receives the JSON report and decides:
  - Whether to close positions based on trailing stop context.
  - Whether momentum/volume justifies holding despite drawdown.
  - Updates peak prices for positions still held.

Usage:
  python3 monitor_positions.py          # Full report (for LLM)
  python3 monitor_positions.py --auto   # Auto-execute hard SL only, silent if clean
"""

import json
import os
import sys
import time
import subprocess
import urllib.request
from datetime import datetime
try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

_WIB = ZoneInfo("Asia/Jakarta")

# Resolved from this file's own location (<profile>/skills/solana-web3/scripts/) so the
# script works whether it's a copy or a symlink into a Hermes profile.
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROFILE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(_SCRIPT_DIR)))

_TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
_TG_CHAT  = os.environ.get("TELEGRAM_HOME_CHANNEL", "")

def _tg_notify(text: str) -> None:
    """Fire-and-forget Telegram message. Silently swallows errors."""
    if not _TG_TOKEN or not _TG_CHAT:
        return
    try:
        payload = json.dumps({"chat_id": _TG_CHAT, "text": text, "parse_mode": "HTML"}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{_TG_TOKEN}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass

HARD_SL_PCT = -0.25          # -25% from entry (AMM/graduated tokens)
HARD_SL_PCT_PUMPFUN = -0.15  # -15% from entry (pump.fun bonding curve — moves faster)
TRAILING_WARN_PCT = -0.12    # -12% from peak = flag for LLM review (tightened from -15%)
TIME_EXIT_HOURS = 24         # 24h stale warn for meme coins (LLM decides)
TRAILING_TP_ACTIVATE_PCT = 100  # activate trailing stop once PEAK gain >= 100% from entry
TRAILING_TP_DROP_PCT = 20       # hard-close if drops 20% from peak once trailing active
RATCHET_ACTIVATE_PCT = 40       # once peak gain >= 40%, lock in breakeven floor
RATCHET_FLOOR_PCT = 5           # hard-close if PnL falls to +5% after ratchet activates

_SOLANA_RPC_URLS = [
    "https://api.mainnet-beta.solana.com",
    "https://rpc.ankr.com/solana",
    "https://solana-mainnet.g.alchemy.com/v2/demo",
]
_WALLET_PUBKEY = os.environ["SOLANA_PUBLIC_KEY"]


def _poll_close_txid(mint, attempts=20, interval=1.5):
    """Poll sol:tx:close:<MINT> until gobot writes the txid (up to ~30s)."""
    for _ in range(attempts):
        time.sleep(interval)
        out, _ = run_command(f"redis-cli get 'sol:tx:close:{mint}'")
        if out and out not in ("(nil)", ""):
            return out.strip()
    return None


def _rpc_post(url, method, params):
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except Exception:
        return {"error": True}


def check_mint_in_wallet(mint):
    """Targeted check: does wallet hold this specific mint?

    Uses getTokenAccountsByOwner with mint filter — much more reliable than
    querying the full token list. Returns True (held), False (confirmed absent),
    or None (all RPCs failed — treat as unknown, do NOT purge).
    """
    for url in _SOLANA_RPC_URLS:
        result = _rpc_post(url, "getTokenAccountsByOwner", [
            _WALLET_PUBKEY,
            {"mint": mint},
            {"encoding": "jsonParsed"},
        ])
        if "error" in result:
            continue
        accounts = result.get("result", {}).get("value", [])
        if accounts is None:
            continue
        for acct in accounts:
            info = acct.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
            amount = int(info.get("tokenAmount", {}).get("amount", "0") or "0")
            if amount > 0:
                return True
        return False  # RPC responded cleanly — mint not held
    return None  # all RPCs failed


def run_command(cmd):
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=15)
        return result.stdout.strip(), result.stderr.strip()
    except Exception as e:
        return "", str(e)


def track_daily_pnl(pnl_pct, size_usd):
    """Accumulate today's realized PnL (WIB) into sol:pnl:daily:YYYY-MM-DD."""
    try:
        today = datetime.now(_WIB).strftime("%Y-%m-%d")
        key = f"sol:pnl:daily:{today}"
        raw, _ = run_command(f"redis-cli get '{key}'", timeout=5)
        try:
            daily = json.loads(raw) if raw and raw not in ("", "(nil)") else {}
        except Exception:
            daily = {}
        pnl_usd = round(size_usd * (pnl_pct / 100), 4)
        daily["total_usd"] = round(daily.get("total_usd", 0.0) + pnl_usd, 4)
        daily["trade_count"] = daily.get("trade_count", 0) + 1
        daily["wins"] = daily.get("wins", 0) + (1 if pnl_pct > 0 else 0)
        daily["losses"] = daily.get("losses", 0) + (1 if pnl_pct <= 0 else 0)
        run_command(f"redis-cli set '{key}' '{json.dumps(daily)}' EX 604800")
    except Exception:
        pass


def publish_to_redis(channel, payload_dict):
    """Publish JSON to Redis channel using subprocess list args — no shell injection risk."""
    payload = json.dumps(payload_dict)
    try:
        result = subprocess.run(
            ["redis-cli", "publish", channel, payload],
            capture_output=True, text=True, timeout=10
        )
        return result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        return "", "timeout"
    except Exception as e:
        return "", str(e)


def _parse_dexscreener_pairs(pairs):
    """Select best pair from DexScreener pairs list and return normalized dict."""
    if not pairs:
        return None
    # Prefer AMM pairs (non-pumpfun) with meaningful liquidity
    amm_with_liq = [p for p in pairs if p.get("dexId") != "pumpfun" and float(p.get("liquidity", {}).get("usd", 0) or 0) >= 5000]
    if amm_with_liq:
        best = max(amm_with_liq, key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0))
    else:
        # Prefer ANY non-pumpfun pair over pumpfun bonding curve.
        # Pumpfun bonding curve price is stale once the token migrates to an AMM.
        non_pumpfun = [p for p in pairs if p.get("dexId") != "pumpfun"]
        if non_pumpfun:
            best = max(non_pumpfun, key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0))
        else:
            pumpfun_pairs = [p for p in pairs if p.get("dexId") == "pumpfun"]
            if pumpfun_pairs:
                best = max(pumpfun_pairs, key=lambda p: float(p.get("volume", {}).get("h24", 0) or 0))
            else:
                best = max(pairs, key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0))
    is_pumpfun = best.get("dexId") == "pumpfun"
    liq_usd = float(best.get("liquidity", {}).get("usd", 0) or 0)
    if is_pumpfun and liq_usd == 0:
        try:
            import math
            price_native = float(best.get("priceNative", 0))
            price_usd_val = float(best.get("priceUsd", 0))
            if price_native > 0 and price_usd_val > 0:
                sol_price = price_usd_val / price_native
                # Constant Product Invariant K = 30 SOL * 1,073,000,000 tokens = 3.219e10
                virtual_sol = math.sqrt(3.219e10 * price_native)
                # Two-sided pool depth = 2 * virtual_sol * sol_price
                liq_usd = 2.0 * virtual_sol * sol_price
            else:
                liq_usd = 30000.0
        except Exception:
            liq_usd = 30000.0
    fdv = float(best.get("fdv", 0) or 0)
    if fdv == 0:
        fdv = float(best.get("priceUsd", 0) or 0) * 1000000000.0
    price = float(best.get("priceUsd", 0) or 0)
    if price == 0:
        return None
    return {
        "price": price,
        "fdv": fdv,
        "volume_24h": float(best.get("volume", {}).get("h24", 0) or 0),
        "volume_6h": float(best.get("volume", {}).get("h6", 0) or 0),
        "volume_1h": float(best.get("volume", {}).get("h1", 0) or 0),
        "price_change_5m": float(best.get("priceChange", {}).get("m5", 0) or 0),
        "price_change_1h": float(best.get("priceChange", {}).get("h1", 0) or 0),
        "price_change_6h": float(best.get("priceChange", {}).get("h6", 0) or 0),
        "price_change_24h": float(best.get("priceChange", {}).get("h24", 0) or 0),
        "liquidity_usd": liq_usd,
        "txns_buy_24h": int(best.get("txns", {}).get("h24", {}).get("buys", 0) or 0),
        "txns_sell_24h": int(best.get("txns", {}).get("h24", {}).get("sells", 0) or 0),
        "is_pumpfun": is_pumpfun,
        "data_source": "dexscreener",
    }


def _fetch_dexscreener_batch(mints):
    """Fetch multiple mints in ONE request to DexScreener. Returns dict of mint→data."""
    if not mints:
        return {}
    joined = ",".join(mints)
    url = f"https://api.dexscreener.com/latest/dex/tokens/{joined}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    results = {}
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            pairs_all = data.get("pairs", []) or []
            # Group pairs by base token address
            by_mint = {}
            for p in pairs_all:
                base_addr = (p.get("baseToken") or {}).get("address", "")
                if base_addr:
                    by_mint.setdefault(base_addr, []).append(p)
            for mint in mints:
                pairs = by_mint.get(mint, [])
                parsed = _parse_dexscreener_pairs(pairs)
                if parsed and parsed["price"] > 0:
                    results[mint] = parsed
    except Exception:
        pass
    return results


def _fetch_geckoterminal(mint):
    """Fallback 1: GeckoTerminal. Full data — price, liquidity, volume, tx counts, price change."""
    url = f"https://api.geckoterminal.com/api/v2/networks/solana/tokens/{mint}/pools?page=1"
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            pools = data.get("data", [])
            if not pools:
                return None
            # Prefer pool with highest liquidity (reserve_in_usd)
            best = max(pools, key=lambda p: float(p.get("attributes", {}).get("reserve_in_usd", 0) or 0))
            attr = best.get("attributes", {})
            price = float(attr.get("base_token_price_usd", 0) or 0)
            if price == 0:
                return None
            fdv = float(attr.get("fdv_usd", 0) or 0)
            if fdv == 0:
                fdv = float(attr.get("market_cap_usd", 0) or 0)
            liq_usd = float(attr.get("reserve_in_usd", 0) or 0)
            vol = attr.get("volume_usd", {})
            pc = attr.get("price_change_percentage", {})
            txns = attr.get("transactions", {})
            # is_pumpfun: check dex relationship or pool name
            pool_name = attr.get("name", "").lower()
            dex_id = ""
            try:
                dex_id = best.get("relationships", {}).get("dex", {}).get("data", {}).get("id", "").lower()
            except Exception:
                pass
            is_pumpfun = "pump" in dex_id or "pump" in pool_name
            return {
                "price": price,
                "fdv": fdv,
                "volume_24h": float(vol.get("h24", 0) or 0),
                "volume_6h": float(vol.get("h6", 0) or 0),
                "volume_1h": float(vol.get("h1", 0) or 0),
                "price_change_5m": float(pc.get("m5", 0) or 0),
                "price_change_1h": float(pc.get("h1", 0) or 0),
                "price_change_6h": float(pc.get("h6", 0) or 0),
                "price_change_24h": float(pc.get("h24", 0) or 0),
                "liquidity_usd": liq_usd,
                "txns_buy_24h": int(txns.get("h24", {}).get("buys", 0) or 0),
                "txns_sell_24h": int(txns.get("h24", {}).get("sells", 0) or 0),
                "is_pumpfun": is_pumpfun,
                "data_source": "geckoterminal",
            }
    except Exception:
        return None


def _fetch_binance_web3(mint):
    """Fallback 2: Binance Web3 wallet API. Price + volume + tx counts. No liquidity or price_change_pct."""
    url = f"https://web3.binance.com/bapi/defi/v4/public/wallet-direct/buw/wallet/market/token/dynamic/info?chainId=CT_501&contractAddress={mint}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp_data = json.loads(resp.read().decode("utf-8"))
            if resp_data.get("code") != "000000":
                return None
            d = resp_data.get("data", {})
            price = float(d.get("price", 0) or 0)
            if price == 0:
                return None
            return {
                "price": price,
                "fdv": 0,
                "volume_24h": float(d.get("volume24h", 0) or 0),
                "volume_6h": 0,
                "volume_1h": float(d.get("volume1h", 0) or 0),
                "price_change_5m": 0,
                "price_change_1h": 0,
                "price_change_6h": 0,
                "price_change_24h": 0,
                "liquidity_usd": 0,
                "txns_buy_24h": int(d.get("count24hBuy", 0) or 0),
                "txns_sell_24h": int(d.get("count24hSell", 0) or 0),
                "is_pumpfun": False,
                "data_source": "binance_web3",
            }
    except Exception:
        return None


def get_token_data(mint):
    """Fetch price + volume + liquidity for a single mint. Tries GeckoTerminal → Binance Web3.
    Prefer fetch_all_market_data() for multiple mints (batches DexScreener into 1 request)."""
    result = _fetch_geckoterminal(mint)
    if result and result["price"] > 0:
        return result
    result = _fetch_binance_web3(mint)
    if result and result["price"] > 0:
        return result
    return None


def fetch_all_market_data(mints):
    """Fetch market data for all mints. DexScreener batch first (1 request), then per-token fallbacks.
    Returns dict of mint → market data. Mints with no data are absent from the dict."""
    # Phase 1: one DexScreener batch request covers all mints
    results = _fetch_dexscreener_batch(mints)
    missing = [m for m in mints if m not in results]
    if not missing:
        return results

    # Phase 2: GeckoTerminal per-token for what DexScreener missed (1s delay between calls)
    for i, mint in enumerate(missing):
        if i > 0:
            time.sleep(1)
        data = _fetch_geckoterminal(mint)
        if data and data["price"] > 0:
            results[mint] = data

    # Phase 3: Binance Web3 for anything still missing (no rate limit concerns)
    still_missing = [m for m in mints if m not in results]
    for mint in still_missing:
        data = _fetch_binance_web3(mint)
        if data and data["price"] > 0:
            results[mint] = data

    return results


_EXITS_LOG = os.path.join(_PROFILE_DIR, "logs", "trade_exits.jsonl")

def _log_exit(record):
    """Append exit record to JSONL for the weekly self-improvement review.
    Auto-closes happen in the 15s fast loop where no LLM runs hindsight_retain,
    so without this file the strategy review has no exit data to learn from."""
    try:
        record["date_wib"] = datetime.now(_WIB).strftime("%Y-%m-%d %H:%M")
        with open(_EXITS_LOG, "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass


def publish_close(mint, ticker, reason, slippage_bps=2000, cooldown_seconds=7200, pnl_pct=None, size_usd=None):
    """Publish CLOSE decision to Redis.

    slippage_bps: tolerance passed to gobot's swap engine. Defaults to 2000 (20%).
    cooldown_seconds: block re-entry after close (default 2h).
    pnl_pct/size_usd: when provided, updates today's realized PnL accumulator.
    """
    ts = int(time.time())
    decision = {
        "signal_id": f"sol_sl_{ts}_exit",
        "decision": "CLOSE",
        "exchange": "solana",
        "symbol": mint,
        "ticker": ticker,
        "side": "SELL",
        "position_size": 0,
        "leverage": 1,
        "slippage_bps": slippage_bps,
        "reason": reason
    }
    out, err = publish_to_redis("decisions", decision)
    if out == "0":
        print(f"WARNING: 0 subscribers on decisions channel — gobot may be down (ticker={ticker})", file=sys.stderr)
    _log_exit({"ts": ts, "ticker": ticker, "mint": mint, "pnl_pct": pnl_pct,
               "size_usd": size_usd, "reason": reason, "type": "full_close"})
    # Set cooldown to prevent immediate re-buy of same token
    run_command(f"redis-cli set 'sol:cooldown:{mint}' '1' EX {cooldown_seconds}")
    # Set name-level cooldown — block all mints with same ticker for 24h
    name_key = ticker.upper().replace("'", "").replace('"', '')
    run_command(f"redis-cli set 'sol:name_cooldown:{name_key}' '{mint}' EX 86400")
    # Track realized PnL for daily overview
    if pnl_pct is not None and size_usd is not None and size_usd > 0:
        track_daily_pnl(pnl_pct, size_usd)
    return decision


def publish_partial_tp(mint, ticker, entry_price, current_price, pnl_pct, pos, key):
    """Sell 50% at first +50% milestone. Mark position so it only fires once."""
    ts = int(time.time())
    decision = {
        "signal_id": f"sol_partial_tp_{ts}_{ticker}",
        "decision": "CLOSE",
        "exchange": "solana",
        "symbol": mint,
        "ticker": ticker,
        "side": "SELL",
        "close_pct": 0.5,
        "leverage": 1,
        "slippage_bps": 1500,
        "reason": f"Partial TP: +{pnl_pct:.1f}% — selling 50%, letting remainder ride"
    }
    out, err = publish_to_redis("decisions", decision)
    if out == "0":
        print(f"WARNING: 0 subscribers on decisions channel — gobot may be down (ticker={ticker})", file=sys.stderr)
    _log_exit({"ts": ts, "ticker": ticker, "mint": mint, "pnl_pct": round(pnl_pct, 2),
               "size_usd": pos.get("size_usd", pos.get("position_size", 0)),
               "reason": decision["reason"], "type": "partial_tp"})
    # Mark partial TP done so it doesn't re-trigger; keep position alive
    pos["partial_tp_done"] = True
    pos["partial_tp_price"] = current_price
    pos["partial_tp_pnl"] = round(pnl_pct, 2)
    run_command(f"redis-cli set '{key}' '{json.dumps(pos)}' EX 604800")
    return decision


def get_wallet_holdings():
    """Return (set of mints, dict of mint→amount) from real wallet.

    Returns (None, {}) on any failure or if the RPC response looks suspicious
    (too few tokens). Callers treat None as 'wallet unknown — skip phantom purge'.
    """
    try:
        out, _ = run_command(f"python3 {os.path.join(_SCRIPT_DIR, 'get_portfolio.py')}")
        data = json.loads(out)
        mints = set(data.get("held_mints", []))
        amounts = {t["mint"]: t["amount"] for t in data.get("tokens", [])}
        # Sanity check: public RPC can return empty on rate-limit.
        # Return None only on truly empty response (0 mints) — even 1 token is valid.
        if len(mints) == 0:
            return None, {}
        return mints, amounts
    except Exception:
        return None, {}


def recover_untracked_positions(wallet_mints, wallet_amounts, now):
    """
    Find wallet tokens that are in the sol:active_mints set but have no
    sol:position:* key (tracking was lost). Creates recovery entries.
    Returns list of newly created position keys.
    """
    if not wallet_mints:
        return []

    active_mints_str, _ = run_command("redis-cli smembers 'sol:active_mints'")
    if not active_mints_str:
        return []

    new_keys = []
    for mint in active_mints_str.split('\n'):
        mint = mint.strip()
        if not mint:
            continue

        if mint not in wallet_mints:
            # Clean it up from the active set if we no longer hold it
            run_command(f"redis-cli srem 'sol:active_mints' '{mint}'")
            continue

        # Skip if position key already exists
        exists, _ = run_command(f"redis-cli exists 'sol:position:{mint}'")
        if exists.strip() == "1":
            continue

        # Read signal for ticker + timestamp
        sig_val, _ = run_command(f"redis-cli get 'sol:signal:{mint}'")
        ticker = mint[:8]
        opened_at = now
        if sig_val and sig_val != "(nil)":
            try:
                sig_data = json.loads(sig_val)
                ticker = sig_data.get("ticker", ticker)
                opened_at = sig_data.get("ts", opened_at)
            except Exception:
                pass

        # Estimate entry price from token amount and default trade size ($5)
        amount = wallet_amounts.get(mint, 0)
        entry_price = (5.0 / amount) if amount > 0 else 0

        pos = {
            "symbol": ticker,
            "entry_price": entry_price,
            "opened_at": opened_at,
            "peak_price": entry_price,
            "recovered": True,  # flag: entry price is estimated, not recorded
        }
        run_command(f"redis-cli set 'sol:position:{mint}' '{json.dumps(pos)}' EX 604800")
        new_keys.append(f"sol:position:{mint}")

    return new_keys


def main():
    auto_only = "--auto" in sys.argv
    now = int(time.time())

    # 1. Fetch real wallet state
    wallet_mints, wallet_amounts = get_wallet_holdings()

    # 2. Load all Redis position data into a map (enrichment — not the authoritative position list)
    redis_pos_map = {}
    keys_str, _ = run_command("redis-cli keys 'sol:position:*'")
    for key in ([k for k in keys_str.split('\n') if k] if keys_str else []):
        mint_key = key.split(':')[-1]
        raw, _ = run_command(f"redis-cli get '{key}'")
        if raw and raw != "(nil)":
            try:
                redis_pos_map[mint_key] = json.loads(raw)
            except Exception:
                pass

    # 3. Load sol:active_mints (supplementary — catches tokens temporarily missing from RPC)
    active_mints_str, _ = run_command("redis-cli smembers 'sol:active_mints'")
    active_mints = {m.strip() for m in active_mints_str.split('\n') if m.strip()} if active_mints_str else set()

    # 4. Build candidate set — wallet-first, never miss a position because Redis write failed.
    # Include wallet tokens that have a Redis entry OR are in sol:active_mints.
    # Also include all Redis-tracked mints so phantom purge can see them.
    candidate_mints = set()
    if wallet_mints:
        for mint in wallet_mints:
            if mint in redis_pos_map or mint in active_mints:
                candidate_mints.add(mint)
    candidate_mints.update(redis_pos_map.keys())

    # 4b. DLMM exclusion — the spot fast-monitor must NEVER adopt or close a token that is the
    # base asset of an active DLMM position. DLMM exits are owned solely by dlmm_monitor.py (which
    # applies the health GUARD). Without this, a DLMM base-token bag landing in the wallet/active set
    # would be auto-recovered as a spot position and SL/TP-sold via gobot — a cross-system close that
    # bypasses the DLMM policy entirely (the Joby-class bug).
    dlmm_base_mints = set()
    dlmm_keys_str, _ = run_command("redis-cli smembers 'sol:dlmm:active_positions'")
    for pa in ([k for k in dlmm_keys_str.split('\n') if k] if dlmm_keys_str else []):
        praw, _ = run_command(f"redis-cli get 'sol:dlmm:position:{pa}'")
        if praw and praw != "(nil)":
            try:
                bm = json.loads(praw).get("base_mint")
                if bm:
                    dlmm_base_mints.add(bm)
            except Exception:
                pass
    if dlmm_base_mints:
        excluded = candidate_mints & dlmm_base_mints
        if excluded:
            print(f"DLMM-owned base mints excluded from spot monitor: {sorted(excluded)}", file=sys.stderr)
        candidate_mints -= dlmm_base_mints

    # 5. Auto-register wallet tokens that are candidates but missing a Redis entry (recovery)
    recovered_keys = []
    for mint in list(candidate_mints):
        if mint in redis_pos_map:
            continue
        if not wallet_mints or mint not in wallet_mints:
            continue
        sig_val, _ = run_command(f"redis-cli get 'sol:signal:{mint}'")
        ticker = mint[:8]
        opened_at = now
        if sig_val and sig_val != "(nil)":
            try:
                sig_data = json.loads(sig_val)
                ticker = sig_data.get("ticker", ticker)
                opened_at = sig_data.get("ts", opened_at)
            except Exception:
                pass
        amount = wallet_amounts.get(mint, 0)
        entry_price = (5.0 / amount) if amount > 0 else 0
        pos = {
            "symbol": ticker,
            "entry_price": entry_price,
            "opened_at": opened_at,
            "peak_price": entry_price,
            "position_size": 5.0,
            "recovered": True,
        }
        run_command(f"redis-cli set 'sol:position:{mint}' '{json.dumps(pos)}' EX 604800")
        run_command(f"redis-cli sadd 'sol:active_mints' '{mint}'")
        redis_pos_map[mint] = pos
        recovered_keys.append(f"sol:position:{mint}")

    # 6. Phantom purge: Redis entry exists but token not in wallet.
    # Safety: only purge when absence is confirmed, not just suspected (RPC gaps are real).
    phantom_cleaned = []
    valid_keys = []
    for mint in candidate_mints:
        key = f"sol:position:{mint}"
        if key in recovered_keys:
            valid_keys.append(key)
            continue
        if wallet_mints is not None and mint not in wallet_mints:
            if mint in wallet_amounts:
                # Explicitly 0 amount — token is sold
                run_command(f"redis-cli del '{key}'")
                run_command(f"redis-cli srem 'sol:active_mints' '{mint}'")
                phantom_cleaned.append(mint)
            else:
                cooldown_chk, _ = run_command(f"redis-cli exists 'sol:cooldown:{mint}'")
                if cooldown_chk.strip() == "1":
                    run_command(f"redis-cli del '{key}'")
                    run_command(f"redis-cli srem 'sol:active_mints' '{mint}'")
                    phantom_cleaned.append(mint)
                else:
                    held = check_mint_in_wallet(mint)
                    if held is True:
                        valid_keys.append(key)
                    elif held is False:
                        # Do NOT record -100% loss — phantom likely means gobot rejected buy.
                        run_command(f"redis-cli del '{key}'")
                        run_command(f"redis-cli srem 'sol:active_mints' '{mint}'")
                        phantom_cleaned.append(mint)
                    else:
                        valid_keys.append(key)  # all RPCs failed — keep
        else:
            valid_keys.append(key)
    keys = valid_keys

    if not keys:
        if auto_only and not recovered_keys and not phantom_cleaned:
            sys.exit(0)
        status = "no_positions" if not recovered_keys else "ok"
        print(json.dumps({"status": status, "positions": [], "phantom_cleaned": phantom_cleaned, "recovered_count": len(recovered_keys)}))
        sys.exit(0)

    positions = []
    auto_closed = []

    # Batch-fetch market data for all valid mints in one go (1 DexScreener request + per-token fallbacks only for misses)
    all_mints = [k.split(':')[-1] for k in keys]
    market_data_map = fetch_all_market_data(all_mints)

    for key in keys:
        mint = key.split(':')[-1]

        raw_val, _ = run_command(f"redis-cli get '{key}'")
        if not raw_val or raw_val == "(nil)":
            continue

        try:
            pos = json.loads(raw_val)
        except Exception:
            continue

        entry_price = pos.get("entry_price", 0)
        opened_at = pos.get("opened_at", 0)
        peak_price = pos.get("peak_price", entry_price)
        ticker = pos.get("symbol", pos.get("ticker", "TOKEN"))
        size_usd = pos.get("size_usd", pos.get("position_size", 0))
        if not size_usd or size_usd <= 0:
            size_usd = 5.0  # default: gobot overwrites position key without size_usd field

        market = market_data_map.get(mint)
        if not market or market["price"] == 0:
            positions.append({
                "ticker": ticker,
                "mint": mint,
                "error": "Could not fetch price",
                "entry_price": entry_price,
                "peak_price": peak_price,
            })
            continue

        current_price = market["price"]
        pnl_pct = ((current_price - entry_price) / entry_price) * 100 if entry_price > 0 else 0
        trail_pct = ((current_price - peak_price) / peak_price) * 100 if peak_price > 0 else 0
        age_hours = (now - opened_at) / 3600

        # Skip hard-exit checks if a close was already published (cooldown set).
        # Gobot deletes the position key after swap; until then the key lingers and
        # would re-trigger every 15s cycle, spamming notifications.
        cooldown_exists, _ = run_command(f"redis-cli exists 'sol:cooldown:{mint}'")
        if cooldown_exists.strip() == "1":
            # NOTE: do NOT append to valid_keys here — keys IS valid_keys (same list
            # object), appending while iterating creates an infinite loop.
            continue

        # === HARD STOP-LOSS: Auto-execute, no LLM needed ===
        is_pumpfun_pos = market.get("is_pumpfun", False) or mint.endswith("pump")
        sl_pct = HARD_SL_PCT_PUMPFUN if is_pumpfun_pos else HARD_SL_PCT
        if pnl_pct <= sl_pct * 100:
            reason = f"HARD STOP-LOSS: {pnl_pct:.1f}% loss from entry (limit: {sl_pct*100:.0f}%{'  pump.fun' if is_pumpfun_pos else ''})"
            decision = publish_close(mint, ticker, reason, slippage_bps=3000, cooldown_seconds=14400, pnl_pct=pnl_pct, size_usd=size_usd)
            auto_closed.append({
                "ticker": ticker,
                "mint": mint,
                "entry_price": entry_price,
                "exit_price": current_price,
                "pnl_pct": round(pnl_pct, 2),
                "reason": reason,
                "decision": decision
            })
            close_txid = _poll_close_txid(mint)
            tx_line = f"\nTX: https://solscan.io/tx/{close_txid}" if close_txid else "\nTX: pending"
            wib_time = datetime.now(_WIB).strftime("%H:%M WIB")
            _tg_notify(
                f"🚨 <b>FAST MONITOR — HARD STOP-LOSS</b>\n"
                f"⏰ {wib_time} | 🤖 sol-fast-monitor\n\n"
                f"<b>{ticker}</b> | PnL: <b>{pnl_pct:.1f}%</b>\n"
                f"Entry: ${entry_price:.8f} → Exit: ${current_price:.8f}\n"
                f"Age: {age_hours:.1f}h | Size: ${size_usd}\n"
                f"Liq: ${market['liquidity_usd']:,.0f} | Vol 1h: ${market['volume_1h']:,.0f}\n"
                f"Slippage: 3000 bps (30%) — aggressive to guarantee fill\n\n"
                f"Reason: {reason}{tx_line}\n"
                f"Mint: <code>{mint}</code>"
            )
            continue

        # === TRAILING TAKE-PROFIT: ride big winners, exit on pullback ===
        # Replaces hard close at +100% — lets multi-baggers run to max profit.
        # Activation is based on PEAK gain (not current PnL): once the position has
        # EVER been up TRAILING_TP_ACTIVATE_PCT (100%), exit fires whenever price
        # drops TRAILING_TP_DROP_PCT (20%) from peak — even if current PnL has
        # already fallen back below +100%.
        peak_gain_pct = ((peak_price - entry_price) / entry_price) * 100 if entry_price > 0 else 0
        if peak_gain_pct >= TRAILING_TP_ACTIVATE_PCT:
            if trail_pct <= -TRAILING_TP_DROP_PCT:
                reason = (
                    f"TRAILING TAKE-PROFIT: peaked +{peak_gain_pct:.1f}% from entry, "
                    f"dropped {trail_pct:.1f}% from peak (limit: -{TRAILING_TP_DROP_PCT}%)"
                )
                decision = publish_close(mint, ticker, reason, slippage_bps=1500, cooldown_seconds=7200, pnl_pct=pnl_pct, size_usd=size_usd)
                auto_closed.append({
                    "ticker": ticker,
                    "mint": mint,
                    "entry_price": entry_price,
                    "exit_price": current_price,
                    "peak_price": peak_price,
                    "peak_gain_pct": round(peak_gain_pct, 2),
                    "pnl_pct": round(pnl_pct, 2),
                    "trail_from_peak_pct": round(trail_pct, 2),
                    "reason": reason,
                    "decision": decision
                })
                close_txid = _poll_close_txid(mint)
                tx_line = f"\nTX: https://solscan.io/tx/{close_txid}" if close_txid else "\nTX: pending"
                wib_time = datetime.now(_WIB).strftime("%H:%M WIB")
                _tg_notify(
                    f"🏆 <b>FAST MONITOR — TRAILING TAKE-PROFIT</b>\n"
                    f"⏰ {wib_time} | 🤖 sol-fast-monitor\n\n"
                    f"<b>{ticker}</b> | PnL: <b>+{pnl_pct:.1f}%</b> (peaked +{peak_gain_pct:.1f}%)\n"
                    f"Entry: ${entry_price:.8f} → Exit: ${current_price:.8f}\n"
                    f"Peak: ${peak_price:.8f} | Dropped: {trail_pct:.1f}% from peak\n"
                    f"Age: {age_hours:.1f}h | Size: ${size_usd}\n"
                    f"Liq: ${market['liquidity_usd']:,.0f} | Vol 1h: ${market['volume_1h']:,.0f}\n"
                    f"Slippage: 1500 bps (15%)\n\n"
                    f"Reason: {reason}{tx_line}\n"
                    f"Mint: <code>{mint}</code>"
                )
                continue
            # Still above activation threshold but hasn't pulled back enough — let it run

        # === PROFIT RATCHET: never let a +40% winner round-trip to a loss ===
        # Once peak gain >= RATCHET_ACTIVATE_PCT (40%), a breakeven floor activates:
        # close if PnL falls to RATCHET_FLOOR_PCT (+5%). Covers the gap below the
        # +100% trailing-TP activation where a winner could otherwise ride down to
        # the -25% hard SL and book a full loss.
        if RATCHET_ACTIVATE_PCT <= peak_gain_pct < TRAILING_TP_ACTIVATE_PCT and pnl_pct <= RATCHET_FLOOR_PCT:
            reason = (
                f"PROFIT RATCHET: peaked +{peak_gain_pct:.1f}% from entry, "
                f"PnL fell to +{pnl_pct:.1f}% (floor: +{RATCHET_FLOOR_PCT}%) — locking breakeven"
            )
            decision = publish_close(mint, ticker, reason, slippage_bps=1500, cooldown_seconds=7200, pnl_pct=pnl_pct, size_usd=size_usd)
            auto_closed.append({
                "ticker": ticker,
                "mint": mint,
                "entry_price": entry_price,
                "exit_price": current_price,
                "peak_price": peak_price,
                "peak_gain_pct": round(peak_gain_pct, 2),
                "pnl_pct": round(pnl_pct, 2),
                "reason": reason,
                "decision": decision
            })
            close_txid = _poll_close_txid(mint)
            tx_line = f"\nTX: https://solscan.io/tx/{close_txid}" if close_txid else "\nTX: pending"
            wib_time = datetime.now(_WIB).strftime("%H:%M WIB")
            _tg_notify(
                f"🔒 <b>FAST MONITOR — PROFIT RATCHET (breakeven lock)</b>\n"
                f"⏰ {wib_time} | 🤖 sol-fast-monitor\n\n"
                f"<b>{ticker}</b> | PnL: <b>+{pnl_pct:.1f}%</b> (peaked +{peak_gain_pct:.1f}%)\n"
                f"Entry: ${entry_price:.8f} → Exit: ${current_price:.8f}\n"
                f"Age: {age_hours:.1f}h | Size: ${size_usd}\n"
                f"Liq: ${market['liquidity_usd']:,.0f} | Vol 1h: ${market['volume_1h']:,.0f}\n\n"
                f"Reason: {reason}{tx_line}\n"
                f"Mint: <code>{mint}</code>"
            )
            continue

        # === HARD LOW LIQUIDITY SHUTDOWN: Auto-execute if liquidity drops below $5000 on AMM ===
        if market["liquidity_usd"] < 5000 and not market["is_pumpfun"]:
            reason = f"LIQUIDITY SHUTDOWN: liquidity is ${market['liquidity_usd']:.0f} (< $5000) on non-pumpfun AMM"
            decision = publish_close(mint, ticker, reason, slippage_bps=2500, cooldown_seconds=21600, pnl_pct=pnl_pct, size_usd=size_usd)
            auto_closed.append({
                "ticker": ticker,
                "mint": mint,
                "entry_price": entry_price,
                "exit_price": current_price,
                "pnl_pct": round(pnl_pct, 2),
                "reason": reason,
                "decision": decision
            })
            close_txid = _poll_close_txid(mint)
            tx_line = f"\nTX: https://solscan.io/tx/{close_txid}" if close_txid else "\nTX: pending"
            wib_time = datetime.now(_WIB).strftime("%H:%M WIB")
            _tg_notify(
                f"⚠️ <b>FAST MONITOR — LIQUIDITY SHUTDOWN</b>\n"
                f"⏰ {wib_time} | 🤖 sol-fast-monitor\n\n"
                f"<b>{ticker}</b> | PnL: <b>{pnl_pct:.1f}%</b>\n"
                f"Entry: ${entry_price:.8f} → Exit: ${current_price:.8f}\n"
                f"Age: {age_hours:.1f}h | Size: ${size_usd}\n"
                f"Liq: ${market['liquidity_usd']:,.0f} ⬇️ (threshold: $5,000)\n"
                f"Vol 1h: ${market['volume_1h']:,.0f}\n"
                f"Slippage: 2500 bps (25%)\n\n"
                f"Reason: {reason}{tx_line}\n"
                f"Mint: <code>{mint}</code>"
            )
            continue

        # === PARTIAL TP: auto-execute at +50% (fires once per position) ===
        partial_tp_done = pos.get("partial_tp_done", False)
        if pnl_pct >= 50 and not partial_tp_done:
            partial_tp_decision = publish_partial_tp(mint, ticker, entry_price, current_price, pnl_pct, pos, key)
            auto_closed.append({
                "type": "partial_tp",
                "ticker": ticker,
                "mint": mint,
                "entry_price": entry_price,
                "tp_price": current_price,
                "pnl_pct": round(pnl_pct, 2),
                "reason": partial_tp_decision["reason"],
                "decision": partial_tp_decision
            })
            close_txid = _poll_close_txid(mint)
            tx_line = f"\nTX: https://solscan.io/tx/{close_txid}" if close_txid else "\nTX: pending"
            wib_time = datetime.now(_WIB).strftime("%H:%M WIB")
            _tg_notify(
                f"🎯 <b>FAST MONITOR — PARTIAL TAKE-PROFIT (50%)</b>\n"
                f"⏰ {wib_time} | 🤖 sol-fast-monitor\n\n"
                f"<b>{ticker}</b> | PnL: <b>+{pnl_pct:.1f}%</b>\n"
                f"Entry: ${entry_price:.8f} → Now: ${current_price:.8f}\n"
                f"Age: {age_hours:.1f}h | Size: ${size_usd}\n"
                f"Liq: ${market['liquidity_usd']:,.0f} | Vol 1h: ${market['volume_1h']:,.0f}\n"
                f"Action: Sold 50% — remainder still monitored by LLM\n\n"
                f"Reason: {partial_tp_decision['reason']}{tx_line}\n"
                f"Mint: <code>{mint}</code>"
            )
            # Don't continue — keep position in active list for LLM to monitor remainder

        # === Build position report for LLM (Heuristic warning flags for qualitative research) ===
        flags = []
        buy_sell_ratio = round(market["txns_buy_24h"] / max(market["txns_sell_24h"], 1), 2)

        # Check hold override conditions first
        is_hold = False
        hold_reason = ""
        if market["price_change_1h"] > 10 and market["volume_1h"] > (market["volume_6h"] / 6):
            is_hold = True
            hold_reason = f"Active pump (+{market['price_change_1h']:.1f}% 1h)"
        elif pnl_pct < -10 and market["price_change_1h"] > 5:
            is_hold = True
            hold_reason = f"Recovering bounce (+{market['price_change_1h']:.1f}% 1h)"

        if is_hold:
            flags.append(f"HOLD_RECOMMENDED: {hold_reason} — overrides qualitative warning triggers")
        else:
            # Trailing stop warning
            if trail_pct <= TRAILING_WARN_PCT * 100:
                flags.append(f"TRAILING_STOP_WARN: Dropped {trail_pct:.1f}% from peak (limit: {TRAILING_WARN_PCT*100:.0f}%) — check narrative momentum")
            # Rally fading warning
            if pnl_pct >= 50 and market["price_change_1h"] < -5:
                flags.append(f"RALLY_FADING_WARN: profit +{pnl_pct:.1f}% but 1h change {market['price_change_1h']:.1f}% (< -5%) — lock profit?")
            # Sellers taking over warning
            if pnl_pct >= 30 and buy_sell_ratio < 0.8:
                flags.append(f"SELLERS_TAKING_OVER_WARN: profit +{pnl_pct:.1f}% but 24h Buy/Sell ratio is {buy_sell_ratio:.2f} (< 0.8)")
            # Volume drying up warning (only relevant for older positions)
            if age_hours > 6.0 and market["volume_1h"] < (market["volume_6h"] / 6):
                flags.append(f"VOLUME_DRYING_UP_WARN: 1h volume ${market['volume_1h']:.0f} is dry (< 6h avg ${market['volume_6h']/6:.0f})")
            # Flash dump warning (only relevant for older positions)
            if age_hours > 2.0 and market["price_change_1h"] <= -15:
                flags.append(f"FLASH_DUMP_WARN: 1h price change is {market['price_change_1h']:.1f}% (<= -15%)")
            # Heavy net selling warning
            if buy_sell_ratio < 0.5:
                flags.append(f"HEAVY_NET_SELLING_WARN: 24h Buy/Sell ratio is {buy_sell_ratio:.2f} (< 0.5)")
            # Stale position warning
            if age_hours > TIME_EXIT_HOURS and pnl_pct < 20:
                flags.append(f"STALE_POSITION_WARN: open {age_hours:.1f}h with low profit +{pnl_pct:.1f}% (< 20%)")
            # Dead launchpad warning
            if market["is_pumpfun"] and market["volume_24h"] < 500:
                flags.append(f"DEAD_LAUNCHPAD_WARN: pump.fun token with 24h volume ${market['volume_24h']:.0f} (< $500)")

        if pos.get("partial_tp_done"):
            flags.append(f"PARTIAL_TP_DONE: sold 50% at ${pos.get('partial_tp_price',0):.8f} (+{pos.get('partial_tp_pnl',0):.1f}%) — monitoring remainder")

        # Update peak price if new high
        new_peak = peak_price
        if current_price > peak_price:
            new_peak = current_price
            pos["peak_price"] = current_price
        
        # Always update Redis key to refresh/extend the TTL to 7 days (604800 seconds)
        run_command(f"redis-cli set '{key}' '{json.dumps(pos)}' EX 604800")

        positions.append({
            "ticker": ticker,
            "mint": mint,
            "entry_price": entry_price,
            "current_price": current_price,
            "peak_price": new_peak,
            "pnl_pct": round(pnl_pct, 2),
            "trail_from_peak_pct": round(trail_pct, 2),
            "age_hours": round(age_hours, 1),
            "size_usd": size_usd,
            "flags": flags,
            "market": {
                "volume_1h": market["volume_1h"],
                "volume_6h": market["volume_6h"],
                "volume_24h": market["volume_24h"],
                "price_change_5m": market["price_change_5m"],
                "price_change_1h": market["price_change_1h"],
                "price_change_6h": market["price_change_6h"],
                "price_change_24h": market["price_change_24h"],
                "liquidity_usd": market["liquidity_usd"],
                "fdv": market["fdv"],
                "is_pumpfun": market.get("is_pumpfun", False),
                "buy_sell_ratio_24h": round(
                    market["txns_buy_24h"] / max(market["txns_sell_24h"], 1), 2
                ),
                "data_source": market.get("data_source", "unknown"),
            }
        })

    # Detect API failure: all tracked positions returned errors, none have live price
    positions_with_errors = [p for p in positions if "error" in p]
    all_api_failed = len(keys) > 0 and len(positions_with_errors) == len(positions) and len(auto_closed) == 0

    # Output structured report
    report = {
        "status": "ok",
        "timestamp": now,
        "api_error": all_api_failed,
        "api_error_detail": (
            f"DexScreener returned no price data for all {len(positions_with_errors)} positions — "
            "likely rate-limited (Cloudflare 1015). DO NOT make any CLOSE decisions. Report API error only."
        ) if all_api_failed else None,
        "auto_closed": auto_closed,
        "phantom_cleaned": phantom_cleaned,
        "recovered_count": len(recovered_keys),
        "positions": positions,
        "summary": {
            "total_tracked": len(keys),
            "recovered_count": len(recovered_keys),
            "phantom_cleaned_count": len(phantom_cleaned),
            "auto_closed_count": len(auto_closed),
            "active_count": len(positions),
            "price_fetch_errors": len(positions_with_errors),
            "flagged_count": len([p for p in positions if p.get("flags")]),
            "partial_tp_count": len([a for a in auto_closed if a.get("type") == "partial_tp"]),
            "hard_sl_count": len([a for a in auto_closed if a.get("type") != "partial_tp"]),
        }
    }

    # --auto mode (fast loop): stay silent unless something actionable happened.
    # Telegram alerts for closes already went out via _tg_notify above.
    if auto_only and not auto_closed and not phantom_cleaned and not recovered_keys:
        sys.exit(0)

    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
