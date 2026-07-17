#!/bin/bash
# dlmm_monitor_loop.sh — Continuous DLMM position monitor (20s interval).
# Runs the one-shot dlmm_monitor.py scan in a loop so auto-close + auto-swap-to-SOL
# fire reliably instead of depending on ad-hoc gateway LLM invocations.

# Resolved from this file's own location (<profile>/skills/solana-dlmm/scripts/) so the
# script works whether it's a copy or a symlink into a Hermes profile — no install-time
# path rewrite needed. Uses logical `pwd` (no -P) so a symlinked scripts/ dir still
# resolves to the profile-side path, not the repo it points at.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROFILE_DIR="$(dirname "$(dirname "$(dirname "$SCRIPT_DIR")")")"

PYTHON="python3"

echo "Starting DLMM Position Monitor Loop (20s interval)..."

cd "$SCRIPT_DIR" || exit 1
# Sweep stranded tokens every SWEEP_EVERY iterations (30 × 20s ≈ 10min).
# On-close auto-swap only fires once; if it aborts on the impact guard the token is
# orphaned (no longer an active position), so this re-sweeps to liquidate it.
SWEEP_EVERY=30
i=0
STATS_STAMP="/tmp/dlmm_stats_last_sent"
while true; do
    # Re-source the profile .env EVERY tick, not once at startup. Sourcing it
    # outside the loop froze the process env at launch time, so a later edit to
    # DRY_RUN (true -> false) stayed invisible to the running loop — it kept
    # simulating closes while positions sat past their stop loss (this bit the
    # Robinhood loop for real: a -50% position was "[dry-run] would close"d for
    # 70 minutes). A trading loop must never hold a stale copy of its own kill
    # switch. Also makes DLMM_TZ / DLMM_STATS_HOUR live-editable.
    set -a
    source "$PROFILE_DIR/.env" 2>/dev/null
    set +a

    "$PYTHON" "$SCRIPT_DIR/dlmm_monitor.py"
    i=$((i + 1))
    if [ "$i" -ge "$SWEEP_EVERY" ]; then
        "$PYTHON" "$SCRIPT_DIR/dlmm_monitor.py" --cleanup-tokens
        i=0
    fi
    # Daily scoreboard — deterministic dlmm_stats.py card via `hermes send`
    # (zero LLM). Hour + timezone are operator-set (profile .env): DLMM_TZ
    # (IANA name, empty = system zone) and DLMM_STATS_HOUR (00-23, default 09).
    # Stamp file dedups to once per day.
    if [ -n "${DLMM_TZ:-}" ]; then
        hour_local=$(TZ="$DLMM_TZ" date +%H); today_local=$(TZ="$DLMM_TZ" date +%F)
    else
        hour_local=$(date +%H); today_local=$(date +%F)
    fi
    if [ "$hour_local" = "${DLMM_STATS_HOUR:-09}" ] && [ "$(cat "$STATS_STAMP" 2>/dev/null)" != "$today_local" ]; then
        "$PYTHON" "$SCRIPT_DIR/dlmm_stats.py" --send && echo "$today_local" > "$STATS_STAMP"
    fi
    sleep 20
done
