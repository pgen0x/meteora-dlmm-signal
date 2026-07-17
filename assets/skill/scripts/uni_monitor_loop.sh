#!/bin/bash
# uni_monitor_loop.sh — Continuous Robinhood Chain (Uniswap v3) position
# monitor. Runs the one-shot uni_monitor.py scan in a loop so the exit rules
# (SL/TP/trailing/fast-out/OOR) fire reliably, mirroring dlmm_monitor_loop.sh.
#
# Interval is longer than the Solana loop (20s): the venue holds few positions
# and every tick makes ~2 GeckoTerminal calls per position against the keyless
# ~4 req/min budget the discovery daemon already shares. 60s stays clear of it.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROFILE_DIR="$(dirname "$(dirname "$(dirname "$SCRIPT_DIR")")")"

echo "Starting Robinhood Chain (Uniswap v3) Position Monitor Loop (60s interval)..."

cd "$SCRIPT_DIR" || exit 1
while true; do
    # Re-source the profile .env EVERY tick, not once at startup. Sourcing it
    # outside the loop froze the process env at launch time: a later edit to
    # DRY_RUN (true -> false) stayed invisible to the running loop, which kept
    # printing "[dry-run] would close" while a position sat 22 points past its
    # emergency SL — exits live in the file, dead in the process. A trading loop
    # must never hold a stale copy of its own kill switch.
    set -a
    source "$PROFILE_DIR/.env" 2>/dev/null
    set +a

    python3 "$SCRIPT_DIR/uni_monitor.py"
    sleep 60
done
