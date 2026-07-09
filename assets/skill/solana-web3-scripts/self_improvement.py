#!/usr/bin/env python3
import asyncio
import json
import os
import subprocess
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from hindsight_client import Hindsight

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

WIB = ZoneInfo("Asia/Jakarta")

# Resolved from this file's own location (<profile>/skills/solana-web3/scripts/) so the
# script works whether it's a copy or a symlink into a Hermes profile.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROFILE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(SCRIPT_DIR)))
PROFILE_NAME = os.path.basename(PROFILE_DIR)
SOUL_PATH = os.path.join(PROFILE_DIR, "SOUL.md")


def run_command(cmd, timeout=5):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception as e:
        return f"Error running command: {e}"


def fetch_wallet_stats(address, time_type="7D"):
    url = f"https://web3.binance.com/bapi/defi/v3/public/wallet-direct/buw/wallet/address/detail/stats?address={address}&chainId=CT_501&timeType={time_type}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json"
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            if data.get("success") and "data" in data:
                return data["data"]
            return {"error": data.get("message", "API returned success=false")}
    except Exception as e:
        return {"error": str(e)}



def get_current_section(soul_content, section_num):
    """Extract a section from SOUL.md starting with '## section_num.' until the next '##' or end of file."""
    lines = soul_content.splitlines()
    section_lines = []
    in_section = False
    
    for line in lines:
        if line.strip().startswith(f"## {section_num}."):
            in_section = True
            section_lines.append(line)
            continue
        elif in_section and line.strip().startswith("## "):
            break
        
        if in_section:
            section_lines.append(line)
            
    return "\n".join(section_lines)


async def fetch_hindsight_memories():
    h = Hindsight(base_url="http://127.0.0.1:8888")
    memories = []
    
    # Query Hindsight memory for trade entry and exit observations
    try:
        # We query with 'SOL' and filter by 'sol-trade' tags
        resp = await h.arecall(
            bank_id="main-memory",
            query="SOL",
            tags=["sol-trade"],
            tags_match="any"
        )
        if resp and resp.results:
            memories = [r.text for r in resp.results]
    except Exception as e:
        memories = [f"Failed to fetch memories from Hindsight: {e}"]
        
    return memories


def main():
    print("# DLMM STRATEGY PERFORMANCE GATHERER\n")
    print(f"Current Time (WIB): {datetime.now(WIB).strftime('%Y-%m-%d %H:%M:%S')}\n")
    
    # 1. Gather Binance Web3 Wallet On-Chain Stats
    print("## 1. Binance Web3 Wallet On-Chain Stats (7D)")
    wallet_address = os.environ["SOLANA_PUBLIC_KEY"]
    print(f"Wallet Address: `{wallet_address}`")
    stats = fetch_wallet_stats(wallet_address, "7D")
    
    if stats and "error" not in stats:
        try:
            pnl_usd = float(stats.get("realizedPnl", 0.0))
            pnl_pct = float(stats.get("realizedPnlPercent", 0.0)) * 100
            vol_usd = float(stats.get("totalVolumeUsd", 0.0))
            gas_usd = float(stats.get("totalGasUsd", 0.0))
            pnl_pct_after_gas = float(stats.get("realizedPnlPercentAfterGas", 0.0)) * 100
            
            print(f"- **Total Volume**: ${vol_usd:,.2f}")
            print(f"- **Total Gas/Fees**: ${gas_usd:,.4f}")
            print(f"- **Trades**: {stats.get('buyCnt', 0)} Buys / {stats.get('sellCnt', 0)} Sells (Total: {int(stats.get('buyCnt', 0)) + int(stats.get('sellCnt', 0))})")
            print(f"- **Unique Tokens Traded**: {stats.get('tradeTokenCnt', 0)}")
            print(f"- **Realized PnL**: ${pnl_usd:+.4f} ({pnl_pct:+.2f}%)")
            print(f"- **Realized PnL After Gas**: {pnl_pct_after_gas:+.2f}%")
            print(f"- **Wallet Age**: {stats.get('ageDays', 0)} days")
        except Exception as e:
            print(f"Error parsing stats response: {e}")
            print(f"Raw data: {stats}")
    else:
        err_msg = stats.get("error", "Unknown API error") if stats else "No response"
        print(f"Warning: Could not fetch Binance Web3 wallet stats: {err_msg}")
    print()

    # 1b. Gather Redis realization PnL statistics for last 7 days
    print("## 1b. Past 7 Days Realized PnL (Redis)")
    today = datetime.now(WIB)
    pnl_records = []

    
    for i in range(7):
        date_str = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        key = f"sol:pnl:daily:{date_str}"
        val = run_command(f"redis-cli get '{key}'")
        
        if val and val != "(nil)" and not val.startswith("Error"):
            try:
                data = json.loads(val)
                pnl_records.append({
                    "date": date_str,
                    "total_usd": data.get("total_usd", 0.0),
                    "trade_count": data.get("trade_count", 0),
                    "wins": data.get("wins", 0),
                    "losses": data.get("losses", 0),
                    "fees_sol": data.get("total_fees_sol", 0.0)
                })
            except Exception:
                pnl_records.append({
                    "date": date_str,
                    "error": f"Failed to parse JSON: {val}"
                })
                
    if pnl_records:
        print("| Date | Realized PnL (USD) | Trades | Wins | Losses | Win Rate | Fees (SOL) |")
        print("| --- | --- | --- | --- | --- | --- | --- |")
        for r in pnl_records:
            if "error" in r:
                print(f"| {r['date']} | ERROR | | | | | |")
                continue
            win_rate = (r["wins"] / r["trade_count"] * 100) if r["trade_count"] > 0 else 0
            print(f"| {r['date']} | ${r['total_usd']:+.4f} | {r['trade_count']} | {r['wins']} | {r['losses']} | {win_rate:.1f}% | {r['fees_sol']:.6f} |")
    else:
        print("No realized PnL records found in Redis for the last 7 days.")
    print()
    
    # 1c. Gather Daily Start Balance
    print("## 1c. Daily Start Balance")
    start_balance_path = f"/tmp/{PROFILE_NAME}_daily_start_balance"
    if os.path.exists(start_balance_path):
        try:
            with open(start_balance_path, "r", encoding="utf-8") as f:
                content = f.read().strip()
            print(f"- **Path**: `{start_balance_path}`")
            print(f"- **Content**: `{content}`")
        except Exception as e:
            print(f"Error reading {start_balance_path}: {e}")
    else:
        print(f"Daily start balance file not found at `{start_balance_path}`")
    print()
    
    # 1d. Past 7 Days Realized DLMM PnL (Meteora Calendar API — ground truth)
    print("## 1d. Past 7 Days Realized DLMM PnL (Meteora)")
    try:
        wallet = None
        with open(os.path.join(PROFILE_DIR, ".env")) as f:
            for line in f:
                if line.startswith("SOLANA_PUBLIC_KEY="):
                    wallet = line.split("=", 1)[1].strip().strip('"\'')
                    break
        dlmm_pnl_records = []
        if wallet:
            for month_delta in (0, -1):
                month_date = today.replace(day=1) + timedelta(days=32 * month_delta)
                month_str = month_date.strftime("%Y-%m")
                url = f"https://portfolio.datapi.meteora.ag/chart/calendar/{wallet}?month={month_str}"
                try:
                    req = urllib.request.Request(url, headers={"User-Agent": "dlmm-lp/1.0"})
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        cal = json.loads(resp.read())
                    for dp in cal.get("data_points", []):
                        date_str = dp.get("date_time", "")[:10]
                        if not date_str:
                            continue
                        dp_date = datetime.strptime(date_str, "%Y-%m-%d").date()
                        if dp_date < today.date() - timedelta(days=6) or dp_date > today.date():
                            continue
                        if int(dp.get("closed_position_count", 0)) == 0:
                            continue
                        dlmm_pnl_records.append({
                            "date": date_str,
                            "pnl_sol": float(dp.get("pnl_sol", 0)),
                            "pnl_usd": float(dp.get("pnl_usd", 0)),
                            "count_exits": int(dp.get("closed_position_count", 0)),
                            "win_rate_sol": float(dp.get("win_rate_sol", 0)),
                        })
                except Exception as e:
                    print(f"Warning: Meteora calendar fetch failed for {month_str}: {e}")
        dlmm_pnl_records.sort(key=lambda x: x["date"])
        if dlmm_pnl_records:
            print("| Date | PnL (SOL) | PnL (USD) | Exits | Win Rate (SOL) |")
            print("| --- | --- | --- | --- | --- |")
            for r in dlmm_pnl_records:
                print(f"| {r['date']} | {r['pnl_sol']:+.4f} SOL | ${r['pnl_usd']:+.2f} | {r['count_exits']} | {r['win_rate_sol']:.0f}% |")
        else:
            print("No realized DLMM PnL records in last 7 days.")
    except Exception as e:
        print(f"Error fetching Meteora calendar PnL: {e}")
    print()
    
    
    # 2. Gather active positions from Redis
    print("## 2. Active Positions (Redis)")
    active_keys = run_command("redis-cli keys 'sol:position:*'")
    positions = []
    
    if active_keys and not active_keys.startswith("Error"):
        keys_list = [k.strip() for k in active_keys.splitlines() if k.strip()]
        for key in keys_list:
            val = run_command(f"redis-cli get '{key}'")
            if val and val != "(nil)" and not val.startswith("Error"):
                try:
                    data = json.loads(val)
                    positions.append(data)
                except Exception:
                    pass
                    
    if positions:
        print("| Ticker | Entry Price | Peak Price | Size (USD) | Opened At |")
        print("| --- | --- | --- | --- | --- |")
        for p in positions:
            ticker = p.get("symbol", "UNKNOWN")
            entry = p.get("entry_price", 0.0)
            peak = p.get("peak_price", 0.0)
            size = p.get("size_usd", p.get("position_size", 0.0))
            opened = p.get("opened_at", "")
            print(f"| {ticker} | {entry} | {peak} | ${size:.2f} | {opened} |")
    else:
        print("No active positions currently tracked.")
    print()
    
    # 2b. Active DLMM Positions (Redis)
    print("## 2b. Active DLMM Positions (Redis)")
    active_dlmm_keys = run_command("redis-cli smembers 'sol:dlmm:active_positions'")
    dlmm_positions = []
    if active_dlmm_keys and not active_dlmm_keys.startswith("Error"):
        keys_list = [k.strip() for k in active_dlmm_keys.splitlines() if k.strip()]
        for key in keys_list:
            val = run_command(f"redis-cli get 'sol:dlmm:position:{key}'")
            if val and val != "(nil)" and not val.startswith("Error"):
                try:
                    data = json.loads(val)
                    data["position_addr"] = key
                    dlmm_positions.append(data)
                except Exception:
                    pass
    if dlmm_positions:
        print("| Pair | Mode | Deployed SOL | Entry Price | Entry Bin | Peak PnL | Deployed At | Position Address |")
        print("| --- | --- | --- | --- | --- | --- | --- | --- |")
        for p in dlmm_positions:
            pair = p.get("pair", "UNKNOWN")
            mode = p.get("mode", "multiday")
            size = p.get("size_sol", 0.0)
            entry = p.get("entry_price", 0.0)
            entry_bin = p.get("entry_bin", 0)
            peak_pnl = p.get("peak_pnl", 0.0)

            try:
                deployed = datetime.fromtimestamp(p.get("deployed_at", 0)).strftime("%Y-%m-%d %H:%M:%S")
            except:
                deployed = str(p.get("deployed_at", 0))

            addr = p.get("position_addr", "")
            print(f"| {pair} | {mode} | {size:.2f} SOL | {entry:.8f} | {entry_bin} | {peak_pnl:+.2f}% | {deployed} | {addr} |")
    else:
        print("No active DLMM positions currently tracked.")
    print()
    
    # 3. Gather hindsight memories
    print("## 3. Trade Entry/Exit Records (Hindsight & Redis)")
    
    # 3a. Read from Hindsight API
    h_memories = []
    try:
        h_memories = asyncio.run(fetch_hindsight_memories())
    except Exception as e:
        print(f"Error reading Hindsight: {e}")
        
    # 3b. Read from Redis keys matching 'sol:hindsight:*'
    redis_memories = []
    redis_hindsight_keys = run_command("redis-cli keys 'sol:hindsight:*'")
    if redis_hindsight_keys and not redis_hindsight_keys.startswith("Error"):
        keys_list = [k.strip() for k in redis_hindsight_keys.splitlines() if k.strip()]
        for key in keys_list:
            val = run_command(f"redis-cli get '{key}'")
            if val and val != "(nil)" and not val.startswith("Error"):
                # Clean enclosing quotes if redis returned a raw string
                if val.startswith('"') and val.endswith('"'):
                    val = val[1:-1]
                redis_memories.append(val)
                
    # Merge and print
    all_memories = []
    for m in redis_memories:
        all_memories.append(f"- [Redis] {m}")
    for m in h_memories:
        # Avoid duplicating if same text is in both
        if not any(m in rm for rm in redis_memories):
            all_memories.append(f"- [Hindsight] {m}")
            
    if all_memories:
        for entry in all_memories[:30]:  # limit to top 30
            print(entry)
    else:
        print("No trade entry/exit records found in Hindsight or Redis.")
    print()
    
    # 4. Fetch current sections from SOUL.md
    print("## 4. Current Risk & Ingestion Parameters (SOUL.md)")
    if os.path.exists(SOUL_PATH):
        try:
            with open(SOUL_PATH, "r", encoding="utf-8") as f:
                soul_content = f.read()
                
            print("### CURRENT Section 3 (Hard Exit Rules)")
            sec3 = get_current_section(soul_content, "3")
            print(sec3)
            print()
            
            print("### CURRENT Section 8 (Webhook Ingestion Reject Gates)")
            sec8 = get_current_section(soul_content, "8")
            print(sec8)
            print()
            
            print("### CURRENT Section 9 (Meteora DLMM LP Ingestion & Management Parameters)")
            sec9 = get_current_section(soul_content, "9")
            print(sec9)
            print()
        except Exception as e:
            print(f"Error reading SOUL.md: {e}")
    else:
        print(f"SOUL.md not found at {SOUL_PATH}")
    print()

    # 5. Weekly Strategy Optimization Rules
    print("## 5. Weekly Strategy Optimization Rules (from performance-review skill)")
    print("During the weekly self-improvement loop, you must evaluate the gathered performance metrics (PnL and Win Rate) and programmatically update parameters in SOUL.md using update_soul_section.py according to these rules:")
    print()
    print("* **Win Rate < 30%** (indicates high false signal rate or momentum decay):")
    print("  * Tighten Webhook Ingestion Gates in Section 8: decrease `market cap` limit to `$350,000` (or 30% reduction from current) and increase signal age minimum (e.g. from 60 seconds to 90 seconds) to avoid immediate rugs.")
    print("  * Reduce max active positions limit to `3`.")
    print("* **Weekly PnL < -10%** (indicates severe drawdowns):")
    print("  * Reduce trade size by `25%` to preserve capital.")
    print("  * Widen stop-loss threshold by `5%` (e.g. from `-25%` to `-30%`) to avoid stop-hunting in high volatility, and reduce take-profit target to lock in gains earlier.")
    print("* **Win Rate > 60% AND Weekly PnL > +10%** (indicates strong market alignment):")
    print("  * Return max active positions limit to `5`.")
    print("  * Restore position size to normal.")
    print("* **DLMM Weekly Realized PnL < -1.0 SOL or Exit Count > 0 with Win Rate < 40%**:")
    print("  * Tighten Ingestion Gates in Section 9: increase `Minimum TVL` by 50% (e.g. up to `$20,000`), increase `Minimum Base Organic Score` to `70` to filter out higher risk memecoins.")
    print("  * Tighten Exit Parameters in Section 9: decrease `Trailing TP Drop` to `1.0%` (to exit positions quicker when PnL declines from peak), and decrease `Max Out of Range Minutes` to `20` minutes (to exit inactive ranges quicker).")
    print("* **DLMM Weekly Realized PnL > +1.0 SOL**:")
    print("  * Loosen Ingestion Gates in Section 9 to capture more volume: restore `Minimum TVL` to `$10,000` and `Minimum Base Organic Score` to `60`.")
    print("  * Loosen Exit Parameters in Section 9 to let profits ride: increase `Trailing TP Trigger` to `7.5%` or `10.0%` and set `Trailing TP Drop` to `2.0%`.")
    print("* **No significant performance drift**:")
    print("  * Retain parameters unchanged and report 'Strategy stable, no adjustments required'.")
    print()


if __name__ == "__main__":
    main()
