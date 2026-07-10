#!/usr/bin/env python3
"""Reconcile the local DLMM close journal against the Meteora portfolio API.

The journal (memories/dlmm_closes.jsonl) is self-reported by dlmm_monitor.py and
historically missed ~95% of closes; the Meteora datapi portfolio endpoint is the
ground truth. This script diffs the two and prints:

  * overall wallet PnL from the API (SOL-denominated),
  * pools the API says closed in the window but have no journal entry,
  * matched entries whose journaled pnl_pct diverges beyond tolerance.

Usage:
  python3 dlmm_reconcile.py [--days 30] [--tolerance 2.0]
"""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

PORTFOLIO_API = "https://dlmm.datapi.meteora.ag/portfolio"
# Daily-aggregate ground truth: pnl = withdrawn + fees - deposit, straight from
# on-chain flows. The bot's own Redis book (sol:dlmm:pnl:daily:*) is written from
# Portfolio-API snapshots at close time and can drift (see the -100% suspect-read
# guard in dlmm_monitor.py); this endpoint is what actually happened.
CALENDAR_API = "https://portfolio.datapi.meteora.ag/chart/calendar"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROFILE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(SCRIPT_DIR)))


def get_wallet_address():
    try:
        with open(os.path.join(PROFILE_DIR, ".env")) as f:
            for line in f:
                if line.startswith("SOLANA_PUBLIC_KEY="):
                    return line.split("=", 1)[1].strip().strip("\"'")
    except Exception:
        pass
    return None


def fetch_portfolio(wallet, days):
    pools, page = [], 1
    while True:
        url = f"{PORTFOLIO_API}?user={wallet}&page={page}&pageSize=50&daysBack={days}"
        req = urllib.request.Request(url, headers={"User-Agent": "dlmm-lp/1.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
        pools += data.get("pools") or []
        if not data.get("hasNext"):
            return pools
        page += 1


def fetch_calendar(wallet, days):
    """Return [(utc_date, pnl_sol)] for the last `days` days from the calendar API."""
    today = datetime.datetime.now(datetime.timezone.utc).date()
    months = {today.strftime("%Y-%m"), (today - datetime.timedelta(days=days)).strftime("%Y-%m")}
    points = []
    for month in sorted(months):
        url = f"{CALENDAR_API}/{wallet}?month={month}"
        req = urllib.request.Request(url, headers={"User-Agent": "dlmm-lp/1.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())
        points += data.get("data_points") or []
    cutoff = today - datetime.timedelta(days=days)
    out = []
    for p in points:
        d = datetime.date.fromisoformat(str(p.get("date_time", ""))[:10])
        if cutoff <= d <= today:
            out.append((d.isoformat(), float(p.get("pnl_sol") or 0)))
    return sorted(out)


def redis_daily_book(dates):
    """Return {date: total_sol} from the bot's own Redis daily book. Dates the
    monitor booked under a different local-timezone day still land in the window
    sum, so compare sums, not individual days."""
    book = {}
    for d in dates:
        try:
            res = subprocess.run(["redis-cli", "hget", f"sol:dlmm:pnl:daily:{d}", "total_sol"],
                                 capture_output=True, text=True, timeout=10)
            val = res.stdout.strip()
            if val and val != "(nil)":
                book[d] = float(val)
        except Exception:
            pass
    return book


def load_journal():
    """Yield {pool, symbol, pnl_pct} from every journal line, tolerating both the
    uniform monitor schema and the legacy free-text 'content' entries."""
    path = os.path.join(PROFILE_DIR, "memories", "dlmm_closes.jsonl")
    entries = []
    if not os.path.exists(path):
        return entries
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            pool = rec.get("pool")
            pnl = rec.get("pnl_pct")
            symbol = None
            content = rec.get("content", "")
            if pool and "-" in str(pool) and not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", str(pool)):
                # legacy entries put the pair name in "pool"
                symbol, pool = str(pool).split("-")[0].upper(), None
            if rec.get("pair"):
                symbol = str(rec["pair"]).split("-")[0].upper()
            if content and pnl is None:
                m = re.search(r"pnl=([+-]?[\d.]+)%", content)
                pnl = float(m.group(1)) if m else None
            if content and not symbol:
                m = re.search(r"pool=(\S+?)-SOL", content)
                symbol = m.group(1).upper() if m else None
            if pnl is None and symbol is None and pool is None:
                continue
            entries.append({"pool": pool, "symbol": symbol,
                            "pnl_pct": float(pnl) if pnl is not None else None})
    return entries


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30, help="API lookback window (daysBack)")
    ap.add_argument("--tolerance", type=float, default=2.0,
                    help="Max |journal - API| pnl_pct divergence in percentage points")
    cli = ap.parse_args()

    wallet = get_wallet_address()
    if not wallet:
        print("ERROR: SOLANA_PUBLIC_KEY not found in profile .env")
        sys.exit(1)

    pools = fetch_portfolio(wallet, cli.days)
    journal = load_journal()
    f = lambda x: float(x or 0)
    cutoff = time.time() - cli.days * 86400

    tot_pnl = sum(f(p.get("pnlSol")) for p in pools)
    tot_fee = sum(f(p.get("totalFeeSol")) for p in pools)
    tot_dep = sum(f(p.get("totalDepositSol")) for p in pools)
    print(f"=== Meteora API ground truth (last {cli.days}d, {len(pools)} pools) ===")
    print(f"Net PnL: {tot_pnl:+.4f} SOL on {tot_dep:.2f} SOL deployed | fees earned {tot_fee:+.4f} SOL "
          f"| price/IL component {tot_pnl - tot_fee:+.4f} SOL")

    by_pool = {p["poolAddress"]: p for p in pools}
    by_symbol = {}
    for p in pools:
        by_symbol.setdefault(str(p.get("tokenX", "")).upper(), []).append(p)

    missing, mismatched, matched = [], [], 0
    seen_pools = set()
    for e in journal:
        p = by_pool.get(e["pool"]) if e["pool"] else None
        if p is None and e["symbol"] and len(by_symbol.get(e["symbol"], [])) == 1:
            p = by_symbol[e["symbol"]][0]
        if p is None:
            continue
        matched += 1
        seen_pools.add(p["poolAddress"])
        api_pct = f(p.get("pnlSolPctChange"))
        if e["pnl_pct"] is not None and abs(e["pnl_pct"] - api_pct) > cli.tolerance:
            mismatched.append((e, p, api_pct))

    for p in pools:
        last = p.get("lastClosedAt")
        if p["poolAddress"] in seen_pools or not last:
            continue
        if f(last) >= cutoff:
            missing.append(p)

    print("\n=== Journal coverage ===")
    print(f"journal entries: {len(journal)} | matched to API pools: {matched}")
    if missing:
        print(f"\nPools closed in window with NO journal entry ({len(missing)}):")
        for p in sorted(missing, key=lambda p: f(p.get("pnlSol"))):
            print(f"  {p.get('tokenX','?'):<14} {f(p.get('pnlSol')):+.4f} SOL ({f(p.get('pnlSolPctChange')):+.2f}%) "
                  f"pool {p['poolAddress']}")
    else:
        print("No unjournaled closes in window.")
    if mismatched:
        print(f"\nPnL divergence > {cli.tolerance}pp ({len(mismatched)}):")
        for e, p, api_pct in mismatched:
            print(f"  {p.get('tokenX','?'):<14} journal {e['pnl_pct']:+.2f}% vs API {api_pct:+.2f}% "
                  f"(pool aggregates all positions — divergence may be multi-position)")
    else:
        print("No PnL divergences beyond tolerance.")

    # Internal book vs on-chain truth. Redis days are keyed in server-local time,
    # calendar days in UTC, so individual days can legitimately differ at the
    # boundary — the window SUM is the signal.
    print(f"\n=== Redis book vs calendar truth (last {cli.days}d) ===")
    try:
        calendar = fetch_calendar(wallet, cli.days)
    except Exception as e:
        print(f"calendar API unavailable ({e}) — skipping book check")
        calendar = None
    if calendar:
        cal_dates = [d for d, _ in calendar]
        book = redis_daily_book(cal_dates)
        print(f"{'date (UTC)':<12} {'truth SOL':>10} {'book SOL':>10}")
        for d, truth in calendar:
            b = book.get(d)
            print(f"{d:<12} {truth:>+10.4f} {(f'{b:+.4f}' if b is not None else '—'):>10}")
        truth_sum = sum(v for _, v in calendar)
        book_sum = sum(book.values())
        delta = book_sum - truth_sum
        print(f"{'SUM':<12} {truth_sum:>+10.4f} {book_sum:>+10.4f}")
        if abs(delta) > 0.02:
            print(f"⚠️ BOOK DRIFT: internal book differs from on-chain truth by {delta:+.4f} SOL "
                  f"over {cli.days}d — phantom bookings or missed closes. Trust the truth column.")
        else:
            print("Book matches on-chain truth ✅")


if __name__ == "__main__":
    main()
