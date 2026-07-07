#!/usr/bin/env python3
"""Darwinian signal weighting — learn which entry signals predict winners.

Reads the close journal (memories/dlmm_closes.jsonl), splits closed positions
into wins/losses, and computes each entry signal's predictive lift (normalized
win-mean minus loss-mean). Top-quartile signals get boosted, bottom-quartile
decayed, clamped to [0.3, 2.5]. Weights persist to memories/signal_weights.json
and to Redis (sol:dlmm:signal_weights) where the deploy agent reads them to
prioritize candidates whose strongest attributes carry high weights.

Runs from the tail of dlmm_monitor.py on every cycle; self-guards so a real
recalc happens at most every RECALC_GUARD_SECS and only with enough samples.
"""
import argparse
import json
import os
import subprocess
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROFILE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(SCRIPT_DIR)))
CLOSES_PATH = os.path.join(PROFILE_DIR, "memories", "dlmm_closes.jsonl")
WEIGHTS_PATH = os.path.join(PROFILE_DIR, "memories", "signal_weights.json")
REDIS_WEIGHTS_KEY = "sol:dlmm:signal_weights"

WINDOW_DAYS = 60
MIN_SAMPLES = 10
BOOST_FACTOR = 1.05
DECAY_FACTOR = 0.95
WEIGHT_FLOOR = 0.3
WEIGHT_CEILING = 2.5
RECALC_GUARD_SECS = 6 * 3600

SIGNAL_NAMES = [
    "score", "organic_score", "fee_tvl_ratio", "fee_active_tvl_ratio",
    "volatility", "mcap", "holders", "tvl", "fee_pct",
    "volume_tvl_ratio", "swap_count", "unique_traders",
    "bot_holders_pct", "global_fees_sol",
]

# Directional signals: higher value should mean better candidate, so lift keeps
# its sign. The rest (volatility, mcap, tvl, bot %, ...) are non-directional —
# any separation between winners and losers is informative, so |lift| is used.
HIGHER_IS_BETTER = {
    "score", "organic_score", "fee_tvl_ratio", "fee_active_tvl_ratio",
    "holders", "volume_tvl_ratio", "unique_traders", "global_fees_sol",
}


def run_command(cmd, timeout=10):
    try:
        out = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return out.stdout.strip()
    except Exception:
        return ""


def load_weights():
    if os.path.exists(WEIGHTS_PATH):
        try:
            with open(WEIGHTS_PATH, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {"weights": {}, "last_recalc": None, "last_recalc_ts": 0, "recalc_count": 0, "history": []}


def save_weights(data):
    os.makedirs(os.path.dirname(WEIGHTS_PATH), exist_ok=True)
    with open(WEIGHTS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    # Best-effort Redis mirror — this is what the deploy agent reads.
    compact = json.dumps({"weights": data["weights"], "last_recalc": data["last_recalc"]})
    run_command(f"redis-cli set \"{REDIS_WEIGHTS_KEY}\" '{compact}'")


def load_recent_closes():
    if not os.path.exists(CLOSES_PATH):
        return []
    cutoff = time.time() - WINDOW_DAYS * 86400
    records = []
    with open(CLOSES_PATH, encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("dry_run"):
                continue
            if not isinstance(rec.get("signal"), dict):
                continue  # pre-snapshot records can't be attributed
            if (rec.get("ts") or 0) < cutoff:
                continue
            records.append(rec)
    return records


def outcome_sol(rec):
    if rec.get("pnl_sol") is not None:
        return float(rec["pnl_sol"])
    return float(rec.get("pnl_pct") or 0)


def numeric_lift(signal, wins, losses):
    win_vals = [float(r["signal"][signal]) for r in wins
                if isinstance(r["signal"].get(signal), (int, float))]
    loss_vals = [float(r["signal"][signal]) for r in losses
                 if isinstance(r["signal"].get(signal), (int, float))]
    if not win_vals or not loss_vals or len(win_vals) + len(loss_vals) < MIN_SAMPLES:
        return None
    all_vals = win_vals + loss_vals
    lo, hi = min(all_vals), max(all_vals)
    if hi == lo:
        return 0.0
    norm = lambda v: (v - lo) / (hi - lo)
    win_mean = sum(map(norm, win_vals)) / len(win_vals)
    loss_mean = sum(map(norm, loss_vals)) / len(loss_vals)
    diff = win_mean - loss_mean
    return diff if signal in HIGHER_IS_BETTER else abs(diff)


def recalculate(quiet=False):
    data = load_weights()
    weights = data.get("weights") or {}
    for name in SIGNAL_NAMES:
        weights.setdefault(name, 1.0)

    recent = load_recent_closes()
    wins = [r for r in recent if outcome_sol(r) > 0]
    losses = [r for r in recent if outcome_sol(r) <= 0]
    if len(recent) < MIN_SAMPLES or not wins or not losses:
        if not quiet:
            print(f"Skipping recalc: {len(recent)} attributable closes in {WINDOW_DAYS}d "
                  f"(need >= {MIN_SAMPLES} with both wins and losses)")
        return False

    lifts = {}
    for signal in SIGNAL_NAMES:
        lift = numeric_lift(signal, wins, losses)
        if lift is not None:
            lifts[signal] = lift
    if not lifts:
        if not quiet:
            print("Skipping recalc: no signal had enough samples")
        return False

    ranked = sorted(lifts.items(), key=lambda kv: kv[1], reverse=True)
    q1_end = max(1, round(len(ranked) * 0.25))
    q3_start = len(ranked) - q1_end
    top = {name for name, _ in ranked[:q1_end]}
    bottom = {name for name, _ in ranked[q3_start:]}

    changes = []
    for signal, lift in ranked:
        prev = weights[signal]
        nxt = prev
        if signal in top:
            nxt = min(prev * BOOST_FACTOR, WEIGHT_CEILING)
        elif signal in bottom:
            nxt = max(prev * DECAY_FACTOR, WEIGHT_FLOOR)
        nxt = round(nxt, 3)
        if nxt != prev:
            changes.append({"signal": signal, "from": prev, "to": nxt, "lift": round(lift, 3)})
            weights[signal] = nxt

    now = time.time()
    data["weights"] = weights
    data["last_recalc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    data["last_recalc_ts"] = int(now)
    data["recalc_count"] = (data.get("recalc_count") or 0) + 1
    if changes:
        data.setdefault("history", []).append({
            "timestamp": data["last_recalc"],
            "changes": changes,
            "window_size": len(recent),
            "wins": len(wins),
            "losses": len(losses),
        })
        data["history"] = data["history"][-20:]
    save_weights(data)

    if not quiet:
        print(f"Recalculated from {len(recent)} closes ({len(wins)}W/{len(losses)}L): "
              f"{len(changes)} weight(s) adjusted")
        for c in changes:
            print(f"  {c['signal']}: {c['from']} -> {c['to']} (lift {c['lift']:+.3f})")
    return True


def main():
    parser = argparse.ArgumentParser(description="Recalculate darwinian signal weights")
    parser.add_argument("--quiet", action="store_true", help="suppress output (cron mode)")
    parser.add_argument("--force", action="store_true", help="ignore the recalc-interval guard")
    parser.add_argument("--show", action="store_true", help="print current weights and exit")
    cli = parser.parse_args()

    if cli.show:
        data = load_weights()
        print(json.dumps(data.get("weights") or {}, indent=2, sort_keys=True))
        print(f"last_recalc: {data.get('last_recalc') or 'never'}")
        return

    if not cli.force:
        last = load_weights().get("last_recalc_ts") or 0
        if time.time() - last < RECALC_GUARD_SECS:
            if not cli.quiet:
                print(f"Recalc guard: last run {int((time.time() - last) / 60)}m ago "
                      f"(interval {RECALC_GUARD_SECS // 3600}h). Use --force to override.")
            return

    recalculate(quiet=cli.quiet)


if __name__ == "__main__":
    main()
