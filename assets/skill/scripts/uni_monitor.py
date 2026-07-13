#!/usr/bin/env python3
"""uni_monitor.py — Robinhood Chain (Uniswap v3) position monitor.

EVM sibling of dlmm_monitor.py. One-shot scan (run on a loop by
uni_monitor_loop.sh): reads open NonfungiblePositionManager positions, prices
each via `uni_executor.js state`, and applies the SAME exit rulebook the Solana
monitor uses — hard SL/TP, trailing profit-ratchet, fast-out velocity exit,
sustained-downtrend exit, and out-of-range timeout — closing through
`UNI_CLOSE_AUTH=1 uni_executor.js close` when a rule trips.

This is the ONLY authorized closer for the venue (the executor's close command
refuses to run without UNI_CLOSE_AUTH=1 or --force), mirroring the Solana
monitor's DLMM_CLOSE_AUTH contract. PnL is WETH-denominated (the venue's quote
asset), the analog of the Solana monitor's SOL terms.

DRY_RUN=true still tracks peaks and prints decisions, but simulates closes.
"""

import json
import os
import subprocess
import sys
import time
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROFILE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(SCRIPT_DIR)))
EXECUTOR = os.path.join(SCRIPT_DIR, "uni_executor.js")
STATE_PATH = os.path.join(PROFILE_DIR, "memories", "uni_monitor_state.json")
CLOSES_PATH = os.path.join(PROFILE_DIR, "memories", "uni_closes.jsonl")

DRY_RUN = os.environ.get("DRY_RUN", "").lower() == "true"

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


def run_executor(args, close_auth=False):
    """Run uni_executor.js and return (parsed_json, err). Reads the last stdout
    line as the JSON payload."""
    env = dict(os.environ)
    if close_auth:
        env["UNI_CLOSE_AUTH"] = "1"
    try:
        r = subprocess.run(["node", EXECUTOR] + args, capture_output=True,
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


def fetch_momentum(pool):
    """Best-effort GeckoTerminal price-change windows for a pool. Returns
    (m5, h1) percent, or (None, None) — missing data never fires a rule."""
    url = f"https://api.geckoterminal.com/api/v2/networks/robinhood/pools/{pool}"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            d = json.load(resp)
        pc = d["data"]["attributes"]["price_change_percentage"]
        return float(pc.get("m5") or 0), float(pc.get("h1") or 0)
    except Exception:
        return None, None


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


def main():
    pos, err = run_executor(["positions"])
    if err:
        print(f"monitor: positions read failed: {err}")
        sys.exit(1)
    ids = [p["tokenId"] for p in pos.get("positions", [])]
    if not ids:
        print("monitor: no open positions")
        return

    state = load_state()
    now = time.time()
    live = set()

    for tid in ids:
        live.add(str(tid))
        s, err = run_executor(["state", "--id", str(tid)])
        if err or not s:
            print(f"monitor: state #{tid} failed: {err}")
            continue

        pnl = s.get("pnlPct")
        in_range = bool(s.get("inRange"))
        age_min = s.get("ageMin")
        pool = s.get("pool")

        ps = state.setdefault(str(tid), {"peak_pnl": 0.0, "oor_since": None})
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

        pnl_str = f"{pnl:.1f}%" if pnl is not None else "n/a"
        print(f"monitor: #{tid} pnl={pnl_str} peak={peak:.1f}% "
              f"{'in' if in_range else 'OUT'}range oor={oor_min:.0f}m "
              f"m5={m5} h1={h1} -> {reason or 'HOLD'}")

        if not reason:
            continue

        if DRY_RUN:
            print(f"monitor: [dry-run] would close #{tid}: {reason}")
            continue

        out, cerr = run_executor(["close", "--id", str(tid)], close_auth=True)
        closed = out and out.get("success")
        journal_close({
            "ts": int(now),
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "tokenId": str(tid), "pool": pool,
            "pnl_pct": round(pnl, 4) if pnl is not None else None,
            "peak_pct": round(peak, 4), "age_min": round(age_min, 1) if age_min else None,
            "reason": reason, "success": bool(closed), "dry_run": False,
        })
        if closed:
            state.pop(str(tid), None)
            live.discard(str(tid))
            alert(f"🔴 Robinhood LP closed #{tid}\n{reason}\npnl {pnl_str} peak {peak:.1f}%")
            print(f"monitor: CLOSED #{tid}: {reason}")
        else:
            print(f"monitor: CLOSE FAILED #{tid}: {cerr}")

    # Drop peak/oor state for positions no longer open (closed elsewhere).
    for tid in list(state.keys()):
        if tid not in live:
            state.pop(tid, None)
    save_state(state)


if __name__ == "__main__":
    main()
