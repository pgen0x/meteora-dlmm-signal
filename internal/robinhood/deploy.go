package robinhood

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"os/exec"
	"strconv"
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
	if err := json.Unmarshal([]byte(lastLine(out)), &d); err != nil {
		return 0, fmt.Errorf("positions: unparseable output: %w", err)
	}
	return d.Count, nil
}

// Balances is the wallet's spendable capital. ETH and WETH are NOT
// interchangeable here: ETH is native gas, WETH is an LP quote asset. That is
// this venue's one structural divergence from Solana, where SOL is both — so
// dlmm_pipeline.compute_deploy_amount's single `reserve` becomes two guards:
// a WETH reserve (below) and a native-gas floor (checked by the caller).
// USDG (dollar units, the token's 6 decimals already applied) is the v4
// venue's second quote asset; the v3 executor doesn't report it and the
// field stays 0 there.
type Balances struct {
	ETH  float64
	WETH float64
	USDG float64
}

// Balance reads the wallet via `uni_executor.js balance`. Like OpenPositions,
// callers MUST fail closed on error: sizing a deploy off an unknown balance
// would either overspend or mint a dust position.
func (r *Runner) Balance(ctx context.Context) (Balances, error) {
	out, err := r.run(ctx, "balance")
	if err != nil {
		return Balances{}, err
	}
	// The executor emits balances as decimal STRINGS (viem formatEther /
	// formatUnits output), not JSON numbers — float64 fields here would fail
	// to unmarshal. `usdg` only exists in the v4 executor's output; absent
	// (v3) parses as 0, which is correct — that wallet flow holds no USDG.
	var d struct {
		ETH  string `json:"eth"`
		WETH string `json:"weth"`
		USDG string `json:"usdg"`
	}
	if err := json.Unmarshal([]byte(lastLine(out)), &d); err != nil {
		return Balances{}, fmt.Errorf("balance: unparseable output: %w", err)
	}
	eth, err := strconv.ParseFloat(d.ETH, 64)
	if err != nil {
		return Balances{}, fmt.Errorf("balance: bad eth %q: %w", d.ETH, err)
	}
	weth, err := strconv.ParseFloat(d.WETH, 64)
	if err != nil {
		return Balances{}, fmt.Errorf("balance: bad weth %q: %w", d.WETH, err)
	}
	var usdg float64
	if d.USDG != "" {
		if usdg, err = strconv.ParseFloat(d.USDG, 64); err != nil {
			return Balances{}, fmt.Errorf("balance: bad usdg %q: %w", d.USDG, err)
		}
	}
	return Balances{ETH: eth, WETH: weth, USDG: usdg}, nil
}

// SizeParams configures ComputeDeployAmount — the quote-asset analogues of
// the Solana pipeline's compute_deploy_amount constants. Field units are the
// QUOTE ASSET the params are configured for (WETH for the v3/ETH-quoted set,
// USDG dollars for the USDG set) — one struct, one balance, one unit.
type SizeParams struct {
	Reserve float64 // held back, never deployed
	Pct     float64 // fraction of the deployable balance per position
	Floor   float64 // smallest position worth its gas + round-trip swap cost
	Ceil    float64 // hard cap per position (0 = uncapped)
}

// ComputeDeployAmount sizes one position from the live quote-asset balance —
// the port of dlmm_pipeline.py's compute_deploy_amount (reserve 0.2 SOL,
// pct 0.45, floor 0.3, ceil 5.0).
//
// Taking a PERCENTAGE of the remaining balance rather than a fixed size is the
// whole point: each open position shrinks the base for the next one, so total
// exposure tapers as positions stack instead of marching linearly to a zero
// balance — and it scales up as the wallet grows, with no config edit. A fixed
// 0.003 WETH size was deploying only ~17% of a 0.0174 WETH wallet.
//
// Returns 0 to mean SKIP THIS DEPLOY — callers must treat it as a skip, never
// as "deploy nothing". A sub-floor position isn't worth its gas and swap costs,
// so it is declined outright rather than minted as dust.
func ComputeDeployAmount(quoteBalance float64, p SizeParams) float64 {
	deployable := quoteBalance - p.Reserve
	if deployable < p.Floor {
		return 0 // can't even fund a floor-sized position — skip
	}
	amount := deployable * p.Pct
	if amount < p.Floor {
		// Affordable in total, but below the floor after the percentage haircut:
		// deploy exactly the floor (the check above proved we can cover it).
		amount = p.Floor
	}
	if p.Ceil > 0 && amount > p.Ceil {
		amount = p.Ceil
	}
	return amount
}

// lastLine returns the final non-empty stdout line — the executor's JSON
// payload, after any transaction-log noise.
func lastLine(out string) string {
	t := strings.TrimSpace(out)
	if i := strings.LastIndex(t, "\n"); i >= 0 {
		return strings.TrimSpace(t[i+1:])
	}
	return t
}

// Deploy mints one position via the executor's `deploy` command and returns
// its combined stdout+stderr. The error is non-nil only for execution
// failures (start error, timeout, non-zero exit / on-chain revert) —
// distinguishable from a clean run via Deployed(). `amount` is in quote
// units. `quote` is the quote-side asset address for a v4 executor (which
// side of the PoolKey to size and settle in); empty omits the flag — the v3
// executor is WETH-only and doesn't know it.
func (r *Runner) Deploy(ctx context.Context, pool string, amount, rangePct, slippagePct float64, strategy, quote string) (string, error) {
	args := []string{"deploy",
		"--pool", pool,
		"--amount", fmt.Sprintf("%g", amount),
		"--strategy", strategy,
		"--range-pct", fmt.Sprintf("%g", rangePct),
		"--slippage", fmt.Sprintf("%g", slippagePct),
	}
	if quote != "" {
		args = append(args, "--quote", quote)
	}
	return r.run(ctx, args...)
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
	// "❌ DEPLOY FAILED" is the executor's own marker for a mint that reverted
	// without opening anything — a two-sided strategy whose fill came back
	// one-sided — and whose swap leg it has already sold back to WETH. That exits
	// 0 because nothing is broken, so it needs a marker of its own; without one
	// the fallback below would report its raw JSON result line.
	for _, marker := range []string{"🚀 DEPLOYED", "🧪 DRY RUN DEPLOY", "❌ DEPLOY FAILED"} {
		if i := strings.Index(out, marker); i >= 0 {
			// Only the marker line — the executor prints a raw JSON result line
			// right after it, which must not leak into the Telegram report.
			line := out[i:]
			if nl := strings.IndexByte(line, '\n'); nl >= 0 {
				line = line[:nl]
			}
			return "[robinhood] " + strings.TrimSpace(line)
		}
	}
	lines := strings.Split(strings.TrimSpace(out), "\n")
	last := ""
	if len(lines) > 0 {
		last = strings.TrimSpace(lines[len(lines)-1])
	}
	return fmt.Sprintf("[robinhood] ❌ %s", last)
}
