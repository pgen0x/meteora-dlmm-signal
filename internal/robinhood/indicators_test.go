package robinhood

import (
	"math"
	"testing"
)

// Fixture: deterministic sine+drift walk, 40 candles, generated once and run
// through the Python reference (local_indicators.py calculate_rsi period=7 and
// calculate_supertrend atr_period=10 multiplier=3.0). The expected values
// below are that run's output — the test pins the Go port to the upstream
// bit-for-bit (within float tolerance), the same parity contract screen.go
// keeps with dlmm_pipeline.py.
var fixtureCloses = []float64{
	99.7, 100.38959, 99.831884, 98.60813, 98.799542, 99.292045, 98.299249,
	97.396727, 97.941332, 98.036081, 96.799457, 96.364828, 97.039182,
	96.642645, 95.413545, 95.469895, 96.022926, 95.172511, 94.193794,
	94.63964, 94.856443, 93.708996, 93.151735, 93.790384, 93.547179,
	92.334727, 92.903497, 94.148802, 94.09389, 93.696509, 94.699276,
	95.693252, 95.274472, 95.242303, 96.527506, 97.096163, 96.515396,
	96.967179, 98.305897, 98.383774,
}

func fixture() candles {
	highs := make([]float64, len(fixtureCloses))
	lows := make([]float64, len(fixtureCloses))
	for i, c := range fixtureCloses {
		// Matches the generator: high = close*1.01, low = close*0.99, both
		// rounded to 6 decimals like the Python fixture.
		highs[i] = math.Round(c*1.01*1e6) / 1e6
		lows[i] = math.Round(c*0.99*1e6) / 1e6
	}
	return candles{highs: highs, lows: lows, closes: fixtureCloses}
}

func TestRSIParity(t *testing.T) {
	rsi := calculateRSI(fixtureCloses, 7)
	// Python reference values at warmup boundary, mid-series, and last.
	want := map[int]float64{
		6:  50.0, // pre-warmup default
		7:  27.19659473,
		20: 33.90074097,
		39: 76.59769759,
	}
	for i, w := range want {
		if math.Abs(rsi[i]-w) > 1e-6 {
			t.Errorf("rsi[%d] = %.8f, want %.8f", i, rsi[i], w)
		}
	}
}

func TestSupertrendParity(t *testing.T) {
	c := fixture()
	vals, bullish, breakUps, breakDowns := supertrend(c.highs, c.lows, c.closes, 10, 3.0)

	// The Python run flips bearish at 18 and back bullish at 38.
	wantBullish := func(i int) bool { return i < 18 || i >= 38 }
	for i := range bullish {
		if bullish[i] != wantBullish(i) {
			t.Errorf("bullish[%d] = %v, want %v", i, bullish[i], wantBullish(i))
		}
	}
	if !breakDowns[18] {
		t.Error("breakDowns[18] = false, want true (bullish->bearish flip)")
	}
	if !breakUps[38] {
		t.Error("breakUps[38] = false, want true (bearish->bullish flip)")
	}
	wantVals := map[int]float64{
		18: 100.13705444,
		25: 98.23467364,
		38: 92.29841314,
		39: 92.38673573,
	}
	for i, w := range wantVals {
		if math.Abs(vals[i]-w) > 1e-6 {
			t.Errorf("vals[%d] = %.8f, want %.8f", i, vals[i], w)
		}
	}
}

func TestEntryConfirm(t *testing.T) {
	c := fixture()

	// Full series ends bullish with close above the line — entry confirmed.
	confirmed, detail := entryConfirm(c)
	if !confirmed {
		t.Errorf("entryConfirm(full) = false (%s), want true", detail)
	}

	// Truncate to the bearish stretch (index 29: bearish trend, RSI 43.7 —
	// neither leg of supertrend_or_rsi fires): entry rejected. This is the
	// FOX case — a token in decline must not confirm.
	bear := candles{highs: c.highs[:30], lows: c.lows[:30], closes: c.closes[:30]}
	confirmed, detail = entryConfirm(bear)
	if confirmed {
		t.Errorf("entryConfirm(bearish) = true (%s), want false", detail)
	}
}

func TestSupertrendShortSeries(t *testing.T) {
	// Below the ATR period the indicator defaults to all-bullish with no
	// flips — mirrors the Python n < atr_period early return.
	vals, bullish, ups, downs := supertrend([]float64{1, 2}, []float64{1, 2}, []float64{1, 2}, 10, 3.0)
	if len(vals) != 2 || !bullish[0] || !bullish[1] || ups[1] || downs[1] {
		t.Errorf("short series: vals=%v bullish=%v ups=%v downs=%v", vals, bullish, ups, downs)
	}
}
