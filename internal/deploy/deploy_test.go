package deploy

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestDeployed(t *testing.T) {
	cases := []struct {
		out  string
		want bool
	}{
		{"🚀 DEPLOYED — 12:00 WIB\nFOO-SOL PosAddr", true},
		{"🧪 DRY RUN DEPLOY — 12:00 WIB\nFOO-SOL PosAddr", true},
		{"Aborting deploy: entry timing check rejected", false},
		{"", false},
	}
	for _, c := range cases {
		if got := Deployed(c.out); got != c.want {
			t.Errorf("Deployed(%q) = %v, want %v", c.out, got, c.want)
		}
	}
}

func TestSummarizeDeploy(t *testing.T) {
	out := "🔍 Starting\nBATCH_PICK: FOO (score 82.0) over [BAR]\n🚀 DEPLOYED — 12:00 WIB\nFOO-SOL PosAddr\nTX | https://solscan.io/tx/abc\n"
	got := Summarize(out, "casual")
	if !strings.HasPrefix(got, "[casual] 🚀 DEPLOYED") {
		t.Errorf("Summarize deploy = %q, want deploy block from marker", got)
	}
	if !strings.Contains(got, "TX | https://solscan.io/tx/abc") {
		t.Errorf("Summarize deploy = %q, want full block through TX line", got)
	}
}

func TestSummarizeReject(t *testing.T) {
	out := "🔍 Starting\nBATCH_PICK: FOO (score 82.0) over [BAR]\nAborting deploy: fee/TVL dropped 60% since screening\n"
	got := Summarize(out, "multiday")
	if !strings.Contains(got, "Aborting deploy: fee/TVL dropped 60% since screening") {
		t.Errorf("Summarize reject = %q, want decisive last line", got)
	}
	if !strings.Contains(got, "BATCH_PICK: FOO") {
		t.Errorf("Summarize reject = %q, want pick context line", got)
	}
}

func TestGateNarrative(t *testing.T) {
	out := "🔍 Starting\nBatch mode: 2 signalled candidate(s)\n" +
		"Batch reject FOO - global fees 3.2 SOL < 5 (bundled/scam pattern)\n" +
		"Batch conviction BAR: -25.0 (dev_close-25) -> score 60.0\n" +
		"No candidates survived batch conviction gates.\n"
	got := GateNarrative(out)
	if !strings.Contains(got, "Batch reject FOO - global fees 3.2 SOL < 5") {
		t.Errorf("GateNarrative = %q, want reject line", got)
	}
	if !strings.Contains(got, "Batch conviction BAR: -25.0") {
		t.Errorf("GateNarrative = %q, want conviction line", got)
	}
	if strings.Contains(got, "Batch mode:") || strings.Contains(got, "No candidates survived") {
		t.Errorf("GateNarrative = %q, must contain only gate lines", got)
	}
	if GateNarrative("🚀 DEPLOYED — 12:00 WIB\n") != "" {
		t.Error("GateNarrative without gate lines must be empty")
	}
}

func TestRunnerDisabled(t *testing.T) {
	r := New("", "", time.Second)
	if r.Enabled() {
		t.Error("empty DEPLOY_CMD must disable direct deploy")
	}
	if err := r.Report(context.Background(), "text"); err != nil {
		t.Errorf("Report with empty REPORT_CMD must be a no-op, got %v", err)
	}
}

func TestRunnerDeployAppendsArgs(t *testing.T) {
	r := New("/bin/echo -n", "", 5*time.Second)
	out, err := r.Deploy(context.Background(), []byte(`[{"pool":"P"}]`), "casual")
	if err != nil {
		t.Fatalf("Deploy via echo failed: %v (out=%q)", err, out)
	}
	want := `--from-batch [{"pool":"P"}] --mode casual`
	if !strings.Contains(out, want) {
		t.Errorf("Deploy args = %q, want substring %q", out, want)
	}
}

func TestRunnerDeployTimeout(t *testing.T) {
	// A stub that ignores the appended --from-batch/--mode args and hangs.
	stub := filepath.Join(t.TempDir(), "hang.sh")
	if err := os.WriteFile(stub, []byte("#!/bin/sh\nsleep 2\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	r := New(stub, "", 100*time.Millisecond)
	_, err := r.Deploy(context.Background(), []byte(`[]`), "casual")
	if err == nil || !strings.Contains(err.Error(), "timed out") {
		t.Errorf("Deploy timeout error = %v, want 'timed out'", err)
	}
}
