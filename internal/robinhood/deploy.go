package robinhood

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"os/exec"
	"strings"
	"time"
)

// Runner shells out to uni_executor.js — the EVM sibling of internal/deploy's
// Runner, which drives dlmm_pipeline.py for the Solana venue. Unlike the
// Solana runner (one command, --from-batch does the picking), robinhood has
// no scoring pipeline of its own: the scanner already computes Score in
// screen.go, so this Runner just executes single, already-decided actions
// (deploy one pool, count open positions) — the picking happens in the
// caller (scanner.pollRobinhood), not here.
type Runner struct {
	execCmd []string
	timeout time.Duration
}

// New builds a Runner. execCmd is a whitespace-split command line, e.g.
// "node /home/ubuntu/.hermes/profiles/solanza/skills/solana-dlmm/scripts/uni_executor.js"
// ("" disables direct deploy; paths with spaces are not supported).
func New(execCmd string, timeout time.Duration) *Runner {
	return &Runner{execCmd: strings.Fields(execCmd), timeout: timeout}
}

// Enabled reports whether the executor command is configured.
func (r *Runner) Enabled() bool { return len(r.execCmd) > 0 }

func (r *Runner) run(ctx context.Context, args ...string) (string, error) {
	cctx, cancel := context.WithTimeout(ctx, r.timeout)
	defer cancel()
	cmd := exec.CommandContext(cctx, r.execCmd[0], append(append([]string{}, r.execCmd[1:]...), args...)...)
	var buf bytes.Buffer
	cmd.Stdout = &buf
	cmd.Stderr = &buf
	err := cmd.Run()
	if cctx.Err() == context.DeadlineExceeded {
		err = fmt.Errorf("uni_executor timed out after %v", r.timeout)
	}
	return buf.String(), err
}

// OpenPositions returns the wallet's current NonfungiblePositionManager
// position count, by running `uni_executor.js positions` and reading its
// {"count": N} JSON line. Callers MUST treat a non-nil error as "unknown" and
// fail closed (skip deploy) — there is no monitor yet to close stale
// positions, so under-counting risks an unbounded number of open positions.
func (r *Runner) OpenPositions(ctx context.Context) (int, error) {
	out, err := r.run(ctx, "positions")
	if err != nil {
		return 0, err
	}
	var d struct {
		Count int `json:"count"`
	}
	// The executor's last stdout line is the JSON payload; earlier lines (if
	// any) are transaction log noise from other commands, never `positions`.
	line := out
	if i := strings.LastIndex(strings.TrimSpace(out), "\n"); i >= 0 {
		line = strings.TrimSpace(out)[i+1:]
	}
	if err := json.Unmarshal([]byte(strings.TrimSpace(line)), &d); err != nil {
		return 0, fmt.Errorf("positions: unparseable output: %w", err)
	}
	return d.Count, nil
}

// Deploy mints one position via `uni_executor.js deploy` and returns its
// combined stdout+stderr. The error is non-nil only for execution failures
// (start error, timeout, non-zero exit / on-chain revert) — distinguishable
// from a clean run via Deployed().
func (r *Runner) Deploy(ctx context.Context, pool string, amountWeth, rangePct, slippagePct float64, strategy string) (string, error) {
	return r.run(ctx, "deploy",
		"--pool", pool,
		"--amount", fmt.Sprintf("%g", amountWeth),
		"--strategy", strategy,
		"--range-pct", fmt.Sprintf("%g", rangePct),
		"--slippage", fmt.Sprintf("%g", slippagePct),
	)
}

// Deployed reports whether executor output contains a successful (or
// dry-run) deploy marker — the same two markers uni_executor.js and
// dlmm_pipeline.py both print, so internal/deploy.Deployed's contract holds
// here too.
func Deployed(out string) bool {
	return strings.Contains(out, "🚀 DEPLOYED") || strings.Contains(out, "🧪 DRY RUN DEPLOY")
}

// Summarize condenses executor stdout into a short report line.
func Summarize(out string) string {
	for _, marker := range []string{"🚀 DEPLOYED", "🧪 DRY RUN DEPLOY"} {
		if i := strings.Index(out, marker); i >= 0 {
			return "[robinhood] " + strings.TrimSpace(out[i:])
		}
	}
	lines := strings.Split(strings.TrimSpace(out), "\n")
	last := ""
	if len(lines) > 0 {
		last = strings.TrimSpace(lines[len(lines)-1])
	}
	return fmt.Sprintf("[robinhood] ❌ %s", last)
}
