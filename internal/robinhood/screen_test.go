package robinhood

import (
	"strings"
	"testing"
	"time"
)

func TestParseFeePct(t *testing.T) {
	cases := []struct {
		name string
		want float64
	}{
		{"CALLIE / WETH 0.3%", 0.3},
		{"NOXA / WETH 1%", 1},
		{"USDG / XIAO 87%", 87},
		{"NOFEE / WETH", 0},
		{"", 0},
	}
	for _, c := range cases {
		if got := parseFeePct(c.name); got != c.want {
			t.Errorf("parseFeePct(%q) = %v, want %v", c.name, got, c.want)
		}
	}
}

// passingPool returns a pool that clears every Fresh gate; each test case
// breaks exactly one gate from this baseline.
func passingPool(now time.Time) Pool {
	return Pool{
		Address:      "0xc187feb911997c06bc94903def113b677e6577c9",
		Name:         "CALLIE / WETH 1%",
		Dex:          "uniswap-v3-robinhood",
		CreatedAt:    now.Add(-2 * time.Hour),
		BaseAddress:  "0x21028be78e8f521214d24328715c1a8aadbac5a8",
		BaseSymbol:   "CALLIE",
		QuoteAddress: WETH,
		QuoteSymbol:  "WETH",
		FeePct:       1,
		ReserveUSD:   20000,
		FdvUSD:       300000,
		VolumeH1USD:  8000, // fee pace = 8000*24*1% / 20000 = 9.6%/day
		TxH1:         gtTxWindow{Buys: 40, Sells: 25, Buyers: 20, Sellers: 12},
		ChangeM5Pct:  1, ChangeH1Pct: 5, ChangeH6Pct: 10, ChangeH24Pct: 20,
	}
}

func TestScreenPasses(t *testing.T) {
	now := time.Now()
	cand, reason := Screen(passingPool(now), Fresh, now)
	if reason != "" {
		t.Fatalf("expected pass, got reject: %s", reason)
	}
	if cand.Chain != Chain || cand.Dex != "uniswap-v3" {
		t.Errorf("candidate venue fields wrong: chain=%q dex=%q", cand.Chain, cand.Dex)
	}
	if cand.FeeTVLDayPct < 9.5 || cand.FeeTVLDayPct > 9.7 {
		t.Errorf("fee pace = %v, want ~9.6", cand.FeeTVLDayPct)
	}
	if cand.Score <= 0 || cand.Score > 100 {
		t.Errorf("score out of range: %v", cand.Score)
	}
}

func TestScreenRejects(t *testing.T) {
	now := time.Now()
	cases := []struct {
		name   string
		mutate func(*Pool)
		want   string // reason prefix
	}{
		{"non-quote-asset quote", func(p *Pool) { p.QuoteAddress = "0x1111111111111111111111111111111111111111" }, "quote not"},
		{"v4 hooked pool", func(p *Pool) { p.Hook = "0x4e3468951D49f2EEa976eD0D6e75fFCb44a9a544" }, "v4 hooked"},
		{"v4 dynamic fee", func(p *Pool) { p.DynamicFee = true }, "v4 dynamic"},
		{"too young", func(p *Pool) { p.CreatedAt = now.Add(-1 * time.Minute) }, "too-young"},
		{"too old", func(p *Pool) { p.CreatedAt = now.Add(-30 * time.Hour) }, "too-old"},
		{"reserve floor", func(p *Pool) { p.ReserveUSD = 500 }, "reserve"},
		{"reserve cap", func(p *Pool) { p.ReserveUSD = 900000 }, "reserve"},
		{"fee tier floor", func(p *Pool) { p.FeePct = 0.05 }, "fee tier"},
		{"fee pace floor", func(p *Pool) { p.VolumeH1USD = 100 }, "fee/TVL pace"},
		{"txn floor", func(p *Pool) { p.TxH1 = gtTxWindow{Buys: 5, Sells: 5, Buyers: 20} }, "txns"},
		{"buyer floor", func(p *Pool) { p.TxH1 = gtTxWindow{Buys: 30, Sells: 10, Buyers: 3} }, "buyers"},
		{"no sells honeypot shape", func(p *Pool) { p.TxH1 = gtTxWindow{Buys: 40, Sells: 0, Buyers: 20} }, "no sells"},
		{"fdv floor", func(p *Pool) { p.FdvUSD = 1000 }, "fdv"},
		{"fdv cap", func(p *Pool) { p.FdvUSD = 90_000_000 }, "fdv"},
		{"m5 dump", func(p *Pool) { p.ChangeM5Pct = -8 }, "5m"},
		{"h1 dump", func(p *Pool) { p.ChangeH1Pct = -20 }, "1h"},
		{"h6 downtrend", func(p *Pool) { p.ChangeH6Pct = -15 }, "6h"},
		{"h24 downtrend", func(p *Pool) { p.ChangeH24Pct = -30 }, "24h"},
	}
	for _, c := range cases {
		p := passingPool(now)
		c.mutate(&p)
		cand, reason := Screen(p, Fresh, now)
		if reason == "" {
			t.Errorf("%s: expected reject, candidate passed (score %.0f)", c.name, cand.Score)
			continue
		}
		if !strings.HasPrefix(reason, c.want) {
			t.Errorf("%s: reason = %q, want prefix %q", c.name, reason, c.want)
		}
	}
}

// The venue's second and third quote assets: USDG pools and v4 native-ETH
// pools must pass the quote gate, and a quote asset arriving on the BASE side
// (GeckoTerminal lists USDG base-side in USDG/memecoin pools) must be
// re-oriented, not rejected.
func TestScreenQuoteAssets(t *testing.T) {
	now := time.Now()

	usdg := passingPool(now)
	usdg.Protocol = "v4"
	usdg.QuoteAddress, usdg.QuoteSymbol, usdg.QuoteDecimals = USDG, "USDG", 6
	cand, reason := Screen(usdg, Fresh, now)
	if reason != "" {
		t.Fatalf("USDG-quoted pool: expected pass, got reject: %s", reason)
	}
	if cand.Dex != "uniswap-v4" || cand.Protocol != "v4" {
		t.Errorf("v4 candidate fields wrong: dex=%q protocol=%q", cand.Dex, cand.Protocol)
	}

	native := passingPool(now)
	native.Protocol = "v4"
	native.QuoteAddress, native.QuoteSymbol = NativeETH, "ETH"
	if _, reason := Screen(native, Fresh, now); reason != "" {
		t.Errorf("native-ETH-quoted v4 pool: expected pass, got reject: %s", reason)
	}

	flipped := passingPool(now)
	flipped.Protocol = "v4"
	// USDG on the base side, memecoin on the quote side — the GT orientation
	// for USDG/memecoin pairs.
	flipped.BaseAddress, flipped.BaseSymbol, flipped.BaseDecimals = USDG, "USDG", 6
	flipped.QuoteAddress, flipped.QuoteSymbol, flipped.QuoteDecimals = "0x21028be78e8f521214d24328715c1a8aadbac5a8", "CALLIE", 18
	cand, reason = Screen(flipped, Fresh, now)
	if reason != "" {
		t.Fatalf("base-side USDG pool: expected pass, got reject: %s", reason)
	}
	if cand.QuoteSymbol != "USDG" || cand.BaseSymbol != "CALLIE" {
		t.Errorf("orientation not repaired: base=%q quote=%q", cand.BaseSymbol, cand.QuoteSymbol)
	}
}

// A v3 pool never sets Protocol today (only discover/mature constructors do);
// Screen must default it rather than emit an empty dex.
func TestScreenProtocolDefault(t *testing.T) {
	now := time.Now()
	cand, reason := Screen(passingPool(now), Fresh, now)
	if reason != "" {
		t.Fatal(reason)
	}
	if cand.Protocol != "v3" || cand.Dex != "uniswap-v3" {
		t.Errorf("protocol default wrong: dex=%q protocol=%q", cand.Dex, cand.Protocol)
	}
}

func TestSecurityReject(t *testing.T) {
	tax := func(v float64) *float64 { return &v }
	cases := []struct {
		name   string
		sec    *Security
		reject bool
	}{
		{"nil fails open", nil, false},
		{"all unknown fails open", &Security{Honeypot: -1, Blacklist: -1}, false},
		{"clean passes", &Security{Honeypot: 0, Blacklist: 0, SellTaxPct: tax(0)}, false},
		{"honeypot rejects", &Security{Honeypot: 1}, true},
		{"blacklist rejects", &Security{Blacklist: 1}, true},
		{"sell tax over cap rejects", &Security{SellTaxPct: tax(25)}, true},
		{"sell tax under cap passes", &Security{SellTaxPct: tax(5)}, false},
	}
	for _, c := range cases {
		if got := SecurityReject(c.sec) != ""; got != c.reject {
			t.Errorf("%s: reject = %v, want %v", c.name, got, c.reject)
		}
	}
}
