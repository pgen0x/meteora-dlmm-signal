// Package deploy executes the deterministic picker pipeline directly instead
// of forwarding signal batches to the Hermes agent webhook. The LLM agent's
// compare-and-pick step took 19-54 minutes per decision (gateway queue + model
// latency), long enough for a pool's yield to decay past the pipeline's own
// fee/TVL freshness gate. Running dlmm_pipeline.py --from-batch as a
// subprocess makes the same decision in seconds with reproducible rules.
package deploy

import (
	"bytes"
	"context"
	"fmt"
	"os/exec"
	"strings"
	"time"
)

// Runner shells out to the picker pipeline and, optionally, a report-delivery
// command (e.g. `hermes send -t telegram`, which reuses the gateway's platform
// credentials without an LLM turn).
type Runner struct {
	deployCmd []string
	reportCmd []string
	timeout   time.Duration
}

// New builds a Runner. deployCmd/reportCmd are whitespace-split command lines
// ("" disables the respective step); paths with spaces are not supported.
func New(deployCmd, reportCmd string, timeout time.Duration) *Runner {
	return &Runner{
		deployCmd: strings.Fields(deployCmd),
		reportCmd: strings.Fields(reportCmd),
		timeout:   timeout,
	}
}

// Enabled reports whether direct deploy mode is configured (DEPLOY_CMD set).
func (r *Runner) Enabled() bool { return len(r.deployCmd) > 0 }

// Deploy runs the pipeline on one signal batch and returns its combined
// stdout+stderr. The error is non-nil only for execution failures — start
// error, timeout, or non-zero exit (the pipeline exits 1 on hard failures
// like a failed on-chain deploy). A deterministic REJECT exits 0 and is a
// normal outcome, distinguishable via Deployed().
func (r *Runner) Deploy(ctx context.Context, batchJSON []byte, mode string) (string, error) {
	cctx, cancel := context.WithTimeout(ctx, r.timeout)
	defer cancel()

	args := append(append([]string{}, r.deployCmd[1:]...),
		"--from-batch", string(batchJSON), "--mode", mode)
	cmd := exec.CommandContext(cctx, r.deployCmd[0], args...)
	var buf bytes.Buffer
	cmd.Stdout = &buf
	cmd.Stderr = &buf
	err := cmd.Run()
	if cctx.Err() == context.DeadlineExceeded {
		err = fmt.Errorf("pipeline timed out after %v", r.timeout)
	}
	return buf.String(), err
}

// Deployed reports whether pipeline output contains a successful (or dry-run)
// deploy marker. These exact markers are the pipeline's success contract —
// the same strings the agent prompt was required to verify in stdout.
func Deployed(out string) bool {
	return strings.Contains(out, "🚀 DEPLOYED") || strings.Contains(out, "🧪 DRY RUN DEPLOY")
}

// Summarize condenses pipeline stdout into a short report suitable for chat
// delivery: the full deploy report block when one exists, otherwise the pick
// line plus the decisive reject line.
func Summarize(out, mode string) string {
	for _, marker := range []string{"🚀 DEPLOYED", "🧪 DRY RUN DEPLOY"} {
		if i := strings.Index(out, marker); i >= 0 {
			return fmt.Sprintf("[%s] %s", mode, strings.TrimSpace(out[i:]))
		}
	}
	var pick, last string
	for _, line := range strings.Split(out, "\n") {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		if strings.HasPrefix(line, "BATCH_PICK:") {
			pick = line
		}
		last = line
	}
	if pick != "" && pick != last {
		return fmt.Sprintf("[%s] ❌ %s\n%s", mode, last, pick)
	}
	return fmt.Sprintf("[%s] ❌ %s", mode, last)
}

// GateNarrative extracts the pipeline's per-candidate conviction lines
// ("Batch reject <name> - <reason>", "Batch conviction <name>: <adj>") so the
// scanner can journal WHY a batch died. Summarize drops these — it keeps only
// the last line for chat delivery — which left reject reasons invisible
// (2026-07-12: five silent hours before anyone could see the decisive gate).
func GateNarrative(out string) string {
	var lines []string
	for _, line := range strings.Split(out, "\n") {
		line = strings.TrimSpace(line)
		if strings.HasPrefix(line, "Batch reject ") || strings.HasPrefix(line, "Batch conviction ") {
			lines = append(lines, line)
		}
	}
	return strings.Join(lines, "\n")
}

// Report pipes text to the report command's stdin. Best-effort: delivery is
// observability, never a reason to fail or retry a deploy.
func (r *Runner) Report(ctx context.Context, text string) error {
	if len(r.reportCmd) == 0 {
		return nil
	}
	cctx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	cmd := exec.CommandContext(cctx, r.reportCmd[0], r.reportCmd[1:]...)
	cmd.Stdin = strings.NewReader(text)
	return cmd.Run()
}
