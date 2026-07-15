package robinhood

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"time"
)

// This file is a verbatim port of the entry-timing half of the skill's
// local_indicators.py (calculate_rsi, calculate_atr, calculate_supertrend and
// the "supertrend_or_rsi" entry preset) for the Robinhood Chain venue — the
// deploy pick happens in the Go scanner here, not in dlmm_pipeline.py, so the
// gate has to live on this side. When changing periods, multipliers or preset
// logic, keep them in sync with that upstream, same as screen.go's contract
// with dlmm_pipeline.py. Exit-side confirmation stays in Python
// (uni_monitor.py imports local_indicators directly).

var ohlcvClient = &http.Client{Timeout: 15 * time.Second}

// ohlcvURL mirrors local_indicators.fetch_ohlcv_candles' 24h-timeframe shape:
// 15-minute candles from GeckoTerminal (default page = 100 candles ≈ 25h).
const ohlcvURL = "https://api.geckoterminal.com/api/v2/networks/robinhood/pools/%s/ohlcv/minute?aggregate=15"

// minCandles is check_local_indicators' history floor: below it the
// indicators are warmup noise (ATR-10 + Supertrend need a settled band), so
// the check reports "data unavailable" and the caller fails open.
const minCandles = 30

type candles struct {
	highs, lows, closes []float64
}

// fetchOHLCV retrieves the pool's 15m candles, oldest-first (GeckoTerminal
// serves newest-first; reversed here exactly like the Python fetch).
func fetchOHLCV(pool string) (candles, error) {
	req, err := http.NewRequest(http.MethodGet, fmt.Sprintf(ohlcvURL, pool), nil)
	if err != nil {
		return candles{}, err
	}
	req.Header.Set("Accept", "application/json")
	resp, err := ohlcvClient.Do(req)
	if err != nil {
		return candles{}, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		io.Copy(io.Discard, io.LimitReader(resp.Body, 512))
		return candles{}, fmt.Errorf("geckoterminal ohlcv status %d", resp.StatusCode)
	}
	var d struct {
		Data struct {
			Attributes struct {
				// [timestamp, open, high, low, close, volume]
				List [][6]float64 `json:"ohlcv_list"`
			} `json:"attributes"`
		} `json:"data"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&d); err != nil {
		return candles{}, fmt.Errorf("geckoterminal ohlcv decode: %w", err)
	}
	list := d.Data.Attributes.List
	c := candles{
		highs:  make([]float64, len(list)),
		lows:   make([]float64, len(list)),
		closes: make([]float64, len(list)),
	}
	for i, row := range list {
		j := len(list) - 1 - i // newest-first -> oldest-first
		c.highs[j], c.lows[j], c.closes[j] = row[2], row[3], row[4]
	}
	return c, nil
}

// calculateRSI is Wilder-smoothed RSI — port of calculate_rsi.
func calculateRSI(closes []float64, period int) []float64 {
	n := len(closes)
	rsi := make([]float64, n)
	for i := range rsi {
		rsi[i] = 50.0
	}
	if n < period+1 {
		return rsi
	}
	gains := make([]float64, n-1)
	losses := make([]float64, n-1)
	for i := 1; i < n; i++ {
		diff := closes[i] - closes[i-1]
		if diff > 0 {
			gains[i-1] = diff
		} else {
			losses[i-1] = -diff
		}
	}
	var avgGain, avgLoss float64
	for i := 0; i < period; i++ {
		avgGain += gains[i]
		avgLoss += losses[i]
	}
	avgGain /= float64(period)
	avgLoss /= float64(period)
	rs := 100.0
	if avgLoss != 0 {
		rs = avgGain / avgLoss
	}
	rsi[period] = 100.0 - 100.0/(1.0+rs)
	for i := period + 1; i < n; i++ {
		avgGain = (avgGain*float64(period-1) + gains[i-1]) / float64(period)
		avgLoss = (avgLoss*float64(period-1) + losses[i-1]) / float64(period)
		rs = 100.0
		if avgLoss != 0 {
			rs = avgGain / avgLoss
		}
		rsi[i] = 100.0 - 100.0/(1.0+rs)
	}
	return rsi
}

// calculateATR is Wilder-smoothed Average True Range — port of calculate_atr.
func calculateATR(highs, lows, closes []float64, period int) []float64 {
	n := len(closes)
	atr := make([]float64, n)
	if n < 2 {
		return atr
	}
	tr := make([]float64, n)
	tr[0] = highs[0] - lows[0]
	for i := 1; i < n; i++ {
		hl := highs[i] - lows[i]
		hc := abs(highs[i] - closes[i-1])
		lc := abs(lows[i] - closes[i-1])
		tr[i] = max3(hl, hc, lc)
	}
	if n < period {
		// SMA fallback for a too-short series (the Python sma(tr, n) path).
		var sum float64
		for i := 0; i < n; i++ {
			sum += tr[i]
			atr[i] = sum / float64(i+1)
		}
		return atr
	}
	var sum float64
	for i := 0; i < period; i++ {
		sum += tr[i]
	}
	atr[period-1] = sum / float64(period)
	for i := period; i < n; i++ {
		atr[i] = (atr[i-1]*float64(period-1) + tr[i]) / float64(period)
	}
	// Warmup fill before the first full period (expanding mean, like Python).
	sum = 0
	for i := 0; i < period-1; i++ {
		sum += tr[i]
		atr[i] = sum / float64(i+1)
	}
	return atr
}

// supertrend ports calculate_supertrend: returns per-candle indicator values,
// bullish flags, and trend-flip flags (break up / break down).
func supertrend(highs, lows, closes []float64, atrPeriod int, multiplier float64) (vals []float64, bullish, breakUps, breakDowns []bool) {
	n := len(closes)
	vals = make([]float64, n)
	bullish = make([]bool, n)
	breakUps = make([]bool, n)
	breakDowns = make([]bool, n)
	if n < atrPeriod {
		for i := range bullish {
			bullish[i] = true
		}
		return
	}
	atr := calculateATR(highs, lows, closes, atrPeriod)
	basicUpper := make([]float64, n)
	basicLower := make([]float64, n)
	for i := 0; i < n; i++ {
		hl2 := (highs[i] + lows[i]) / 2.0
		basicUpper[i] = hl2 + multiplier*atr[i]
		basicLower[i] = hl2 - multiplier*atr[i]
	}
	finalUpper := make([]float64, n)
	finalLower := make([]float64, n)
	trend := make([]int, n) // 1 bullish, -1 bearish
	for i := range trend {
		trend[i] = 1
	}
	finalUpper[0] = basicUpper[0]
	finalLower[0] = basicLower[0]
	for i := 1; i < n; i++ {
		if basicLower[i] > finalLower[i-1] || closes[i-1] < finalLower[i-1] {
			finalLower[i] = basicLower[i]
		} else {
			finalLower[i] = finalLower[i-1]
		}
		if basicUpper[i] < finalUpper[i-1] || closes[i-1] > finalUpper[i-1] {
			finalUpper[i] = basicUpper[i]
		} else {
			finalUpper[i] = finalUpper[i-1]
		}
		switch {
		case closes[i] > finalUpper[i-1]:
			trend[i] = 1
		case closes[i] < finalLower[i-1]:
			trend[i] = -1
		default:
			trend[i] = trend[i-1]
			if trend[i] == 1 && finalLower[i] < finalLower[i-1] {
				finalLower[i] = finalLower[i-1]
			}
			if trend[i] == -1 && finalUpper[i] > finalUpper[i-1] {
				finalUpper[i] = finalUpper[i-1]
			}
		}
	}
	for i := 0; i < n; i++ {
		if trend[i] == 1 {
			vals[i] = finalLower[i]
			bullish[i] = true
		} else {
			vals[i] = finalUpper[i]
		}
		if i > 0 {
			breakUps[i] = trend[i] == 1 && trend[i-1] == -1
			breakDowns[i] = trend[i] == -1 && trend[i-1] == 1
		}
	}
	return
}

// entryConfirm applies the "supertrend_or_rsi" entry preset to a candle set:
// a fresh bullish flip, an established bullish trend holding above the line,
// or an RSI-7 oversold reading (<= 30) confirms the entry.
func entryConfirm(c candles) (confirmed bool, detail string) {
	i := len(c.closes) - 1
	rsi := calculateRSI(c.closes, 7)
	vals, bull, breakUps, _ := supertrend(c.highs, c.lows, c.closes, 10, 3.0)
	dir := "bearish"
	if bull[i] {
		dir = "bullish"
	}
	confirmed = breakUps[i] || (bull[i] && c.closes[i] >= vals[i]) || rsi[i] <= 30
	return confirmed, fmt.Sprintf("ST %s, RSI %.1f", dir, rsi[i])
}

// EntryTimingConfirm runs the supertrend_or_rsi entry check against the
// pool's live 15m candles. ok=false means the data was unavailable or too
// thin to judge — callers MUST fail open there (proceed on the other gates),
// matching check_local_indicators' None contract. It is best-effort by
// design: this venue's pools are young and GeckoTerminal rate-limits, so
// failing closed would silence the deploy path entirely.
func EntryTimingConfirm(pool string) (confirmed bool, detail string, ok bool) {
	c, err := fetchOHLCV(pool)
	if err != nil {
		return false, err.Error(), false
	}
	if len(c.closes) < minCandles {
		return false, fmt.Sprintf("only %d candles < %d", len(c.closes), minCandles), false
	}
	confirmed, detail = entryConfirm(c)
	return confirmed, detail, true
}

func abs(x float64) float64 {
	if x < 0 {
		return -x
	}
	return x
}

func max3(a, b, c float64) float64 {
	m := a
	if b > m {
		m = b
	}
	if c > m {
		m = c
	}
	return m
}
