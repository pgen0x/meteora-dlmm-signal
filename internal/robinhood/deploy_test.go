package robinhood

import (
	"math"
	"strings"
	"testing"
)

// defaults mirrors config.go's ROBINHOOD_DEPLOY_* defaults.
func defaults() SizeParams {
	return SizeParams{Reserve: 0.002, Pct: 0.45, Floor: 0.003, Ceil: 0.05}
}

func TestComputeDeployAmount(t *testing.T) {
	tests := []struct {
		name    string
		balance float64
		params  SizeParams
		want    float64
	}{
		{
			// The live wallet that motivated this: the old fixed 0.003 WETH size
			// was deploying ~17% of it. (0.017410 - 0.002) * 0.45 = 0.0069345.
			name:    "live wallet sizes up from the old fixed 0.003",
			balance: 0.017410,
			params:  defaults(),
			want:    0.0069345,
		},
		{
			// Percentage of the REMAINING balance, so exposure tapers as
			// positions stack instead of marching to a zero balance. The reserve
			// is subtracted every time, not just on the first deploy:
			// (0.0104755 - 0.002) * 0.45 = 0.003813975.
			name:    "second position is smaller than the first",
			balance: 0.017410 - 0.0069345,
			params:  defaults(),
			want:    0.003813975,
		},
		{
			// Above the floor in total, but the 45% haircut lands under it —
			// deploy exactly the floor rather than dust.
			name:    "haircut below floor clamps up to floor",
			balance: 0.008,
			params:  defaults(),
			want:    0.003,
		},
		{
			// Deployable = 0.003, exactly the floor -> fundable, deploys floor.
			name:    "exactly floor-fundable deploys the floor",
			balance: 0.005,
			params:  defaults(),
			want:    0.003,
		},
		{
			// 0.004 - 0.002 = 0.002 deployable < 0.003 floor -> skip entirely.
			// 0 means SKIP, never "deploy nothing".
			name:    "below floor after reserve returns 0 (skip)",
			balance: 0.004,
			params:  defaults(),
			want:    0,
		},
		{
			name:    "empty wallet skips",
			balance: 0,
			params:  defaults(),
			want:    0,
		},
		{
			name:    "balance under reserve skips (no negative size)",
			balance: 0.001,
			params:  defaults(),
			want:    0,
		},
		{
			// Scales with the wallet without a config edit — until the ceil.
			// (1.0 - 0.002) * 0.45 = 0.4491, capped to 0.05.
			name:    "whale wallet clamps to ceil",
			balance: 1.0,
			params:  defaults(),
			want:    0.05,
		},
		{
			name:    "zero ceil means uncapped",
			balance: 1.0,
			params:  SizeParams{Reserve: 0.002, Pct: 0.45, Floor: 0.003, Ceil: 0},
			want:    0.4491,
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := ComputeDeployAmount(tt.balance, tt.params)
			if math.Abs(got-tt.want) > 1e-9 {
				t.Errorf("ComputeDeployAmount(%v) = %v, want %v", tt.balance, got, tt.want)
			}
		})
	}
}

// A deploy must never spend more than the wallet holds, nor dip into the
// reserve — the two properties that turn a sizing bug into a drained wallet.
func TestComputeDeployAmountNeverOverspends(t *testing.T) {
	p := defaults()
	for _, bal := range []float64{0, 0.001, 0.002, 0.003, 0.005, 0.0174, 0.1, 1, 100} {
		got := ComputeDeployAmount(bal, p)
		if got < 0 {
			t.Fatalf("balance %v: negative size %v", bal, got)
		}
		if got > bal {
			t.Fatalf("balance %v: size %v exceeds balance", bal, got)
		}
		if got > 0 && got > bal-p.Reserve {
			t.Fatalf("balance %v: size %v eats into the %v reserve", bal, got, p.Reserve)
		}
	}
}

// The executor prints ether amounts as decimal STRINGS (viem formatEther), and
// its JSON payload is the LAST stdout line, after any tx-log noise.
func TestLastLineStripsLogNoise(t *testing.T) {
	out := "some tx log noise\n" +
		`{"address":"0xABCDEF0000000000000000000000000000000000","eth":"0.00084617594819808","weth":"0.017409921705255751"}`
	line := lastLine(out)
	if line == "" || line[0] != '{' {
		t.Fatalf("lastLine did not strip log noise: %q", line)
	}
	if got := lastLine("  single line  "); got != "single line" {
		t.Fatalf("lastLine single-line = %q", got)
	}
}

// A mint that reverts without opening a position exits 0 (the executor sells the
// swap leg back to WETH and reports it), so Summarize must recognise its marker
// line instead of falling through and reporting the raw JSON result line.
func TestSummarizeReportsFailedDeployInWords(t *testing.T) {
	out := "swap 0.0015 WETH -> token: 0xabc\n" +
		"mint failed (no position opened): Price slippage check\n" +
		"❌ DEPLOY FAILED (no position opened): Price slippage check, refunded 0.00148 WETH\n" +
		`{"success":false,"error":"mint failed: Price slippage check","pool":"0xdead"}`

	got := Summarize(out)
	if strings.Contains(got, "{") {
		t.Fatalf("Summarize leaked the JSON result line: %q", got)
	}
	if !strings.Contains(got, "DEPLOY FAILED") || !strings.Contains(got, "refunded 0.00148 WETH") {
		t.Fatalf("Summarize dropped the failure detail: %q", got)
	}
	if Deployed(out) {
		t.Fatal("Deployed() must be false when no position was opened")
	}
}
