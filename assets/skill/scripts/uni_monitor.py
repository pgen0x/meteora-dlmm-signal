#!/usr/bin/env python3
"""uni_monitor.py — Robinhood Chain (Uniswap v3 + v4) position monitor.

EVM sibling of dlmm_monitor.py. One-shot scan (run on a loop by
uni_monitor_loop.sh): reads open positions from BOTH executors — v3 via
uni_executor.js (NonfungiblePositionManager) and v4 via uni_v4_executor.js
(the v4 PositionManager; skipped when the script is absent) — and applies the
SAME exit rulebook the Solana monitor uses — hard SL/TP, trailing
profit-ratchet, fast-out velocity exit, sustained-downtrend exit, and
out-of-range timeout — closing through `UNI_CLOSE_AUTH=1 <executor> close`
when a rule trips.

This is the ONLY authorized closer for the venue (both executors' close
commands refuse to run without UNI_CLOSE_AUTH=1 or --force), mirroring the
Solana monitor's DLMM_CLOSE_AUTH contract. PnL is quote-denominated: WETH on
v3, the pool's own quote asset on v4 (WETH, native ETH, or USDG — the state
output's `quoteSymbol` says which). The rulebook is percentages, so the
thresholds are unit-agnostic and shared across protocols.

DRY_RUN=true still tracks peaks and prints decisions, but simulates closes.
"""

import json
import os
import subprocess
import sys
import time
import urllib.request

from local_indicators import check_local_indicators

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROFILE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(SCRIPT_DIR)))
# (protocol, executor path) pairs, monitored in order. v4 rides the same tick
# only when its script is deployed — an absent file is pre-Phase-7, not an
# error. v3 state keys stay bare tokenIds (the live state file predates v4);
# v4 keys are namespaced "v4:<tokenId>" because the two PositionManagers mint
# independent, colliding tokenId sequences.
EXECUTORS = [("v3", os.path.join(SCRIPT_DIR, "uni_executor.js"))]
_V4_EXECUTOR = os.path.join(SCRIPT_DIR, "uni_v4_executor.js")
if os.path.exists(_V4_EXECUTOR):
    EXECUTORS.append(("v4", _V4_EXECUTOR))
STATE_PATH = os.path.join(PROFILE_DIR, "memories", "uni_monitor_state.json")
CLOSES_PATH = os.path.join(PROFILE_DIR, "memories", "uni_closes.jsonl")

DRY_RUN = os.environ.get("DRY_RUN", "").lower() == "true"
# Report-only: read positions + state + momentum, compute the decision label for
# each, print a status card + MONITOR_REPORT JSON, and NEVER close or mutate the
# persisted state file. This is the mode the Hermes reporting cron runs — the
# systemd loop (rh-dlmm-monitor.service) owns the actual exits, so the cron is a
# read-only mirror ("rules not cadence are the lever"). A report tick must never
# race the loop's on-chain writes.
REPORT_ONLY = "--report-only" in sys.argv

# Exit thresholds — percentages, so identical to the Solana monitor's
# (dlmm_monitor.py). "Same like solana" per the operator; recalibrate from the
# venue's own close journal once it has live outcomes.
STOP_LOSS_PCT = float(os.environ.get("UNI_STOP_LOSS_PCT", "-25.0"))
TAKE_PROFIT_PCT = float(os.environ.get("UNI_TAKE_PROFIT_PCT", "50.0"))
TRAILING_TRIGGER_PCT = float(os.environ.get("UNI_TRAILING_TRIGGER_PCT", "5.0"))
TRAILING_DROP_PCT = float(os.environ.get("UNI_TRAILING_DROP_PCT", "1.5"))
TRAILING_MIN_LOCK_PCT = 0.3        # round-trip swap cost floor for a "profit" exit
EMERGENCY_SL_BUFFER_PCT = 3.0      # below SL-buffer, close bypasses the age grace
FAST_EXIT_M5_PCT = -3.0            # armed trailing + this 5m dump -> close now
DOWNTREND_1H_PCT = -5.0            # sustained-downtrend exit (both must trip)
DOWNTREND_PNL_PCT = -5.0
MAX_OOR_MINUTES = 30.0             # out-of-range this long -> close (fee-dead)
MIN_AGE_MIN_BEFORE_SL = 5.0        # grace so a fresh mint's settling isn't an SL

# Pre-exit indicator confirmation — the Solana monitor's supertrend timing
# check (dlmm_monitor.py step 7), applied to this venue's non-emergency exits:
# a trailing/fast-out/downtrend close is postponed while the indicators still
# read bullish (a dip, not a dump), then forced once the block ages past the
# cap. Emergency SL / hard SL / TP / OOR-timeout closes are never postponed.
INDICATORS_ENABLED = os.environ.get("UNI_INDICATORS_ENABLED", "true").lower() != "false"
INDICATORS_PRESET = os.environ.get("UNI_INDICATORS_PRESET", "supertrend_or_rsi")
MAX_INDICATOR_BLOCK_MINUTES = 60.0


def exit_confirmable(reason):
    """Only momentum-shaped exits wait for indicator confirmation; hard-risk
    rules (SL, TP, fee-dead OOR) close unconditionally, mirroring the Solana
    monitor's is_emergency carve-out."""
    return reason.startswith(("trailing exit", "fast-out", "downtrend"))


def run_executor(executor, args, close_auth=False):
    """Run an executor script and return (parsed_json, err). Reads the last
    stdout line as the JSON payload."""
    env = dict(os.environ)
    if close_auth:
        env["UNI_CLOSE_AUTH"] = "1"
    try:
        r = subprocess.run(["node", executor] + args, capture_output=True,
                           text=True, timeout=150, env=env)
        out = (r.stdout or "").strip()
        line = out.splitlines()[-1] if out else ""
        try:
            return json.loads(line), None
        except json.JSONDecodeError:
            return None, (r.stderr or out or "no output").strip()
    except Exception as e:
        return None, str(e)


def load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state):
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        with open(STATE_PATH, "w") as f:
            json.dump(state, f)
    except OSError as e:
        print(f"warn: could not save monitor state: {e}")


# GeckoTerminal 403s the default urllib User-Agent ("Python-urllib/3.x") — the
# request never reaches the API, fetch_momentum returns (None, None), and the two
# momentum-driven exits (fast-out, sustained-downtrend) silently never fire,
# because missing data is treated as passing. Any non-default UA is accepted
# (Go's default gets 200, which is why the discovery daemon was never affected).
# Same trap the Jupiter audit gate hit. Do not drop this header.
USER_AGENT = "mdtb-uni-monitor/1.0"


def fetch_momentum(pool):
    """Best-effort GeckoTerminal price-change windows for a pool. Returns
    (m5, h1) percent, or (None, None) — missing data never fires a rule."""
    url = f"https://api.geckoterminal.com/api/v2/networks/robinhood/pools/{pool}"
    try:
        req = urllib.request.Request(url, headers={
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            d = json.load(resp)
        pc = d["data"]["attributes"]["price_change_percentage"]
        return float(pc.get("m5") or 0), float(pc.get("h1") or 0)
    except Exception:
        return None, None


def fetch_eth_usd():
    """Best-effort ETH/USD from Blockscout's stats endpoint — one request per
    tick, shared by every position row. None just drops the $ figures from the
    card; no exit rule reads it."""
    url = "https://robinhoodchain.blockscout.com/api/v2/stats"
    try:
        req = urllib.request.Request(url, headers={
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            d = json.load(resp)
        return float(d["coin_price"])
    except Exception:
        return None


def trailing_floor_pct(peak):
    """Profit-ratchet floor — identical shape to dlmm_monitor.py: tight near
    activation, locks progressively more as the peak grows, gives big winners
    room instead of a flat drop that caps every win."""
    if peak >= 20.0:
        return max(14.0, peak * 0.70)
    if peak >= 10.0:
        return max(6.0, peak - 4.0)
    if peak >= 5.0:
        return max(2.0, peak - 2.5)
    return peak - TRAILING_DROP_PCT


def decide(pnl, peak, in_range, age_min, oor_min, m5, h1):
    """Return a close reason string, or None to hold. Mirrors the Solana
    monitor's rule precedence: emergency SL first, then hard SL/TP, then
    trailing/fast-out/downtrend, then OOR timeout."""
    if pnl is not None:
        # Emergency SL — bypasses the age grace.
        if pnl <= STOP_LOSS_PCT - EMERGENCY_SL_BUFFER_PCT:
            return f"emergency SL {pnl:.1f}% <= {STOP_LOSS_PCT - EMERGENCY_SL_BUFFER_PCT:.1f}%"
        # Hard SL (after a short settle grace).
        if pnl <= STOP_LOSS_PCT and (age_min is None or age_min >= MIN_AGE_MIN_BEFORE_SL):
            return f"stop loss {pnl:.1f}% <= {STOP_LOSS_PCT:.1f}%"
        # Hard TP.
        if pnl >= TAKE_PROFIT_PCT:
            return f"take profit {pnl:.1f}% >= {TAKE_PROFIT_PCT:.1f}%"
        # Trailing profit ratchet (armed once peak clears the trigger).
        if peak >= TRAILING_TRIGGER_PCT:
            floor = trailing_floor_pct(peak)
            if pnl < floor and pnl >= TRAILING_MIN_LOCK_PCT:
                return f"trailing exit {pnl:.1f}% < floor {floor:.1f}% (peak {peak:.1f}%)"
            # Fast-out velocity: armed + still locked + a steep 5m dump that
            # would gap through the floor between ticks.
            if m5 is not None and m5 <= FAST_EXIT_M5_PCT and pnl >= TRAILING_MIN_LOCK_PCT:
                return f"fast-out {m5:.1f}% 5m dump (pnl {pnl:.1f}%, peak {peak:.1f}%)"
        # Sustained downtrend: underwater AND token in steady 1h decline.
        if h1 is not None and h1 <= DOWNTREND_1H_PCT and pnl <= DOWNTREND_PNL_PCT:
            return f"downtrend 1h {h1:.1f}% + pnl {pnl:.1f}%"
    # Out-of-range timeout — fee-dead capital past the patience window.
    if not in_range and oor_min >= MAX_OOR_MINUTES:
        return f"out of range {oor_min:.0f}m >= {MAX_OOR_MINUTES:.0f}m"
    return None


def journal_close(rec):
    try:
        os.makedirs(os.path.dirname(CLOSES_PATH), exist_ok=True)
        with open(CLOSES_PATH, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError as e:
        print(f"warn: could not journal close: {e}")


def sweep_stranded(proto, executor):
    """Retry the exit sell for bags a close could not unload.

    Runs every tick, before the position pass, once per executor (the two keep
    separate stranded journals). A pool that was dead when we closed can be
    re-seeded by another LP, and a sell that reverted on a transient just works
    next time — so the bag is worth re-offering cheaply and often. No-op (one
    RPC read) when nothing is stranded.
    """
    out, err = run_executor(executor, ["sweep"], close_auth=True)
    if err or not out:
        if err:
            print(f"monitor: {proto} sweep failed: {err}")
        return
    if not out.get("swept"):
        return
    for r in out.get("results", []):
        # v3 reports weth_out; v4 reports quote_out + quote_symbol (the quote
        # varies per pool there).
        recovered = r.get("quote_out", r.get("weth_out", "0"))
        unit = r.get("quote_symbol", "WETH")
        if r.get("resolved") and recovered != "0":
            print(f"monitor: SWEPT {r.get('symbol')} -> {recovered} {unit} (fee {r.get('fee')})")
            alert(f"🧹 Robinhood sweep recovered {recovered} {unit}\n"
                  f"sold stranded {r.get('symbol')} ({r.get('token')})")
        elif r.get("resolved") is False:
            print(f"monitor: still stranded {r.get('symbol')} "
                  f"(attempt {r.get('attempts')}, retry in {r.get('retry_in_s')}s): {r.get('reason')}")


def alert(text):
    """Best-effort operator alert via hermes; never fails the tick."""
    target = os.environ.get("DLMM_ALERT_TARGET", "telegram")
    if not target:
        return
    try:
        subprocess.run(["hermes", "send", "-t", target, "-m", text, "-q"],
                       timeout=30, capture_output=True)
    except Exception:
        pass


def render_card(rows):
    """Telegram status card for the reporting cron. Deterministic — the cron
    prompt copies it verbatim, so the format lives here, not in the agent turn.
    First line prefix is load-bearing (the cron's OUTPUT RULE keys off it)."""
    ts = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    lines = [f"Robinhood LP Status — {ts}", f"📊 Active Positions: {len(rows)}"]
    if not rows:
        lines.append("\nNo active positions. Bot is idle.")
    for r in rows:
        pnl = f"{r['pnl_pct']:+.1f}%" if r["pnl_pct"] is not None else "n/a"
        if r.get("pnl_usd") is not None:
            pnl += f" (${r['pnl_usd']:+.2f})"
        val = r["value_weth"]
        ent = r["entry_weth"]
        qsym = r.get("quote_symbol") or "WETH"
        # USDG values ARE dollars — two decimals, no ETH/USD conversion.
        if qsym == "USDG":
            val_s = f"{val:.2f}" if val is not None else "?"
            ent_s = f"{ent:.2f}" if ent is not None else "?"
            usd_s = ""
        else:
            val_s = f"{val:.5f}" if val is not None else "?"
            ent_s = f"{ent:.5f}" if ent is not None else "?"
            usd = r.get("eth_usd")
            usd_s = (f" (${val * usd:.2f} / ${ent * usd:.2f})"
                     if usd and val is not None and ent is not None else "")
        age_min = r["age_min"]
        if age_min is None:
            age = "n/a"
        elif age_min >= 60:
            age = f"{int(age_min // 60)}h{int(age_min % 60):02d}m"
        else:
            age = f"{age_min:.0f}m"
        rng = "🟢 In Range" if r["in_range"] else f"🔴 OOR {r['oor_min']:.0f}m"
        m5 = f"{r['m5']:+.1f}%" if r["m5"] is not None else "n/a"
        h1 = f"{r['h1']:+.1f}%" if r["h1"] is not None else "n/a"
        # Only ⚠️ bullets are printed — a healthy position shows just the table
        # and the summary line, matching the Solana card's compact layout.
        warnings = []
        if not r["in_range"]:
            warnings.append(f"⚠️ Out of range {r['oor_min']:.0f}m (limit {MAX_OOR_MINUTES:.0f}m)")
        if r["peak_pct"] >= TRAILING_TRIGGER_PCT:
            warnings.append(f"⚠️ Trailing stop ACTIVE (peak {r['peak_pct']:+.1f}%, floor {trailing_floor_pct(r['peak_pct']):+.1f}%)")
        if r["m5"] is None or r["h1"] is None:
            warnings.append("⚠️ No momentum data — fast-out and downtrend exits cannot fire")
        lines += [
            "",
            "---",
            "",
            f"### {r.get('pair') or 'Position #' + r['tokenId']}",
            (f"`{r['pool']}`" if r.get("pool") else "`?`") + f" · #{r['tokenId']}",
            "",
            "| Metric | Value |",
            "|--------|-------|",
            f"| PnL | {pnl} · peak {r['peak_pct']:+.1f}% |",
            f"| Value | {val_s} / {ent_s} {qsym}{usd_s} |",
            f"| Range | {rng} · age {age} |",
            f"| Price 5m/1h | {m5} / {h1} |",
        ] + [f"- {w}" for w in warnings] + [
            "",
            f"→ {r['decision']}: {r['reason']}",
        ]
    return "\n".join(lines)


def state_key(proto, tid):
    """Peak/oor state key. v3 keys are bare tokenIds (the live state file
    predates v4 and must keep matching); v4 is namespaced because the two
    PositionManagers mint colliding tokenId sequences."""
    return str(tid) if proto == "v3" else f"{proto}:{tid}"


def main():
    # Gather (proto, executor, tokenId) across executors. One executor's read
    # failure must not blind the monitor to the other's positions — note it,
    # keep scanning, and only fail the tick when EVERY read failed.
    work = []
    errors = []
    for proto, executor in EXECUTORS:
        pos, err = run_executor(executor, ["positions"])
        if err:
            errors.append(f"{proto}: {err}")
            print(f"monitor: {proto} positions read failed: {err}")
            continue
        # Stranded bags outlive the positions that created them, so sweep
        # BEFORE the no-open-positions early return below — the venue sits
        # flat most of the time, and a sweep that only ran when something was
        # open would never run. Each executor keeps its own stranded journal.
        if not REPORT_ONLY and not DRY_RUN:
            sweep_stranded(proto, executor)
        for p in pos.get("positions", []):
            work.append((proto, executor, p["tokenId"]))
    if errors and len(errors) == len(EXECUTORS):
        # Report-only must still hand the cron a parseable line so it can decide
        # SILENT vs surface-the-error, instead of leaving the agent to guess.
        if REPORT_ONLY:
            print("MONITOR_REPORT:" + json.dumps({"positions": [], "error": "; ".join(errors)}))
        sys.exit(1)

    if not work:
        if REPORT_ONLY:
            report = {"positions": []}
            if errors:
                report["error"] = "; ".join(errors)
            print("MONITOR_REPORT:" + json.dumps(report))
            return
        print("monitor: no open positions")
        return

    if REPORT_ONLY:
        state = load_state()
        now = time.time()
        eth_usd = fetch_eth_usd()
        rows = []
        for proto, executor, tid in work:
            s, serr = run_executor(executor, ["state", "--id", str(tid)])
            if serr or not s:
                print(f"monitor: {proto} state #{tid} failed: {serr}")
                continue
            pnl = s.get("pnlPct")
            in_range = bool(s.get("inRange"))
            age_min = s.get("ageMin")
            pool = s.get("pool")
            qsym = s.get("quoteSymbol") or "WETH"
            # Read persisted peak/oor without mutating — the systemd loop owns
            # writes to STATE_PATH; the report reflects its last tick.
            ps = state.get(state_key(proto, tid), {"peak_pnl": 0.0, "oor_since": None})
            peak = ps.get("peak_pnl", 0.0)
            if pnl is not None and pnl > peak:
                peak = pnl
            if in_range or not ps.get("oor_since"):
                oor_min = 0.0
            else:
                oor_min = (now - ps["oor_since"]) / 60.0
            m5, h1 = fetch_momentum(pool) if pool else (None, None)
            reason = decide(pnl, peak, in_range, age_min, oor_min, m5, h1)
            val_w, ent_w = s.get("valueWeth"), s.get("entryWeth")
            # USDG positions are dollar-quoted already; everything else is
            # ETH-quoted and needs the ETH/USD conversion.
            if val_w is None or ent_w is None:
                pnl_usd = None
            elif qsym == "USDG":
                pnl_usd = round(val_w - ent_w, 2)
            else:
                pnl_usd = round((val_w - ent_w) * eth_usd, 2) if eth_usd else None
            rows.append({
                "tokenId": str(tid), "protocol": proto, "pool": pool, "pair": s.get("pair"),
                "quote_symbol": qsym,
                "pnl_pct": round(pnl, 2) if pnl is not None else None,
                "pnl_usd": pnl_usd, "eth_usd": eth_usd,
                "peak_pct": round(peak, 2), "in_range": in_range,
                "oor_min": round(oor_min, 1), "age_min": round(age_min, 1) if age_min is not None else None,
                "m5": m5, "h1": h1,
                "value_weth": val_w, "entry_weth": ent_w,
                "decision": "CLOSE" if reason else "HOLD",
                "reason": reason or "healthy — held by monitor loop",
            })
        report = {"positions": rows}
        if errors:
            report["error"] = "; ".join(errors)
        print(render_card(rows))
        print("MONITOR_REPORT:" + json.dumps(report))
        return

    state = load_state()
    now = time.time()
    live = set()

    for proto, executor, tid in work:
        skey = state_key(proto, tid)
        live.add(skey)
        s, err = run_executor(executor, ["state", "--id", str(tid)])
        if err or not s:
            print(f"monitor: {proto} state #{tid} failed: {err}")
            continue

        pnl = s.get("pnlPct")
        in_range = bool(s.get("inRange"))
        age_min = s.get("ageMin")
        pool = s.get("pool")
        pair = s.get("pair") or f"#{tid}"
        qsym = s.get("quoteSymbol") or "WETH"

        ps = state.setdefault(skey, {"peak_pnl": 0.0, "oor_since": None})
        if pnl is not None and pnl > ps["peak_pnl"]:
            ps["peak_pnl"] = pnl
        peak = ps["peak_pnl"]

        if in_range:
            ps["oor_since"] = None
            oor_min = 0.0
        else:
            if ps["oor_since"] is None:
                ps["oor_since"] = now
            oor_min = (now - ps["oor_since"]) / 60.0

        m5, h1 = fetch_momentum(pool) if pool else (None, None)
        reason = decide(pnl, peak, in_range, age_min, oor_min, m5, h1)

        # Pre-exit indicator confirmation (non-emergency exits only): postpone
        # the close while supertrend/RSI still read bullish — a dip inside an
        # uptrend, not a dump — but force it once the block outlives the cap.
        if reason and INDICATORS_ENABLED and exit_confirmable(reason) and pool:
            blocked_since = ps.get("ind_block_since")
            blocked_min = (now - blocked_since) / 60.0 if blocked_since else 0.0
            if blocked_min >= MAX_INDICATOR_BLOCK_MINUTES:
                print(f"monitor: {proto} #{tid} indicator exit block timed out after {blocked_min:.0f}m — forcing close")
                ps.pop("ind_block_since", None)
            else:
                confirmed = check_local_indicators(pool, s.get("token"), "exit",
                                                   INDICATORS_PRESET, "24h", network="robinhood")
                if confirmed is False:
                    print(f"monitor: {proto} #{tid} exit postponed ({INDICATORS_PRESET} rejected): {reason}")
                    if not blocked_since:
                        ps["ind_block_since"] = now
                    reason = None
                else:
                    if confirmed is None:
                        print(f"monitor: {proto} #{tid} indicator data unavailable — proceeding with exit (fail-open)")
                    ps.pop("ind_block_since", None)
        elif not reason:
            # Rule no longer fires — the dip resolved; a stale block must not
            # shortcut a future, unrelated exit straight to "timed out".
            ps.pop("ind_block_since", None)

        pnl_str = f"{pnl:.1f}%" if pnl is not None else "n/a"
        print(f"monitor: {proto} #{tid} pnl={pnl_str} peak={peak:.1f}% "
              f"{'in' if in_range else 'OUT'}range oor={oor_min:.0f}m "
              f"m5={m5} h1={h1} -> {reason or 'HOLD'}")

        if not reason:
            continue

        if DRY_RUN:
            print(f"monitor: [dry-run] would close {proto} #{tid}: {reason}")
            continue

        out, cerr = run_executor(executor, ["close", "--id", str(tid)], close_auth=True)
        closed = out and out.get("success")
        # A close can succeed while its token->WETH sell fails (rugged pool,
        # sell tax): the liquidity is out and the NFT burned, but the token side
        # is still a bag in the wallet. The executor journals it for `sweep`;
        # record it here too so the close journal never claims a clean exit that
        # actually left value behind.
        stranded = (out or {}).get("stranded")
        journal_close({
            "ts": int(now),
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "tokenId": str(tid), "protocol": proto, "pool": pool,
            "pnl_pct": round(pnl, 4) if pnl is not None else None,
            "peak_pct": round(peak, 4), "age_min": round(age_min, 1) if age_min else None,
            "reason": reason, "success": bool(closed), "dry_run": False,
            "swapped_out": bool((out or {}).get("swapped_out")),
            # weth_out is the v4 executor's alias for quote_out, so this stays
            # populated on both protocols; quote_symbol says what unit it is.
            "weth_out": (out or {}).get("weth_out"),
            "quote_symbol": (out or {}).get("quote_symbol", qsym),
            "stranded": stranded,
        })
        if closed:
            state.pop(skey, None)
            live.discard(skey)
            msg = f"🔴 Robinhood LP closed {pair} (#{tid})\n{reason}\npnl {pnl_str} peak {peak:.1f}%"
            if stranded:
                msg += (f"\n⚠️ {stranded.get('symbol', '?')} NOT sold — {stranded.get('reason', '?')}"
                        f"\ntoken {stranded.get('token')}\nqueued for sweep")
            else:
                msg += (f"\nsold for {(out or {}).get('weth_out', '?')} "
                        f"{(out or {}).get('quote_symbol', qsym)}")
            alert(msg)
            print(f"monitor: CLOSED {proto} #{tid}: {reason}"
                  + (f" [STRANDED {stranded.get('symbol')}]" if stranded else ""))
        else:
            print(f"monitor: CLOSE FAILED {proto} #{tid}: {cerr}")

    # Drop peak/oor state for positions no longer open (closed elsewhere) —
    # but never for an executor whose positions read failed this tick: its
    # positions are missing from `live` because we couldn't see them, not
    # because they closed, and pruning would reset their peaks to zero.
    failed = {e.split(":", 1)[0] for e in errors}
    for key in list(state.keys()):
        proto = key.split(":", 1)[0] if ":" in key else "v3"
        if key not in live and proto not in failed:
            state.pop(key, None)
    save_state(state)


if __name__ == "__main__":
    main()
