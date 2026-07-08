#!/usr/bin/env python3
import sys
import json
import time
import subprocess
import os
import re
import urllib.request
from decimal import Decimal
from local_indicators import check_local_indicators

METEORA_PORTFOLIO_API = "https://dlmm.datapi.meteora.ag/portfolio/open"

# Resolved from this file's own location (<profile>/skills/solana-dlmm/scripts/) so the
# script works whether it's a copy or a symlink into a Hermes profile — no install-time
# path rewrite needed.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROFILE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(SCRIPT_DIR)))

def get_wallet_address():
    try:
        with open(os.path.join(PROFILE_DIR, ".env")) as f:
            for line in f:
                if line.startswith("SOLANA_PUBLIC_KEY="):
                    return line.split("=", 1)[1].strip().strip('"\'')
    except Exception:
        pass
    return None

def get_meteora_portfolio_positions(wallet_address):
    """Returns dict keyed by position address using Meteora Portfolio HTTP API.
    More reliable than DLMM SDK getAllLbPairPositionsByUser which requires fully initialized bin arrays."""
    try:
        url = f"{METEORA_PORTFOLIO_API}?user={wallet_address}"
        req = urllib.request.Request(url, headers={"User-Agent": "dlmm-lp/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        sol_price_usd = float(data.get("solPrice") or 0.0)
        result = {}
        for pool_data in (data.get("pools") or []):
            pool_addr = pool_data.get("poolAddress")
            oor_set = set(pool_data.get("positionsOutOfRange") or [])
            for pos_addr in (pool_data.get("listPositions") or []):
                result[pos_addr] = {
                    "position": pos_addr,
                    "pool": pool_addr,
                    "is_out_of_range": pos_addr in oor_set,
                    "pnl_pct": float(pool_data.get("pnlPctChange") or 0.0),
                    "pnl_sol": float(pool_data.get("pnlSol") or 0.0),
                    "fee_per_tvl_24h": float(pool_data.get("feePerTvl24h") or 0.0),
                    "pool_price": float(pool_data.get("poolPrice") or 0.0),
                    "unclaimed_fees_sol": float(pool_data.get("unclaimedFeesSol") or 0.0),
                    "balances_sol": float(pool_data.get("balancesSol") or 0.0),
                    "sol_price_usd": sol_price_usd,
                }
        return result, None
    except Exception as e:
        return None, str(e)

STOP_LOSS_PCT = -25.0
TAKE_PROFIT_PCT = 50.0
MAX_OOR_MINUTES = 30
# Turnover fast-cycle (Meridian-style): an OOR turnover position is idle
# fee-capture capital, so it re-centers after minutes — not the multi-hour
# patience of the thesis modes. The 20s monitor loop makes this cadence real.
TURNOVER_MAX_OOR_MINUTES = 2
# Turnover rebalance circuit breaker: re-centers stop once the pool's cumulative
# realized PnL across rebalance closes (24h window) drops below this many SOL.
# Replaces a count cap as the primary guard — the count backstop stays at 20/24h.
TURNOVER_CB_LOSS_SOL = -0.05
SOL_MINT = "So11111111111111111111111111111111111111112"
DEFAULT_DEPLOY_SOL = 0.5
TRAILING_TRIGGER_PCT = 5.0
TRAILING_DROP_PCT = 1.5
MIN_FEE_TVL_24H_LIMIT = 1.0
MIN_AGE_BEFORE_YIELD_CHECK = 60.0
# Exit-side liquidity floor. Below the $10k entry TVL gate on purpose — only fires
# when a pool's liquidity DRAINS after entry (the "can't exit cleanly" scenario).
MIN_EXIT_LIQUIDITY_USD = 7000.0
# Emergency SL floor sits this far below the configured hard SL. Below the floor the
# close bypasses the age grace, AI holds, indicator timing, and even --report-only.
EMERGENCY_SL_BUFFER_PCT = 3.0
# Trailing TP must lock at least this much (≈ round-trip swap cost) to count as a
# take-profit. Below it, a floor breach gets one-tick gap-through grace first.
TRAILING_MIN_LOCK_PCT = 0.3

def trailing_floor_pct(peak_pnl, trailing_drop_pct):
    """Profit-ratchet floor for the trailing exit. Tight near activation, locks
    progressively more profit as the peak grows, and gives big winners room to run
    instead of a flat drop — a flat drop (or another rule) tends to cut every
    position early, capping the best wins at a few percent."""
    if peak_pnl >= 20.0:
        return max(14.0, peak_pnl * 0.70)
    if peak_pnl >= 10.0:
        return max(6.0, peak_pnl - 4.0)
    if peak_pnl >= 5.0:
        return max(2.0, peak_pnl - 2.5)
    return peak_pnl - trailing_drop_pct

def log_close(pool, pair, meta, pos_addr, pnl_pct, realized_sol, fee_per_tvl_24h,
              age_min, reason, txs, dry_run):
    """Append a uniform close record to memories/dlmm_closes.jsonl. Every monitor
    close is journaled here; reconcile against the Meteora portfolio API (ground
    truth) with dlmm_reconcile.py."""
    entry = {
        "ts": int(time.time()),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": "monitor",
        "pool": pool,
        "pair": pair,
        "position": pos_addr,
        "base_mint": meta.get("base_mint"),
        "mode": meta.get("mode"),
        "pnl_pct": round(float(pnl_pct), 4),
        "pnl_sol": round(float(realized_sol), 6) if realized_sol is not None else None,
        "fee_per_tvl_24h": round(float(fee_per_tvl_24h), 4),
        "age_min": round(float(age_min), 1),
        "reason": reason,
        "txs": txs,
        "dry_run": bool(dry_run),
        # Entry-time signal snapshot (written by dlmm_pipeline.py at deploy) —
        # dlmm_weights.py correlates these with pnl_pct to learn signal weights.
        "signal": meta.get("signal"),
    }
    try:
        path = os.path.join(PROFILE_DIR, "memories", "dlmm_closes.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        print(f"⚠️ Failed to write close journal: {e}")
    # Pool memory: per-pool close outcomes, read by dlmm_pipeline.py's
    # "past losses" skip gate. Last 10 closes, 30-day expiry, live closes only.
    if not dry_run:
        try:
            rec = json.dumps({
                "ts": entry["ts"],
                "pnl_pct": entry["pnl_pct"],
                "pnl_sol": entry["pnl_sol"],
                "mode": entry["mode"],
                "reason": (reason or "")[:80],
            })
            run_command(f"redis-cli lpush \"sol:dlmm:history:pool:{pool}\" '{rec}'")
            run_command(f"redis-cli ltrim \"sol:dlmm:history:pool:{pool}\" 0 9")
            run_command(f"redis-cli expire \"sol:dlmm:history:pool:{pool}\" 2592000")
        except Exception as e:
            print(f"⚠️ Failed to write pool memory: {e}")

def load_soul_dlmm_params():
    params = {
        "STOP_LOSS_PCT": float(STOP_LOSS_PCT),
        "TRAILING_TRIGGER_PCT": float(TRAILING_TRIGGER_PCT),
        "TRAILING_DROP_PCT": float(TRAILING_DROP_PCT),
        "MAX_BINS_PUMPED_ABOVE": 10,
        "MAX_OOR_MINUTES": int(MAX_OOR_MINUTES),
        "TURNOVER_MAX_OOR_MINUTES": int(TURNOVER_MAX_OOR_MINUTES),
        "TURNOVER_CB_LOSS_SOL": float(TURNOVER_CB_LOSS_SOL),
        "MIN_AGE_BEFORE_YIELD_CHECK": float(MIN_AGE_BEFORE_YIELD_CHECK),
        "MIN_FEE_TVL_24H_LIMIT": float(MIN_FEE_TVL_24H_LIMIT),
        "TIMEFRAME": "24h",
        "STRATEGY": "spot",
        "INDICATORS_ENABLED": False,
        "INDICATORS_PRESET": "supertrend_or_rsi",
        "SLIPPAGE_BPS": 1000,
        "MIN_EXIT_LIQUIDITY_USD": float(MIN_EXIT_LIQUIDITY_USD)
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
                
        for line in sec9_lines:
            if not line.startswith("*"):
                continue
            if ":" not in line:
                continue
            parts = line.split(":", 1)
            name = parts[0].replace("*", "").strip()
            value_part = parts[1].strip()
            
            # String and boolean extraction
            if "Timeframe" in name:
                token = re.split(r'[\s(]', value_part)[0].strip().lower()
                params["TIMEFRAME"] = token
                continue
            elif "Strategy" in name:
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
                
            if "Hard Stop-Loss" in name:
                params["STOP_LOSS_PCT"] = val
            elif "Trailing TP Trigger" in name:
                params["TRAILING_TRIGGER_PCT"] = val
            elif "Trailing TP Drop" in name:
                params["TRAILING_DROP_PCT"] = val
            elif "Max Bins Pumped Above" in name:
                params["MAX_BINS_PUMPED_ABOVE"] = int(val)
            elif "Turnover Max OOR Minutes" in name:
                params["TURNOVER_MAX_OOR_MINUTES"] = int(val)
            elif "Turnover CB Loss SOL" in name:
                params["TURNOVER_CB_LOSS_SOL"] = val
            elif "Max Out of Range Minutes" in name:
                params["MAX_OOR_MINUTES"] = int(val)
            elif "Min Age for Yield Check" in name:
                params["MIN_AGE_BEFORE_YIELD_CHECK"] = val
            elif "Min 24h Fee/TVL for Yield Check" in name:
                params["MIN_FEE_TVL_24H_LIMIT"] = val
            elif "Slippage" in name:
                params["SLIPPAGE_BPS"] = int(val)
            elif "Min Exit Liquidity" in name:
                params["MIN_EXIT_LIQUIDITY_USD"] = val
    except Exception as e:
        print(f"Error parsing SOUL.md parameters: {e}")
        return params
    return params


def get_pool_liquidity_usd(pool, base_mint):
    """Live pool liquidity (USD) from DexScreener, looked up by pool address (falls
    back to the base mint's pairs). Returns None on any failure — callers MUST treat
    None as 'unknown, do not act' (fail-open) so a transient API blip never force-closes."""
    import urllib.request
    urls = [f"https://api.dexscreener.com/latest/dex/pairs/solana/{pool}"]
    if base_mint:
        urls.append(f"https://api.dexscreener.com/latest/dex/tokens/{base_mint}")
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=6) as resp:
                data = json.loads(resp.read())
            pairs = data.get("pairs") or ([data["pair"]] if data.get("pair") else [])
            if not pairs:
                continue
            for p in pairs:
                if (p.get("pairAddress") or "").lower() == pool.lower():
                    return float((p.get("liquidity") or {}).get("usd") or 0.0)
            return float((pairs[0].get("liquidity") or {}).get("usd") or 0.0)
        except Exception:
            continue
    return None

EXECUTOR_PATH = os.path.join(SCRIPT_DIR, "dlmm_executor.js")

def run_command(cmd, timeout=30):
    try:
        res = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return res.stdout.strip(), res.stderr.strip(), res.returncode
    except Exception as e:
        return "", str(e), -1

def run_command_json(cmd, timeout=30):
    out, err, code = run_command(cmd, timeout=timeout)
    if code != 0 or not out:
        return None, err or "No output from command"
    try:
        json_line = None
        for line in reversed(out.split('\n')):
            stripped = line.strip()
            if (stripped.startswith('{') and stripped.endswith('}')) or (stripped.startswith('[') and stripped.endswith(']')):
                json_line = stripped
                break
        if not json_line:
            return None, f"No JSON found. Raw: {out}"
        return json.loads(json_line), None
    except Exception as e:
        return None, f"JSON parse error: {e}. Raw: {out}"

def get_wallet_sol_balance():
    try:
        data, err = run_command_json(f"node {EXECUTOR_PATH} spl-balance SOL")
        if data and "balance" in data:
            return float(data.get("balance", 0.0))
    except Exception as e:
        print(f"Warning: Error fetching SOL balance via executor: {e}")
    return 10.0

def get_active_positions():
    out, _, _ = run_command("redis-cli smembers sol:dlmm:active_positions")
    if not out:
        return []
    return [line.strip() for line in out.split('\n') if line.strip()]

def get_position_metadata(position_address):
    out, _, _ = run_command(f"redis-cli get \"sol:dlmm:position:{position_address}\"")
    if not out or out == "(nil)":
        return None
    try:
        return json.loads(out)
    except:
        return None

def build_report_row(pos_addr, pair, pool, meta, pool_liquidity_usd, pnl_pct, pnl_sol_actual,
                      in_range, fee_per_tvl_24h, api_available, bp, active_price, entry_price,
                      peak_pnl, trailing_active, close_reason, now):
    """Builds one status-table row dict, shared by --report-only and live (HOLD) runs
    so both render through the same Telegram template instead of drifting per-run."""
    oor_val, _, _ = run_command(f"redis-cli get sol:dlmm:position:{pos_addr}:oor_since")
    oor_minutes = (now - int(oor_val)) / 60.0 if oor_val and oor_val != "(nil)" else 0.0
    hold_val, _, _ = run_command(f"redis-cli get \"sol:dlmm:position:{pos_addr}:ai_hold_until\"")
    ai_hold_active = hold_val and hold_val != "(nil)" and int(hold_val) > now
    return {
        "position": pos_addr,
        "pair": pair,
        "pool": pool,
        "base_mint": meta.get("base_mint"),
        "base_symbol": meta.get("base_symbol"),
        "mode": meta.get("mode", "multiday"),
        "pool_liquidity_usd": round(pool_liquidity_usd, 0) if pool_liquidity_usd is not None else None,
        "strategy": meta.get("strategy", "spot"),
        "pnl_pct": round(pnl_pct, 4),
        "pnl_sol": round(pnl_sol_actual, 6) if pnl_sol_actual is not None else None,
        "in_range": in_range,
        "oor_minutes": round(oor_minutes, 1),
        "fee_per_tvl_24h": round(fee_per_tvl_24h, 2),
        # Deterministic days-to-breakeven: loss% / (fee% earned per 24h).
        # Computed here so the AI does NOT do this division by hand (it gets it wrong).
        # Only meaningful when underwater AND earning fees; otherwise 0 (= no breakeven concern).
        "break_even_days": round(abs(pnl_pct) / fee_per_tvl_24h, 2) if (pnl_pct < 0 and fee_per_tvl_24h > 0) else 0.0,
        "unclaimed_fees_sol": round(bp.get("unclaimed_fees_sol", 0.0) if (api_available and bp) else 0.0, 6),
        "pool_price": active_price,
        "entry_price": entry_price,
        "age_minutes": round((now - meta.get("deployed_at", now)) / 60.0, 1),
        "size_sol": meta.get("size_sol", 0.5),
        "peak_pnl": round(peak_pnl, 4),
        "trailing_active": trailing_active,
        "ai_hold_active": ai_hold_active,
        "triggered_rules": [close_reason] if close_reason else [],
        "hard_rule": close_reason and ("Stop-Loss" in close_reason or "Pumped far" in close_reason),
    }

def render_status_report(report_rows, sol_price_usd, trailing_trigger_pct, min_fee_tvl_24h_limit, max_oor_minutes, header_label="DLMM Position Status"):
    """Renders the fixed Telegram status template from report_rows. Used by both
    --report-only and live runs (for positions that end the cycle in HOLD) so the
    delivered format never depends on which code path produced the data."""
    wib_str = time.strftime("%H:%M WIB", time.gmtime(int(time.time()) + 7 * 3600))
    lines = [f"{header_label} — {wib_str}", f"📊 Active Positions: {len(report_rows)}"]
    if not report_rows:
        lines.append("\nNo active positions. Bot is idle.")
    for r in report_rows:
        age_h = int(r["age_minutes"] // 60)
        age_m = int(r["age_minutes"] % 60)
        age_str = f"{age_h}h{age_m:02d}m" if age_h > 0 else f"{r['age_minutes']:.0f}m"
        range_str = "🟢 In Range" if r["in_range"] else f"🔴 OOR {r['oor_minutes']:.0f}m"
        risk = []
        if r["triggered_rules"]:
            risk.append(f"⚠️ Triggered: {', '.join(r['triggered_rules'])}")
        else:
            risk.append("✅ No triggered rules")
        if r["trailing_active"]:
            risk.append(f"⚠️ Trailing stop ACTIVE (peak {r['peak_pnl']:+.2f}%)")
        else:
            risk.append(f"✅ No trailing stop (needs +{trailing_trigger_pct:.0f}% PnL)")
        if r["ai_hold_active"]:
            risk.append("⚠️ AI hold override active")
        else:
            risk.append("✅ No AI hold override")
        if r["fee_per_tvl_24h"] >= min_fee_tvl_24h_limit:
            risk.append(f"✅ Healthy Fee/TVL ({r['fee_per_tvl_24h']:.2f}% ≥ {min_fee_tvl_24h_limit:.0f}% min)")
        else:
            risk.append(f"⚠️ Low Fee/TVL ({r['fee_per_tvl_24h']:.2f}% < {min_fee_tvl_24h_limit:.0f}% min)")
        if r["in_range"]:
            risk.append("✅ In range (earning fees)")
        else:
            risk.append(f"⚠️ Out of range {r['oor_minutes']:.0f}m (limit {max_oor_minutes}m)")
        if r["triggered_rules"]:
            summary = f"⚠️ Rule triggered: {r['triggered_rules'][0]}. AI reviewing."
        elif not r["in_range"]:
            summary = f"⏳ Out of range {r['oor_minutes']:.0f}m. Monitoring."
        else:
            summary = "Position healthy, earning fees. No action needed."
        # Pipe-table card: rendered natively by the deliverer's Bot API 10.1
        # rich-message path (config platforms.telegram.extra.rich_messages:
        # true). Tables trigger _needs_rich_rendering -> sendRichMessage, so
        # they render as a real table with no MarkdownV2 flattening/escapes.
        # Compact layout: 4 merged rows instead of 12, and only ⚠️ risk
        # bullets are printed — a healthy position shows just table + summary.
        pnl_usd_str = ""
        if sol_price_usd:
            pnl_usd = (r['pnl_sol'] * sol_price_usd) if r.get('pnl_sol') is not None else (r['size_sol'] * (r['pnl_pct'] / 100) * sol_price_usd)
            pnl_usd_str = f" (${pnl_usd:+.2f})"
        fees_usd_str = f" (${r['unclaimed_fees_sol'] * sol_price_usd:.2f})" if sol_price_usd else ""
        size_str = f"{r['size_sol']:.4f}".rstrip('0').rstrip('.')
        warnings = [b for b in risk if b.startswith("⚠️")]
        lines += [
            "",
            "---",
            "",
            f"### {r['pair']} ({r['mode']})",
            f"`{r['position']}`",
            "",
            "| Metric | Value |",
            "|--------|-------|",
            f"| PnL | {r['pnl_pct']:+.2f}%{pnl_usd_str} · peak {r['peak_pnl']:+.2f}% |",
            f"| Range | {range_str} · age {age_str} |",
            f"| Fee/TVL 24h | {r['fee_per_tvl_24h']:.2f}% · unclaimed {r['unclaimed_fees_sol']:.4f} SOL{fees_usd_str} |",
            f"| Size | {size_str} SOL @ {r['entry_price']:.8f} → {r['pool_price']:.8f} |",
        ] + [f"- {b}" for b in warnings] + [
            "",
            f"→ {summary}",
        ]
    return "\n".join(lines)

# local indicators are imported and check_local_indicators is used directly below.


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-enforce", action="store_true", help="With --report-only: do not execute emergency closes (pure reporting)")
    parser.add_argument("--report-only", action="store_true", help="Output position status JSON without executing any closes")
    parser.add_argument("--override-close", type=str, default=None, metavar="POSITION_ADDR", help="AI-decided force-close for a specific position")
    parser.add_argument("--override-hold", type=str, default=None, metavar="POSITION_ADDR", help="AI-decided hold — skip auto-close rules for this position")
    parser.add_argument("--hold-minutes", type=int, default=30, help="Minutes to hold (used with --override-hold)")
    parser.add_argument("--reset-trailing", type=str, default=None, metavar="POSITION_ADDR", help="Reset peak_pnl and trailing_active after a standalone fee claim")
    parser.add_argument("--reason", type=str, default="AI decision", help="Reason string for close/hold (logged)")
    parser.add_argument("--force", action="store_true", help="Bypass the health GUARD on --override-close (close a healthy in-range high-fee position anyway)")
    parser.add_argument("--cleanup-tokens", action="store_true", help="Swap all leftover SPL token balances back to SOL")
    parser.add_argument("--min-swap-sol", type=float, default=0.005, help="Minimum SOL value threshold to trigger cleanup swap (default: 0.005)")
    cli = parser.parse_args()

    print("🔄 Starting DLMM Position Monitor")
    
    params = load_soul_dlmm_params()
    stop_loss_pct = params["STOP_LOSS_PCT"]
    trailing_trigger_pct = params["TRAILING_TRIGGER_PCT"]
    trailing_drop_pct = params["TRAILING_DROP_PCT"]
    max_bins_pumped_above = params["MAX_BINS_PUMPED_ABOVE"]
    max_oor_minutes = params["MAX_OOR_MINUTES"]
    turnover_max_oor_minutes = params["TURNOVER_MAX_OOR_MINUTES"]
    min_age_before_yield_check = params["MIN_AGE_BEFORE_YIELD_CHECK"]
    min_fee_tvl_24h_limit = params["MIN_FEE_TVL_24H_LIMIT"]
    min_exit_liquidity_usd = params["MIN_EXIT_LIQUIDITY_USD"]
    
    print(f"Loaded SOUL.md parameters -> SL: {stop_loss_pct:.1f}%, Trailing TP Trigger: {trailing_trigger_pct:.1f}%, Trailing TP Drop: {trailing_drop_pct:.1f}%, Max Bins Pumped Above: {max_bins_pumped_above}, Max OOR: {max_oor_minutes}m (turnover {turnover_max_oor_minutes}m), Min Age for Yield Check: {min_age_before_yield_check:.1f}m, Min Fee/TVL: {min_fee_tvl_24h_limit:.2f}%")
    
    # --reset-trailing: reset peak_pnl + trailing_active after a standalone fee claim
    if cli.reset_trailing:
        meta = get_position_metadata(cli.reset_trailing)
        if not meta:
            print(f"Position {cli.reset_trailing} not found in Redis.")
            sys.exit(1)
        meta["peak_pnl"] = 0.0
        meta["trailing_active"] = False
        run_command(f"redis-cli set \"sol:dlmm:position:{cli.reset_trailing}\" '{json.dumps(meta)}'")
        print(f"🔄 Trailing TP state reset for {meta.get('pair', cli.reset_trailing)} — peak_pnl→0, trailing_active→False")
        sys.exit(0)

    # --cleanup-tokens: swap all leftover SPL token balances back to SOL
    if cli.cleanup_tokens:
        print("🧹 Token Cleanup: scanning wallet for leftover SPL tokens...")
        tokens_data, tokens_err = run_command_json(f"node {EXECUTOR_PATH} list-tokens")
        if not tokens_data or not tokens_data.get("success"):
            print(f"Failed to list tokens: {tokens_err or tokens_data}")
            sys.exit(1)
        tokens = tokens_data.get("tokens", [])
        print(f"Found {len(tokens)} non-zero SPL token(s) in wallet.")
        swapped = 0
        skipped = 0
        for t in tokens:
            mint = t.get("mint")
            balance = float(t.get("balance", 0))
            if not mint or balance <= 0:
                continue
            # Get current price in SOL via active-bin lookup or DexScreener
            try:
                import urllib.request
                dex_url = f"https://api.dexscreener.com/latest/dex/tokens/{mint}"
                req = urllib.request.Request(dex_url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=6) as resp:
                    dex_data = json.loads(resp.read())
                pairs = dex_data.get("pairs") or []
                sol_pairs = [p for p in pairs if (p.get("quoteToken") or {}).get("symbol") == "SOL"]
                price_sol = float((sol_pairs[0].get("priceNative") or 0)) if sol_pairs else 0.0
            except Exception:
                price_sol = 0.0
            est_sol = balance * price_sol
            print(f"  Token {mint[:8]}...: {balance:.4f} tokens × {price_sol:.8f} SOL = ~{est_sol:.4f} SOL")
            if est_sol < cli.min_swap_sol:
                print(f"  ⏭ Skipping (value {est_sol:.4f} SOL < min {cli.min_swap_sol} SOL)")
                skipped += 1
                continue
            print(f"  🔄 Swapping {balance:.4f} tokens back to SOL...")
            # Cleanup sweeps leftover/stranded tokens — force liquidation at high impact tolerance (intent is "get them out").
            # 90% cap (vs the 50% on-close guard) so thin/dumping pools actually clear instead of re-aborting forever.
            swap_res, swap_err = run_command_json(f"node {EXECUTOR_PATH} swap {mint} SOL {balance} 90 300", timeout=90)
            if swap_res and swap_res.get("success"):
                print(f"  ✅ Swapped. Tx: {swap_res.get('txHash', '?')}")
                swapped += 1
            else:
                print(f"  ❌ Swap failed: {swap_err or swap_res.get('error') if swap_res else 'No response'}")
        print(f"\n🧹 Cleanup done: {swapped} swapped, {skipped} skipped (dust).")
        sys.exit(0)

    # --override-hold: set AI hold flag in Redis, skip auto-close for N minutes
    if cli.override_hold:
        hold_until = int(time.time()) + (cli.hold_minutes * 60)
        run_command(f"redis-cli set \"sol:dlmm:position:{cli.override_hold}:ai_hold_until\" {hold_until} EX {cli.hold_minutes * 60}")
        print(f"✋ AI HOLD set for {cli.override_hold} — auto-close suppressed for {cli.hold_minutes}m. Reason: {cli.reason}")
        sys.exit(0)

    # --override-close: force-close a specific position immediately (AI decision)
    if cli.override_close:
        meta = get_position_metadata(cli.override_close)
        if not meta:
            print(f"Position {cli.override_close} not found in Redis.")
            sys.exit(1)
        is_dry = meta.get("tx_hash") == "DRY_RUN_TX_HASH"

        # Fetch live metrics (shared by the health GUARD below and daily-PnL booking after close).
        guard_pnl_pct = None
        guard_fee_tvl = 0.0
        guard_in_range = None
        guard_break_even = 0.0
        if not is_dry:
            guard_wallet = get_wallet_address()
            if guard_wallet:
                guard_bp_dict, _ = get_meteora_portfolio_positions(guard_wallet)
                guard_bp = guard_bp_dict.get(cli.override_close) if guard_bp_dict else None
                if guard_bp:
                    guard_pnl_pct = guard_bp.get("pnl_pct", 0.0)
                    guard_fee_tvl = guard_bp.get("fee_per_tvl_24h", 0.0)
                    guard_in_range = not guard_bp.get("is_out_of_range", False)
                    if guard_pnl_pct < 0 and guard_fee_tvl > 0:
                        guard_break_even = round(abs(guard_pnl_pct) / guard_fee_tvl, 2)

        # HEALTH GUARD — block closing a healthy, in-range, high-fee, near-break-even position.
        # Mirrors the DLMM cron's GUARD so ANY caller (gateway agent, manual, cron) is bound by it.
        # Pass --force to override (genuine manual intervention). Skipped when live data is unavailable
        # (can't verify → defer to caller) and for dry-run positions.
        if (not cli.force and guard_in_range is True and guard_fee_tvl >= 10
                and guard_pnl_pct is not None and guard_pnl_pct > -20 and guard_break_even < 5):
            print(f"🛑 GUARD: refusing to close healthy {meta.get('pair')} — "
                  f"in-range, fee/TVL {guard_fee_tvl:.1f}% (>=10), PnL {guard_pnl_pct:+.2f}% (>-20), "
                  f"break_even {guard_break_even}d (<5). Pass --force to override.")
            sys.exit(2)

        # AI HOLD CHECK — respect hold flag set by a previous cron run.
        # --override-close bypasses the rules-based path where hold is normally checked,
        # so we must re-check here. Bypass only when: --force is passed, OR PnL <= hard SL (-25%)
        # — genuine emergency that no hold should block.
        if not cli.force and not is_dry:
            hold_val, _, _ = run_command(f"redis-cli get \"sol:dlmm:position:{cli.override_close}:ai_hold_until\"")
            if hold_val and hold_val != "(nil)":
                try:
                    hold_until_ts = int(hold_val)
                    now_ts = int(time.time())
                    if hold_until_ts > now_ts:
                        is_emergency = guard_pnl_pct is not None and guard_pnl_pct <= -25.0
                        if not is_emergency:
                            mins_left = (hold_until_ts - now_ts) / 60.0
                            print(f"✋ AI HOLD active for {meta.get('pair')} ({mins_left:.0f}m left) — "
                                  f"refusing --override-close. PnL {guard_pnl_pct:+.2f}% above hard SL. "
                                  f"Pass --force to override the hold.")
                            sys.exit(2)
                        else:
                            print(f"⚡ AI HOLD bypassed: PnL {guard_pnl_pct:+.2f}% <= -25% (hard SL emergency).")
                except (ValueError, TypeError):
                    pass

        print(f"🤖 AI FORCE-CLOSE: {meta.get('pair')} — Reason: {cli.reason}")
        env_prefix = "DRY_RUN=true " if is_dry else ""
        close_res, close_err = run_command_json(f"{env_prefix}DLMM_CLOSE_AUTH=1 node {EXECUTOR_PATH} close {cli.override_close}")
        if close_res and close_res.get("success"):
            run_command(f"redis-cli srem sol:dlmm:active_positions \"{cli.override_close}\"")
            run_command(f"redis-cli del \"sol:dlmm:position:{cli.override_close}\"")
            run_command(f"redis-cli del \"sol:dlmm:position:{cli.override_close}:oor_since\"")
            run_command(f"redis-cli del \"sol:dlmm:position:{cli.override_close}:ai_hold_until\"")
            txs = close_res.get("txHashes", [close_res.get("txHash", "?")])
            print(f"✅ Force-closed. TX: {txs[0] if txs else '?'}")
            # Set re-entry cooldown (same as auto-close path)
            base_symbol_cd = meta.get("base_symbol", meta.get("pair", "").split("-")[0]).upper()
            reason_lower = cli.reason.lower()
            is_dump_close = any(kw in reason_lower for kw in ("trailing", "dump", "stop-loss", "stop_loss", "sell pressure", "momentum"))
            cooldown_secs = 7200 if is_dump_close else 3600
            cooldown_key = f"sol:dlmm:cooldown:{base_symbol_cd}"
            run_command(f"redis-cli set \"{cooldown_key}\" \"{cli.reason[:120]}\" ex {cooldown_secs}")
            print(f"🚫 Re-entry cooldown set for {base_symbol_cd}: {cooldown_secs // 3600}h")
            # Book daily realized PnL — same as the auto-close path so override-closes are not invisible to WR/stats.
            if not is_dry and guard_pnl_pct is not None:
                today = time.strftime("%Y-%m-%d")
                pnl_key = f"sol:dlmm:pnl:daily:{today}"
                current_pnl_str, _, _ = run_command(f"redis-cli hget {pnl_key} total_sol")
                current_pnl = float(current_pnl_str) if current_pnl_str and current_pnl_str != "(nil)" else 0.0
                size_sol = meta.get("size_sol", DEFAULT_DEPLOY_SOL)
                # Use Meteora's exact pnlSol when available; fall back to pct×size estimate
                _guard_pnl_sol = guard_bp.get("pnl_sol") if guard_bp else None
                realized_sol = _guard_pnl_sol if _guard_pnl_sol is not None else size_sol * (guard_pnl_pct / 100.0)
                run_command(f"redis-cli hset {pnl_key} total_sol {current_pnl + realized_sol}")
                run_command(f"redis-cli hincrby {pnl_key} count_exits 1")
                if realized_sol < 0:
                    run_command(f"redis-cli hincrby {pnl_key} count_losses 1")
                    # Repeat-loss escalation: extend cooldown if same token loses repeatedly within 7 days
                    loss_streak_key = f"sol:dlmm:loss_streak:{base_symbol_cd}"
                    run_command(f"redis-cli incr \"{loss_streak_key}\"")
                    run_command(f"redis-cli expire \"{loss_streak_key}\" 604800")
                    streak_str, _, _ = run_command(f"redis-cli get \"{loss_streak_key}\"")
                    loss_streak = int(streak_str) if streak_str and streak_str.strip().lstrip('-').isdigit() else 1
                    if loss_streak >= 2:
                        escalated_secs = 259200 if loss_streak >= 3 else 86400  # 3+ losses→72h, 2 losses→24h
                        run_command(f"redis-cli expire \"{cooldown_key}\" {escalated_secs}")
                        print(f"🔴 REPEAT LOSS #{loss_streak} in 7d — escalated {base_symbol_cd} cooldown to {escalated_secs // 3600}h")
                else:
                    run_command(f"redis-cli del \"sol:dlmm:loss_streak:{base_symbol_cd}\"")  # reset streak on profit
                print(f"📊 Daily PnL booked: {realized_sol:+.4f} SOL ({guard_pnl_pct:+.2f}%)")
            # Auto-swap base token back to SOL
            base_mint = meta.get("base_mint")
            pool_addr = meta.get("pool")
            strategy = meta.get("strategy", "spot")
            skip_swap = (strategy == "single_sided_reseed")
            if base_mint and base_mint != SOL_MINT and not skip_swap:
                force_pair = meta.get("pair", cli.override_close)
                print(f"Checking {force_pair} base token balance for auto-swap...")
                time.sleep(5)
                current_price = float(meta.get("entry_price", 0))
                ab_data, _ = run_command_json(f"node {EXECUTOR_PATH} active-bin {pool_addr}")
                if ab_data and ab_data.get("price"):
                    current_price = float(ab_data["price"])
                # Harden dump detection: a token trading below entry is dumping regardless of the AI's reason wording.
                # Force high-impact liquidation so a crashing token is never left in the wallet on a vaguely-worded close.
                entry_px = float(meta.get("entry_price", 0) or 0)
                if entry_px > 0 and current_price > 0:
                    price_change_pct = (current_price - entry_px) / entry_px * 100
                    if price_change_pct <= -5 and not is_dump_close:
                        is_dump_close = True
                        print(f"⚠️ Dump detected by price ({price_change_pct:+.1f}% vs entry) — forcing high-impact liquidation despite reason wording.")
                bal_data, bal_err = run_command_json(f"{env_prefix}node {EXECUTOR_PATH} spl-balance {base_mint}")
                if bal_data and float(bal_data.get("balance", 0)) > 0:
                    token_balance = float(bal_data["balance"])
                    est_sol = token_balance * current_price
                    print(f"Base token balance: {token_balance} (~{est_sol:.4f} SOL)")
                    if est_sol > 0.01 or is_dry:
                        # Dump exits force-liquidate (high impact OK); normal exits use tight 5% guard.
                        swap_max_impact = 50 if is_dump_close else 15
                        swap_slip_bps = 300 if is_dump_close else 300
                        print(f"Executing auto-swap back to SOL for {token_balance} tokens (max_impact {swap_max_impact}%)...")
                        swap_res, swap_err = run_command_json(f"{env_prefix}node {EXECUTOR_PATH} swap {base_mint} SOL {token_balance} {swap_max_impact} {swap_slip_bps}", timeout=90)
                        if swap_res and swap_res.get("success"):
                            print(f"✅ Auto-swapped back to SOL. Tx: {swap_res.get('txHash', 'DRY_RUN_SWAP_TX_HASH')}")
                        else:
                            print(f"❌ Auto-swap failed: {swap_err or (swap_res.get('error') if swap_res else 'No response')}")
                    else:
                        print(f"Base token SOL value too small ({est_sol:.4f} SOL), skipping swap")
                else:
                    print("Base token balance is zero, skipping swap")
        else:
            print(f"❌ Force-close failed: {close_err or close_res}")
        sys.exit(0)

    active_positions = get_active_positions()
    if not active_positions:
        print("No active DLMM positions found in Redis.")
        sys.exit(0)
        
    # Get current positions from Meteora Portfolio API (reliable vs SDK which requires fully-initialized bin arrays)
    blockchain_positions = {}
    api_available = False
    meteora_sol_price = 0.0
    wallet_address = get_wallet_address()
    if wallet_address:
        bp_dict, bp_err = get_meteora_portfolio_positions(wallet_address)
        if bp_dict is not None:
            blockchain_positions = bp_dict
            api_available = True
            meteora_sol_price = next((v["sol_price_usd"] for v in bp_dict.values() if v.get("sol_price_usd")), 0.0)
            print(f"Meteora Portfolio API: {len(bp_dict)} position(s) found on-chain")
        else:
            print(f"Meteora Portfolio API failed ({bp_err}). Falling back to SDK.")
            bp_list, bp_err2 = run_command_json(f"node {EXECUTOR_PATH} positions")
            if bp_list and isinstance(bp_list, list):
                for bp in bp_list:
                    blockchain_positions[bp["position"]] = bp
    else:
        print("Warning: SOLANA_PUBLIC_KEY not found. Skipping on-chain verification.")

    now = int(time.time())
    report_rows = []

    # --- Reconciliation: adopt/reclaim on-chain positions Redis doesn't know about ---
    # The wide-range deploy path can mint a position NFT and then fail to add liquidity,
    # leaving an untracked 0-deposit zombie on-chain that the monitor is blind to (this is
    # exactly how the 5-on-chain / 3-tracked mismatch happens). Reconcile the full on-chain
    # set against Redis: close empty zombies to reclaim rent; adopt funded-but-untracked
    # positions so they get managed. Skip entirely in report-only mode (never mutate).
    if api_available and blockchain_positions:
        tracked = set(active_positions)
        for oc_addr, oc_bp in blockchain_positions.items():
            if oc_addr in tracked:
                continue
            bal_sol = oc_bp.get("balances_sol", 0.0)
            oc_pool = oc_bp.get("pool", "")
            if bal_sol < 1e-6:
                # Empty zombie NFT — reclaim rent.
                if cli.report_only:
                    print(f"🧟 [report-only] Untracked EMPTY position {oc_addr} (pool {oc_pool}) — would close to reclaim rent.")
                    continue
                print(f"🧟 Untracked EMPTY position {oc_addr} (pool {oc_pool}) — closing to reclaim rent.")
                close_res, close_err = run_command_json(f"DLMM_CLOSE_AUTH=1 node {EXECUTOR_PATH} close {oc_addr}")
                if close_res and close_res.get("success"):
                    print(f"✅ Reclaimed empty position {oc_addr}.")
                else:
                    print(f"⚠️ Failed to close empty position {oc_addr}: {close_err or (close_res and close_res.get('error'))}")
            else:
                # Funded but untracked — adopt into Redis so the main loop manages it.
                if cli.report_only:
                    print(f"➕ [report-only] Untracked FUNDED position {oc_addr} ({bal_sol:.4f} SOL) — would adopt.")
                    continue
                print(f"➕ Adopting untracked FUNDED position {oc_addr} (pool {oc_pool}, {bal_sol:.4f} SOL) into Redis.")
                run_command(f"redis-cli sadd sol:dlmm:active_positions \"{oc_addr}\"")
                adopt_meta = {
                    "pool": oc_pool, "pair": oc_pool, "base_mint": "", "base_symbol": "",
                    "entry_price": oc_bp.get("pool_price", 0.0), "entry_bin": 0,
                    "bins_below": 0, "bins_above": 0, "size_sol": bal_sol,
                    "deployed_at": now, "tx_hash": "ADOPTED", "strategy": "spot",
                    "amount_x": 0, "amount_y": 0, "adopted": True
                }
                run_command("redis-cli set \"sol:dlmm:position:%s\" '%s'" % (oc_addr, json.dumps(adopt_meta)))
        # Refresh active set after adoption so the main loop manages newly-adopted entries.
        active_positions = get_active_positions()

    for pos_addr in active_positions:
        meta = get_position_metadata(pos_addr)
        if not meta:
            # Clean up ghost key in set
            run_command(f"redis-cli srem sol:dlmm:active_positions \"{pos_addr}\"")
            print(f"Removed unregistered position {pos_addr} from Redis active set")
            continue
            
        pool = meta["pool"]
        pair = meta["pair"]
        # Skip orphan positions (deployment failures with no entry_price)
        if meta.get("orphan") or "entry_price" not in meta:
            print(f"⊘ Skipping orphan position {pos_addr} ({meta.get('pair', '?')}) — stranded NFT")
            continue
        
        entry_price = float(meta["entry_price"])
        bins_below = meta["bins_below"]
        
        # Check if position still exists on-chain
        bp = blockchain_positions.get(pos_addr)
        is_dry_run_stored = meta.get("tx_hash") == "DRY_RUN_TX_HASH"
        env_prefix = "DRY_RUN=true " if is_dry_run_stored else ""
        
        if not bp and not is_dry_run_stored:
            if not api_available:
                # API failed — cannot confirm position is gone, skip cleanup to avoid false wipe
                print(f"⚠️ API unavailable — cannot verify {pos_addr} ({pair}) on-chain. Skipping cleanup.")
                continue
            # Grace period: skip removal for positions deployed < 2h ago (RPC lag / TX confirmation delay)
            deployed_at = meta.get("deployed_at", 0)
            if now - deployed_at < 7200:
                print(f"⏳ Position {pos_addr} ({pair}) not found on-chain yet — deployed {int((now - deployed_at) / 60)}m ago, within grace period. Skipping.")
                continue
            # Position confirmed absent from Portfolio API — closed outside the bot
            print(f"⚠️ Position {pos_addr} ({pair}) confirmed absent from Meteora API. Cleaning up Redis state.")
            run_command(f"redis-cli srem sol:dlmm:active_positions \"{pos_addr}\"")
            run_command(f"redis-cli del \"sol:dlmm:position:{pos_addr}\"")
            run_command(f"redis-cli del \"sol:dlmm:position:{pos_addr}:oor_since\"")
            run_command(f"redis-cli del \"sol:dlmm:position:{pos_addr}:indicator_blocked_since\"")
            run_command(f"redis-cli del \"sol:dlmm:position:{pos_addr}:ai_hold_until\"")
            continue
            
        # Get current pool price and active bin (executor active-bin still used for binId)
        active_price = entry_price
        active_bin = meta.get("entry_bin")
        in_range = True
        pnl_pct = 0.0
        pnl_data = None
        fee_per_tvl_24h = 0.0

        pnl_sol_actual = None  # Meteora-exact SOL P&L; used at close-booking instead of size*pct
        if api_available and bp:
            # Use Meteora Portfolio API data (reliable, real-time)
            in_range = not bp.get("is_out_of_range", False)
            pnl_pct = bp.get("pnl_pct", 0.0)
            pnl_sol_actual = bp.get("pnl_sol")  # exact SOL P&L from Meteora
            fee_per_tvl_24h = bp.get("fee_per_tvl_24h", 0.0)
            if bp.get("pool_price", 0.0) > 0:
                active_price = bp["pool_price"]
            # Still call active-bin for binId (needed for pumped-above check)
            ab_data, ab_err = run_command_json(f"node {EXECUTOR_PATH} active-bin {pool}")
            if ab_data:
                active_bin = ab_data.get("binId")
                if not bp.get("pool_price"):
                    active_price = float(ab_data.get("price", entry_price))
            print(f"Meteora Portfolio API: {pnl_pct:+.2f}% PnL | Fee/TVL 24h: {fee_per_tvl_24h:.2f}% | Range: {'🟢 In' if in_range else '🔴 Out'}")
        elif not is_dry_run_stored:
            # Fallback: executor active-bin + pnl command
            ab_data, ab_err = run_command_json(f"node {EXECUTOR_PATH} active-bin {pool}")
            if ab_data:
                active_price = float(ab_data.get("price", entry_price))
                active_bin = ab_data.get("binId")
            pnl_data, pnl_err = run_command_json(f"node {EXECUTOR_PATH} pnl {pool} {pos_addr}")
            if pnl_data and pnl_data.get("success") != False:
                pnl_pct = float(pnl_data.get("pnl_pct", 0.0))
                in_range = pnl_data.get("in_range", True)
                fee_per_tvl_24h = float(pnl_data.get("fee_per_tvl_24h", 0.0))
                print(f"Executor PnL: {pnl_pct:+.2f}% | Fee/TVL 24h: {fee_per_tvl_24h:.2f}% | Range: {'🟢 In' if in_range else '🔴 Out'}")
            else:
                if entry_price > 0:
                    pnl_pct = ((active_price - entry_price) / entry_price) * 100.0
                if active_bin is not None:
                    lower_bin = meta.get("entry_bin", 0) - bins_below
                    upper_bin = meta.get("entry_bin", 0)
                    in_range = (active_bin >= lower_bin) and (active_bin <= upper_bin)
                print(f"Price proxy fallback: {pnl_pct:+.2f}% PnL | Range: {'🟢 In' if in_range else '🔴 Out'}")
        else:
            # Dry run
            ab_data, ab_err = run_command_json(f"node {EXECUTOR_PATH} active-bin {pool}")
            if ab_data:
                active_price = float(ab_data.get("price", entry_price))
                active_bin = ab_data.get("binId")
            if entry_price > 0:
                pnl_pct = ((active_price - entry_price) / entry_price) * 100.0
            if active_bin is not None:
                lower_bin = meta.get("entry_bin", 0) - bins_below
                upper_bin = meta.get("entry_bin", 0)
                in_range = (active_bin >= lower_bin) and (active_bin <= upper_bin)
            print(f"Dry Run Price Proxy: {pnl_pct:+.2f}% PnL | Range: {'🟢 In' if in_range else '🔴 Out'}")

        # Update Trailing Take-Profit State in Redis
        peak_pnl = float(meta.get("peak_pnl", 0.0))
        trailing_active = meta.get("trailing_active", False)
        
        # Track the peak PnL
        if pnl_pct > peak_pnl:
            peak_pnl = pnl_pct
            
        # Check trailing TP trigger — scale by fee/TVL so high-yield pools activate sooner
        # Low yield (<10%): trigger at 2% | Mid (10-30%): trigger at 3-5% | High (>30%): trigger at 5%+
        if fee_per_tvl_24h > 0:
            effective_trigger = max(2.0, min(trailing_trigger_pct, trailing_trigger_pct * (fee_per_tvl_24h / 30.0)))
        else:
            effective_trigger = trailing_trigger_pct
        if not trailing_active and pnl_pct >= effective_trigger:
            trailing_active = True
            print(f"🔥 Trailing TP activated for {pair}! Trigger {effective_trigger:.1f}% (fee/TVL {fee_per_tvl_24h:.1f}%) reached at {pnl_pct:.2f}% PnL")
            
        meta["peak_pnl"] = peak_pnl
        meta["trailing_active"] = trailing_active
        if not is_dry_run_stored:
            run_command(f"redis-cli set \"sol:dlmm:position:{pos_addr}\" '{json.dumps(meta)}'")

        close_reason = None
        drop_from_peak = 0.0
        position_closed_this_cycle = False
        emergency_close = False
        emergency_reason = None

        # 1. Trailing Take-Profit Exit Check — ratchet floor instead of flat drop
        if trailing_active:
            drop_from_peak = peak_pnl - pnl_pct
            floor_pct = trailing_floor_pct(peak_pnl, trailing_drop_pct)
            print(f"ℹ️ Trailing TP active: Peak PnL {peak_pnl:.2f}% | Current PnL {pnl_pct:.2f}% | Ratchet floor: {floor_pct:.2f}%")
            if pnl_pct <= floor_pct:
                # Gap-through grace: a "take-profit" that realizes a loss means price
                # wicked through the floor between monitor ticks (BABYANSEM peaked
                # +3.31% and TP-closed at -2.07%). Give one extra tick to recover;
                # close on the next tick if still below the floor. Slow bleeds close
                # one cycle later; single-tick wicks survive. Emergency SL unaffected.
                if pnl_pct < TRAILING_MIN_LOCK_PCT and not meta.get("trailing_grace_used", False):
                    meta["trailing_grace_used"] = True
                    if not is_dry_run_stored:
                        run_command(f"redis-cli set \"sol:dlmm:position:{pos_addr}\" '{json.dumps(meta)}'")
                    print(f"⏳ Trailing TP gap-through: PnL {pnl_pct:.2f}% below floor {floor_pct:.2f}% AND below +{TRAILING_MIN_LOCK_PCT}% lock — one-tick grace before close")
                else:
                    close_reason = f"Trailing Take-Profit hit (Peak: {peak_pnl:.2f}%, Current: {pnl_pct:.2f}% <= ratchet floor {floor_pct:.2f}%)"
            elif meta.get("trailing_grace_used", False):
                # Recovered above the floor — re-arm the grace for the next gap.
                meta["trailing_grace_used"] = False
                if not is_dry_run_stored:
                    run_command(f"redis-cli set \"sol:dlmm:position:{pos_addr}\" '{json.dumps(meta)}'")

        # 2. Hard Stop-Loss Check. Grace is conditional: only a young position that is
        # still in range AND earning hard (fee/TVL >= 10%) may ride a breach of the SL,
        # and never below the emergency floor. An unconditional grace window lets
        # dumping positions ride far past the SL before the next tick closes them.
        emergency_floor_pct = stop_loss_pct - EMERGENCY_SL_BUFFER_PCT
        age_minutes_sl = (now - meta.get("deployed_at", now)) / 60.0
        if pnl_pct <= emergency_floor_pct:
            close_reason = f"EMERGENCY Stop-Loss ({pnl_pct:.2f}% <= {emergency_floor_pct:.1f}% floor) — bypasses grace/holds"
            emergency_close = True
            emergency_reason = close_reason
        elif pnl_pct <= stop_loss_pct:
            if age_minutes_sl < 15 and in_range and fee_per_tvl_24h >= 10.0:
                print(f"⏳ SL deferred: {age_minutes_sl:.0f}m old, in range, fee/TVL {fee_per_tvl_24h:.1f}% — grace until 15m or {emergency_floor_pct:.1f}% floor")
            else:
                close_reason = f"Hard Stop-Loss hit ({pnl_pct:.2f}% <= {stop_loss_pct}%)"

        # 3. Pumped Far Above Range Check
        lower_bin = None
        upper_bin = None
        if bp:
            lower_bin = bp.get("lower_bin")
            upper_bin = bp.get("upper_bin")
        else:
            lower_bin = meta.get("entry_bin", 0) - bins_below
            upper_bin = meta.get("entry_bin", 0)
            
        if active_bin is not None and upper_bin is not None:
            if active_bin > upper_bin + max_bins_pumped_above:
                close_reason = f"Pumped far above range (Active bin {active_bin} > Upper bin {upper_bin} + {max_bins_pumped_above})"

        # 4. Out of Range (OOR) countdown check. Turnover runs a much shorter
        # fuse: its OOR close feeds the rebalance re-center, so every extra
        # minute waiting is idle fee-capture capital (thesis modes keep the
        # long fuse — their OOR close is a real exit decision).
        oor_limit_minutes = turnover_max_oor_minutes if meta.get("mode") == "turnover" else max_oor_minutes
        oor_key = f"sol:dlmm:position:{pos_addr}:oor_since"
        if not in_range:
            oor_val, _, _ = run_command(f"redis-cli get {oor_key}")
            if not oor_val or oor_val == "(nil)":
                run_command(f"redis-cli set {oor_key} {now}")
                print(f"🔴 Position {pair} is Out of Range. Starting {oor_limit_minutes}m countdown.")
            else:
                oor_start = int(oor_val)
                minutes_oor = (now - oor_start) / 60.0
                print(f"🔴 Position {pair} has been Out of Range for {minutes_oor:.1f} minutes.")
                if minutes_oor >= oor_limit_minutes:
                    close_reason = f"Out of Range for {minutes_oor:.1f}m (limit {oor_limit_minutes}m)"
        else:
            # Clear OOR timer
            run_command(f"redis-cli del {oor_key}")

        # 5. Low Yield Exit Check
        deployed_at = meta.get("deployed_at", now)
        age_minutes = (now - deployed_at) / 60.0
        if age_minutes >= min_age_before_yield_check:
            if fee_per_tvl_24h < min_fee_tvl_24h_limit:
                close_reason = f"Low yield (Fee/TVL 24h: {fee_per_tvl_24h:.2f}% < {min_fee_tvl_24h_limit}% after {age_minutes:.1f}m)"

        # 5b. Exit-side liquidity floor: entry depth gate is not enough — pool liquidity
        # can drain AFTER entry, stranding the position. Re-check live every cycle.
        # fail-open: None (fetch failed) never closes; only a confirmed sub-floor reading does.
        pool_liquidity_usd = get_pool_liquidity_usd(pool, meta.get("base_mint"))
        if pool_liquidity_usd is not None:
            print(f"Exit-liquidity check: pool {pair} liquidity = ${pool_liquidity_usd:,.0f} (floor ${min_exit_liquidity_usd:,.0f})")
            if pool_liquidity_usd < min_exit_liquidity_usd and not close_reason:
                close_reason = f"Thin pool liquidity (${pool_liquidity_usd:,.0f} < ${min_exit_liquidity_usd:,.0f} floor) — exit before it strands"
                emergency_close = True
                emergency_reason = close_reason

        # An emergency reason must not be diluted by a softer rule that fired after it
        # (pumped-above / OOR / low-yield all overwrite close_reason unconditionally).
        if emergency_close and emergency_reason:
            close_reason = emergency_reason

        # 6. Advanced Strategies Hooks (Fee Compounding & Partial Harvesting)
        strategy = meta.get("strategy", "spot")
        
        # Fee Compounding: claim fees and compound back into the position when
        # in range. Fires for the explicit fee_compounding strategy AND for any
        # turnover-mode position (fee capture is the whole thesis there, so
        # earned fees go straight back to work at a slightly higher bar to
        # clear the extra close/deploy tx cost).
        # NOTE: --report-only must never mutate on-chain state — skip all execution in report mode.
        is_turnover_compound = (meta.get("mode") == "turnover" and strategy != "single_sided_reseed")
        compound_min_sol = 0.02 if is_turnover_compound else 0.01
        if (strategy == "fee_compounding" or is_turnover_compound) and in_range and not close_reason and not cli.report_only:
            unclaimed_fees_sol = 0.0
            if api_available and bp:
                unclaimed_fees_sol = bp.get("unclaimed_fees_sol", 0.0)
            elif pnl_data:
                unclaimed_fees_sol = float(pnl_data.get("unclaimed_fees_sol", 0.0) or 0.0)
            if unclaimed_fees_sol >= compound_min_sol:
                print(f"💎 {'Turnover' if is_turnover_compound else 'Strategy: fee_compounding'} — compounding {unclaimed_fees_sol:.4f} SOL back into range...")
                claim_cmd = f"{env_prefix}node {EXECUTOR_PATH} claim {pos_addr}"
                claim_res, claim_err = run_command_json(claim_cmd)
                if claim_res and claim_res.get("success"):
                    # Close current position and redeploy total amount
                    close_cmd = f"{env_prefix}DLMM_CLOSE_AUTH=1 node {EXECUTOR_PATH} close {pos_addr}"
                    close_res, close_err = run_command_json(close_cmd)
                    if close_res and close_res.get("success"):
                        new_deploy_sol = meta.get("size_sol", DEFAULT_DEPLOY_SOL) + unclaimed_fees_sol
                        compound_shape = "bid_ask" if is_turnover_compound else "spot"
                        deploy_cmd = f"node {EXECUTOR_PATH} deploy {pool} 0 {new_deploy_sol} {bins_below} {meta.get('bins_above', 0)} {compound_shape} {params.get('SLIPPAGE_BPS', 1000)}"
                        print(f"Compounded redeploy: {deploy_cmd}")
                        dep_res, dep_err = run_command_json(deploy_cmd)
                        if dep_res and dep_res.get("success"):
                            new_pos = dep_res.get("position")
                            new_tx = dep_res.get("txHash")
                            run_command(f"redis-cli srem sol:dlmm:active_positions \"{pos_addr}\"")
                            run_command(f"redis-cli del \"sol:dlmm:position:{pos_addr}\"")
                            run_command(f"redis-cli del {oor_key}")
                            
                            ts = int(time.time())
                            tracking_data = {
                                "pool": pool,
                                "pair": pair,
                                "base_mint": meta.get("base_mint"),
                                "base_symbol": meta.get("base_symbol"),
                                "entry_price": active_price,
                                "entry_bin": active_bin,
                                "bins_below": bins_below,
                                "bins_above": meta.get("bins_above", 0),
                                "size_sol": new_deploy_sol,
                                "deployed_at": ts,
                                "tx_hash": new_tx,
                                # Turnover keeps its own strategy label so rebalance
                                # eligibility survives the compound; mode rides along
                                # for the per-mode OOR fuse and rebalance rules.
                                "strategy": strategy if is_turnover_compound else "fee_compounding",
                                "mode": meta.get("mode") or "multiday",
                                "amount_x": 0,
                                "amount_y": new_deploy_sol
                             }
                            if not is_dry_run_stored:
                                run_command(f"redis-cli set \"sol:dlmm:position:{new_pos}\" '{json.dumps(tracking_data)}'")
                                run_command(f"redis-cli sadd \"sol:dlmm:active_positions\" \"{new_pos}\"")
                                print(f"Compounded position tracked successfully: {new_pos}")
                            continue

        # Partial Harvesting: secure 50% profits if PnL reaches +10%
        has_harvested = meta.get("has_harvested", False)
        if strategy == "partial_harvest" and pnl_pct >= 10.0 and not has_harvested and in_range and not close_reason and not cli.report_only:
            print(f"💰 Strategy: partial_harvest. Securing 50% profits at +{pnl_pct:.2f}%...")
            close_cmd = f"{env_prefix}DLMM_CLOSE_AUTH=1 node {EXECUTOR_PATH} close {pos_addr}"
            close_res, close_err = run_command_json(close_cmd)
            if close_res and close_res.get("success"):
                new_deploy_sol = meta.get("size_sol", DEFAULT_DEPLOY_SOL) * 0.5
                deploy_cmd = f"node {EXECUTOR_PATH} deploy {pool} 0 {new_deploy_sol} {bins_below} {meta.get('bins_above', 0)} spot {params.get('SLIPPAGE_BPS', 1000)}"
                print(f"Partial redeploy: {deploy_cmd}")
                dep_res, dep_err = run_command_json(deploy_cmd)
                if dep_res and dep_res.get("success"):
                    new_pos = dep_res.get("position")
                    new_tx = dep_res.get("txHash")
                    run_command(f"redis-cli srem sol:dlmm:active_positions \"{pos_addr}\"")
                    run_command(f"redis-cli del \"sol:dlmm:position:{pos_addr}\"")
                    run_command(f"redis-cli del {oor_key}")
                    
                    ts = int(time.time())
                    tracking_data = {
                        "pool": pool,
                        "pair": pair,
                        "base_mint": meta.get("base_mint"),
                        "base_symbol": meta.get("base_symbol"),
                        "entry_price": active_price,
                        "entry_bin": active_bin,
                        "bins_below": bins_below,
                        "bins_above": meta.get("bins_above", 0),
                        "size_sol": new_deploy_sol,
                        "deployed_at": ts,
                        "tx_hash": new_tx,
                        "strategy": "partial_harvest",
                        "has_harvested": True,
                        "amount_x": 0,
                        "amount_y": new_deploy_sol
                    }
                    if not is_dry_run_stored:
                        run_command(f"redis-cli set \"sol:dlmm:position:{new_pos}\" '{json.dumps(tracking_data)}'")
                        run_command(f"redis-cli sadd \"sol:dlmm:active_positions\" \"{new_pos}\"")
                        print(f"Partial harvested position tracked successfully: {new_pos}")
                    continue

        # 7. Pre-exit Indicators Timing Check (for non-emergency exits)
        if close_reason and params.get("INDICATORS_ENABLED"):
            is_emergency = emergency_close or "stop-loss" in close_reason.lower() or "out of range" in close_reason.lower() or "pumped" in close_reason.lower()
            if not is_emergency:
                MAX_INDICATOR_BLOCK_MINUTES = 60.0
                ind_block_key = f"sol:dlmm:position:{pos_addr}:indicator_blocked_since"
                ind_block_val, _, _ = run_command(f"redis-cli get \"{ind_block_key}\"")
                ind_blocked_minutes = (now - int(ind_block_val)) / 60.0 if ind_block_val and ind_block_val != "(nil)" else 0.0
                if ind_blocked_minutes >= MAX_INDICATOR_BLOCK_MINUTES:
                    print(f"⚠️ Indicator exit check timed out for {pair} after {ind_blocked_minutes:.0f}m — forcing close.")
                    run_command(f"redis-cli del \"{ind_block_key}\"")
                else:
                    confirmed = check_local_indicators(pool, meta.get("base_mint"), "exit", params.get("INDICATORS_PRESET", "supertrend_or_rsi"), "24h")
                    if confirmed is False:
                        print(f"Indicators exit timing check rejected exit signal for {pair}. Postponing close.")
                        if not ind_block_val or ind_block_val == "(nil)":
                            run_command(f"redis-cli set \"{ind_block_key}\" {now} EX 86400")
                        close_reason = None
                    elif confirmed is None:
                        print(f"Indicators exit timing: data unavailable for {pair} — proceeding with exit (fail-open).")
                    else:
                        run_command(f"redis-cli del \"{ind_block_key}\"")

        # --report-only: collect state, skip execution — EXCEPT emergency closes.
        # The wakegate cron runs report-only and the agent decision hop adds minutes;
        # a breach of the emergency floor cannot wait for it (SOUL GUARD: "pnl < -15%
        # always closes"). --no-enforce restores pure reporting.
        enforce_emergency = emergency_close and close_reason and not cli.no_enforce
        if cli.report_only and not enforce_emergency:
            report_rows.append(build_report_row(
                pos_addr, pair, pool, meta, pool_liquidity_usd, pnl_pct, pnl_sol_actual,
                in_range, fee_per_tvl_24h, api_available, bp, active_price, entry_price,
                peak_pnl, trailing_active, close_reason, now,
            ))
            continue
        if cli.report_only and enforce_emergency:
            print(f"⚡ [report-only] Emergency close enforced for {pair}: {close_reason}")

        # AI hold check — suppress rule-based close if AI flagged hold
        # Bypass if trailing TP already dropped >= 3% from peak: that's a real dump, not a bounce dip
        # Emergency closes (SL floor / thin liquidity) are never suppressed.
        AI_HOLD_BYPASS_DROP_PCT = 3.0
        if close_reason and not emergency_close:
            hold_val, _, _ = run_command(f"redis-cli get \"sol:dlmm:position:{pos_addr}:ai_hold_until\"")
            if hold_val and hold_val != "(nil)" and int(hold_val) > now:
                is_large_trailing_drop = "trailing take-profit" in close_reason.lower() and drop_from_peak >= AI_HOLD_BYPASS_DROP_PCT
                if is_large_trailing_drop:
                    print(f"⚡ AI HOLD bypassed for {pair}: trailing TP drop {drop_from_peak:.2f}% >= {AI_HOLD_BYPASS_DROP_PCT}% (real dump, not bounce)")
                else:
                    mins_left = (int(hold_val) - now) / 60.0
                    print(f"✋ AI HOLD active for {pair} ({mins_left:.0f}m left) — suppressing: {close_reason}")
                    close_reason = None

        # Execute close if triggered
        if close_reason:
            print(f"🚨 Exiting Position {pair} - Reason: {close_reason}")
            env_prefix = "DRY_RUN=true " if is_dry_run_stored else ""
            close_cmd = f"{env_prefix}DLMM_CLOSE_AUTH=1 node {EXECUTOR_PATH} close {pos_addr}"
            close_res, close_err = run_command_json(close_cmd)
            
            if close_res and close_res.get("success"):
                position_closed_this_cycle = True
                txs = close_res.get("txHashes", ["DRY_RUN_TX_HASH"])
                print(f"✅ Successfully closed position {pair}. Txs: {', '.join(txs)}")
                
                # Update Redis
                run_command(f"redis-cli srem sol:dlmm:active_positions \"{pos_addr}\"")
                run_command(f"redis-cli del \"sol:dlmm:position:{pos_addr}\"")
                run_command(f"redis-cli del {oor_key}")
                
                # Write Daily PnL statistics
                today = time.strftime("%Y-%m-%d")
                pnl_key = f"sol:dlmm:pnl:daily:{today}"
                current_pnl_str, _, _ = run_command(f"redis-cli hget {pnl_key} total_sol")
                current_pnl = float(current_pnl_str) if current_pnl_str and current_pnl_str != "(nil)" else 0.0
                
                size_sol = meta.get("size_sol", DEFAULT_DEPLOY_SOL)
                # Use Meteora's exact pnlSol when available; fall back to pct×size estimate
                realized_sol = pnl_sol_actual if pnl_sol_actual is not None else size_sol * (pnl_pct / 100.0)
                new_pnl = current_pnl + realized_sol
                
                run_command(f"redis-cli hset {pnl_key} total_sol {new_pnl}")
                run_command(f"redis-cli hincrby {pnl_key} count_exits 1")
                if realized_sol < 0:
                    run_command(f"redis-cli hincrby {pnl_key} count_losses 1")

                # Journal every close with API-verified PnL (dlmm_reconcile.py audits
                # this file against the Meteora portfolio API).
                log_close(pool, pair, meta, pos_addr, pnl_pct, realized_sol,
                          fee_per_tvl_24h, age_minutes, close_reason, txs,
                          close_res.get("dryRun") or close_res.get("dry_run") == True)

                # Re-entry cooldown blacklist — prevent re-opening same token too soon
                base_symbol_cd = meta.get("base_symbol", pair.split("-")[0]).upper()
                reason_lower = close_reason.lower()
                is_dump_close = any(kw in reason_lower for kw in ("trailing", "dump", "stop-loss", "stop_loss", "sell pressure", "momentum"))
                cooldown_key = f"sol:dlmm:cooldown:{base_symbol_cd}"
                loss_streak_key = f"sol:dlmm:loss_streak:{base_symbol_cd}"

                # OOR rebalance (Meridian-style re-center): a position drifting
                # out of range can be a re-center opportunity instead of an exit,
                # but only when the drift doesn't contradict the mode's entry
                # thesis. Turnover pools are picked for fee flow, so re-center in
                # either direction; multiday re-centers only while profitable
                # (drift, not dump); casual re-centers only on an UPWARD break —
                # a downward OOR on a 30m-window pool is usually the dump, and
                # re-centering there re-buys a falling token. Common guards:
                # plain OOR closes only (never stop-loss / thin-liquidity /
                # dump), shallow drawdown, and max 3 re-centers per pool per 24h
                # so a genuinely trending pool still exits instead of being
                # chased forever.
                rebalance_count_key = f"sol:dlmm:rebalance_count:{pool}"
                _rc_str, _, _ = run_command(f"redis-cli get \"{rebalance_count_key}\"")
                rebalances_24h = int(_rc_str) if _rc_str and _rc_str.strip().isdigit() else 0
                mode_cd = meta.get("mode", "multiday")
                # Direction needs live bin data; unknown bins fail closed (no rebalance).
                oor_above = (active_bin is not None and upper_bin is not None and active_bin > upper_bin)
                mode_allows_rebalance = (
                    mode_cd == "turnover"
                    or (mode_cd == "multiday" and pnl_pct > 0)
                    or (mode_cd == "casual" and oor_above)
                )
                # Rebalance budget. Thesis modes keep the hard 3/24h count cap.
                # Turnover churns by design (fast OOR fuse feeds re-centers), so
                # its primary guard is a net-PnL circuit breaker: cumulative
                # realized PnL across this pool's rebalance closes (24h window)
                # must stay above the floor. The 20/24h count is only a backstop.
                rebalance_pnl_key = f"sol:dlmm:rebalance_pnl:{pool}"
                _rp_str, _, _ = run_command(f"redis-cli get \"{rebalance_pnl_key}\"")
                try:
                    rebalance_pnl_24h = float(_rp_str) if _rp_str and _rp_str.strip() and _rp_str.strip() != "(nil)" else 0.0
                except ValueError:
                    rebalance_pnl_24h = 0.0
                cb_floor_sol = params.get("TURNOVER_CB_LOSS_SOL", TURNOVER_CB_LOSS_SOL)
                if mode_cd == "turnover":
                    rebalance_cap = 20
                    rebalance_budget_ok = rebalances_24h < rebalance_cap and rebalance_pnl_24h > cb_floor_sol
                else:
                    rebalance_cap = 3
                    rebalance_budget_ok = rebalances_24h < rebalance_cap
                is_oor_rebalance = (
                    mode_allows_rebalance
                    and strategy != "single_sided_reseed"  # reseed path redeploys on its own — never both
                    and close_reason.startswith("Out of Range")
                    and not emergency_close
                    and not is_dump_close
                    and pnl_pct > -8.0
                    and rebalance_budget_ok
                )

                if is_oor_rebalance:
                    print(f"♻️ {mode_cd} rebalance eligible ({rebalances_24h}/{rebalance_cap} in 24h, pool rebalance PnL {rebalance_pnl_24h:+.4f} SOL) — skipping re-entry cooldown for {base_symbol_cd}")
                elif (mode_cd == "turnover" and close_reason.startswith("Out of Range")
                        and rebalance_pnl_24h <= cb_floor_sol):
                    print(f"⛔ Turnover rebalance circuit breaker tripped: pool 24h rebalance PnL {rebalance_pnl_24h:+.4f} SOL <= {cb_floor_sol} SOL floor — normal exit + cooldown")
                else:
                    cooldown_secs = 7200 if is_dump_close else 3600  # 2h dump, 1h other
                    # Clean profitable casual exit: the pool is often still hot after a
                    # 30m-window harvest — a full 1h block just forfeits re-entry. Dump
                    # closes and losses keep the longer cooldowns (and the loss-streak
                    # escalation below only fires on realized_sol < 0 anyway).
                    if not is_dump_close and realized_sol > 0 and meta.get("mode", "multiday") == "casual":
                        cooldown_secs = 1800
                    if realized_sol < 0:
                        # Track repeat losses within a 7-day window and escalate cooldown duration
                        run_command(f"redis-cli incr \"{loss_streak_key}\"")
                        run_command(f"redis-cli expire \"{loss_streak_key}\" 604800")
                        streak_str, _, _ = run_command(f"redis-cli get \"{loss_streak_key}\"")
                        loss_streak = int(streak_str) if streak_str and streak_str.strip().lstrip('-').isdigit() else 1
                        if loss_streak >= 2:
                            cooldown_secs = 259200 if loss_streak >= 3 else 86400  # 3+ losses→72h, 2 losses→24h
                            print(f"🔴 REPEAT LOSS #{loss_streak} in 7d — escalating {base_symbol_cd} cooldown to {cooldown_secs // 3600}h")
                    else:
                        run_command(f"redis-cli del \"{loss_streak_key}\"")  # reset streak on profit
                    run_command(f"redis-cli set \"{cooldown_key}\" \"{close_reason[:120]}\" ex {cooldown_secs}")
                    print(f"🚫 Re-entry cooldown set for {base_symbol_cd}: {cooldown_secs // 3600}h (reason: {'dump/momentum' if is_dump_close else 'normal exit'})")

                # Low-yield exits also cool the POOL itself for 4h (ported from
                # Meridian's low-yield pool cooldown): the symbol cooldown above
                # expires in 1h, but a pool whose fee flow already decayed won't
                # recover that fast — without this we re-enter the same fee-dead
                # pool on the next signal and churn.
                if close_reason.startswith("Low yield"):
                    run_command(f"redis-cli set \"sol:dlmm:cooldown:pool:{pool}\" \"{close_reason[:120]}\" ex 14400")
                    print(f"🚫 Pool cooldown set 4h (low yield): {pool}")

                # Auto-swap base token back to SOL (unless skip_swap is active)
                base_mint = meta.get("base_mint")
                skip_swap = (strategy == "single_sided_reseed")
                swap_report = ""
                
                if base_mint and base_mint != SOL_MINT and not skip_swap:
                    print(f"Checking {pair} base token balance for auto-swap...")
                    time.sleep(5)  # Wait for transactions to confirm on-chain
                    bal_data, bal_err = run_command_json(f"{env_prefix}node {EXECUTOR_PATH} spl-balance {base_mint}")
                    if bal_data and bal_data.get("balance", 0) > 0:
                        balance = float(bal_data.get("balance", 0))
                        est_sol = balance * active_price
                        print(f"Base token balance: {balance} (~{est_sol:.4f} SOL)")
                        if est_sol > 0.01 or (close_res.get("dryRun") or close_res.get("dry_run") == True):
                            # Dump exits MUST liquidate even at high impact — holding a crashing token is worse than slippage.
                            # Normal/profit exits use a tight 5% guard to avoid bad fills on thin pools (token re-swept by --cleanup-tokens).
                            swap_max_impact = 50 if is_dump_close else 15
                            swap_slip_bps = 300 if is_dump_close else 300
                            print(f"Executing auto-swap back to SOL for {balance} tokens (max_impact {swap_max_impact}%, {'dump' if is_dump_close else 'normal'} exit)...")
                            swap_res, swap_err = run_command_json(f"{env_prefix}node {EXECUTOR_PATH} swap {base_mint} SOL {balance} {swap_max_impact} {swap_slip_bps}")
                            if swap_res and swap_res.get("success"):
                                swap_tx = swap_res.get("txHash", "DRY_RUN_SWAP_TX_HASH")
                                swap_report = f"\n**Auto-Swap**: Swapped {balance:.4f} base tokens back to SOL.\n**Swap TX**: https://solscan.io/tx/{swap_tx}"
                                print(f"✅ Auto-swapped back to SOL successfully. Tx: {swap_tx}")
                            else:
                                swap_report = f"\n**Auto-Swap**: Failed to swap back to SOL: {swap_err or swap_res.get('error')}"
                                print(f"❌ Auto-swap failed: {swap_err or swap_res.get('error')}")
                        else:
                            print("Base token balance SOL value too small (<0.01 SOL), skipping swap")
                    else:
                        print("Base token balance is zero, skipping swap")
                        
                elif skip_swap:
                    # In reseed strategy, redeploy token balance at new lower price bins
                    print(f"Strategy: single_sided_reseed. Bypassing auto-swap back to SOL and re-seeding position...")
                    time.sleep(5)
                    bal_data, bal_err = run_command_json(f"{env_prefix}node {EXECUTOR_PATH} spl-balance {base_mint}")
                    if bal_data and bal_data.get("balance", 0) > 0:
                        amount_x = float(bal_data.get("balance", 0))
                        ab_data, ab_err = run_command_json(f"node {EXECUTOR_PATH} active-bin {pool}")
                        if ab_data:
                            active_price = float(ab_data.get("price", entry_price))
                            active_bin = ab_data.get("binId")
                            deploy_cmd = f"node {EXECUTOR_PATH} deploy {pool} {amount_x} 0 40 0 bid_ask {params.get('SLIPPAGE_BPS', 1000)}"
                            print(f"Running re-seed LP deploy: {deploy_cmd}")
                            dep_res, dep_err = run_command_json(deploy_cmd)
                            if dep_res and dep_res.get("success"):
                                new_pos = dep_res.get("position")
                                new_tx = dep_res.get("txHash")
                                swap_report = f"\n**Re-seeded**: Redeployed {amount_x:.4f} base tokens into new LP position {new_pos} at active bin {active_bin}."
                                tracking_data = {
                                    "pool": pool,
                                    "pair": pair,
                                    "base_mint": base_mint,
                                    "base_symbol": meta.get("base_symbol"),
                                    "entry_price": active_price,
                                    "entry_bin": active_bin,
                                    "bins_below": 40,
                                    "bins_above": 0,
                                    "size_sol": size_sol,
                                    "deployed_at": now,
                                    "tx_hash": new_tx,
                                    "strategy": "single_sided_reseed",
                                    "amount_x": amount_x,
                                    "amount_y": 0
                                }
                                if not is_dry_run_stored:
                                    run_command(f"redis-cli set \"sol:dlmm:position:{new_pos}\" '{json.dumps(tracking_data)}'")
                                    run_command(f"redis-cli sadd \"sol:dlmm:active_positions\" \"{new_pos}\"")
                                    print(f"Re-seeded position tracked: {new_pos}")
                    else:
                        print("No base token balance available to re-seed.")

                # Execute the re-center: capital is back in SOL (the auto-swap
                # above already liquidated the base side), so redeploy the same
                # pool single-sided below the current active bin, edge-weighted.
                # Width follows the mode's holding intent: turnover stays tight
                # for fee density, casual medium for the retrace window, multiday
                # wide to survive days of drift. Deploy failure falls back to a
                # normal 1h cooldown so the pool isn't silently forgotten.
                if is_oor_rebalance:
                    rebalance_bins = {"turnover": 20, "casual": 25, "multiday": 40}.get(mode_cd, 40)
                    run_command(f"redis-cli incr \"{rebalance_count_key}\"")
                    run_command(f"redis-cli expire \"{rebalance_count_key}\" 86400")
                    # Feed the circuit breaker: this close's realized PnL joins the
                    # pool's 24h rebalance tally. TTL refreshes on every write, so
                    # the window is "24h since the last re-center" — sticky on
                    # purpose: a pool that keeps churning keeps its loss history.
                    run_command(f"redis-cli incrbyfloat \"{rebalance_pnl_key}\" {realized_sol:.6f}")
                    run_command(f"redis-cli expire \"{rebalance_pnl_key}\" 86400")
                    rebalance_cmd = f"{env_prefix}node {EXECUTOR_PATH} deploy {pool} 0 {size_sol} {rebalance_bins} 0 bid_ask {params.get('SLIPPAGE_BPS', 1000)}"
                    print(f"♻️ {mode_cd} rebalance redeploy: {rebalance_cmd}")
                    dep_res, dep_err = run_command_json(rebalance_cmd)
                    if dep_res and dep_res.get("success"):
                        new_pos = dep_res.get("position")
                        ab_data, _ = run_command_json(f"node {EXECUTOR_PATH} active-bin {pool}")
                        rb_price = float(ab_data.get("price", active_price)) if ab_data else active_price
                        rb_bin = ab_data.get("binId") if ab_data else None
                        tracking_data = {
                            "pool": pool,
                            "pair": pair,
                            "base_mint": base_mint,
                            "base_symbol": meta.get("base_symbol"),
                            "entry_price": rb_price,
                            "entry_bin": rb_bin,
                            "bins_below": rebalance_bins,
                            "bins_above": 0,
                            "size_sol": size_sol,
                            "deployed_at": now,
                            "tx_hash": dep_res.get("txHash"),
                            "strategy": f"{mode_cd}_rebalance",
                            "mode": mode_cd,
                            "amount_x": 0,
                            "amount_y": size_sol
                        }
                        if not is_dry_run_stored:
                            run_command(f"redis-cli set \"sol:dlmm:position:{new_pos}\" '{json.dumps(tracking_data)}'")
                            run_command(f"redis-cli sadd \"sol:dlmm:active_positions\" \"{new_pos}\"")
                        swap_report += f"\n**Rebalanced**: re-centered {size_sol} SOL at the current price (re-center #{rebalances_24h + 1}/{rebalance_cap} in 24h). New position: {new_pos}"
                        print(f"♻️ {mode_cd} rebalance deployed: {new_pos}")
                    else:
                        print(f"❌ {mode_cd} rebalance deploy failed: {dep_err or (dep_res or {}).get('error')} — position stays closed, normal cooldown applies")
                        run_command(f"redis-cli set \"{cooldown_key}\" \"{close_reason[:120]}\" ex 3600")

                # Telegram report formatting
                wib_str = time.strftime("%H:%M WIB", time.gmtime(int(time.time()) + 7 * 3600))
                is_dry = close_res.get("dryRun") or close_res.get("dry_run") == True
                status_label = "🧪 DRY RUN CLOSE" if is_dry else "🚨 POSITION CLOSED"
                report = f"""{status_label} — {wib_str}
{pair} {pos_addr}
Exit Reason | {close_reason}
Metric | Value
Entry Price | {entry_price:.8f}
Exit Price | {active_price:.8f}
Realized PnL | {pnl_pct:+.2f}% ({realized_sol:+.4f} SOL){swap_report}
TX | https://solscan.io/tx/{txs[0]}
"""
                print(report)
            else:
                print(f"❌ Failed to close position {pair}: {close_err or close_res.get('error')}")

        # Live run, position still active (HOLD, or a close attempt that failed):
        # feed it through the same row builder as --report-only so the Telegram
        # status card doesn't drift into an ad-hoc format on live ticks.
        if not cli.report_only and not position_closed_this_cycle:
            report_rows.append(build_report_row(
                pos_addr, pair, pool, meta, pool_liquidity_usd, pnl_pct, pnl_sol_actual,
                in_range, fee_per_tvl_24h, api_available, bp, active_price, entry_price,
                peak_pnl, trailing_active, close_reason, now,
            ))

    if cli.report_only or report_rows:
        sol_price_usd = meteora_sol_price
        print(render_status_report(report_rows, sol_price_usd, trailing_trigger_pct, min_fee_tvl_24h_limit, max_oor_minutes))
        print("MONITOR_REPORT:" + json.dumps({"positions": report_rows}))

    # Darwinian signal-weights refresh. Self-guarded inside the script (recalcs
    # at most every 6h and only with enough closed positions) — cheap no-op on
    # most runs, and never allowed to fail the monitor.
    try:
        subprocess.run(
            [sys.executable, os.path.join(SCRIPT_DIR, "dlmm_weights.py"), "--quiet"],
            timeout=60,
        )
    except Exception:
        pass

if __name__ == "__main__":
    main()
    