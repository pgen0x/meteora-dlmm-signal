package robinhood

import (
	"strings"
	"testing"
	"time"
)

// maturePool returns a pool that clears every Mature gate; each test case
// breaks exactly one gate from this baseline. Modeled on the live MEOW/WETH
// pool that passed on 2026-07-14 (the only survivor of that cycle's shortlist).
func maturePool(now time.Time) Pool {
	return Pool{
		Address:      "0x6f0a2d3c7f5e1b9a8c4d2e0f1a3b5c7d9e1f2a4b",
		Name:         "MEOW / WETH 1%",
		Dex:          "uniswap-v3",
		CreatedAt:    now.Add(-66 * time.Hour),
		BaseAddress:  "0x21028be78e8f521214d24328715c1a8aadbac5a8",
		BaseSymbol:   "MEOW",
		QuoteAddress: WETH,
		QuoteSymbol:  "WETH",
		FeePct:       1,
		ReserveUSD:   82000,
		FdvUSD:       655000,
		VolumeH1USD:  30000,  // deliberately NOT the source of the pace under FeePaceH24
		VolumeH24USD: 740000, // fee pace = 740000*1% / 82000 = 9.0%/day
		TxH1:         gtTxWindow{Buys: 70, Sells: 40, Buyers: 46, Sellers: 30},
		ChangeM5Pct:  1, ChangeH1Pct: 3, ChangeH6Pct: 5, ChangeH24Pct: 10,
	}
}

func TestScreenMaturePasses(t *testing.T) {
	now := time.Now()
	cand, reason := Screen(maturePool(now), Mature, now)
	if reason != "" {
		t.Fatalf("expected pass, got reject: %s", reason)
	}
	if cand.Mode != "rh-mature" {
		t.Errorf("mode = %q, want rh-mature", cand.Mode)
	}
	// 9.0%/day comes from the 24h volume. Had Screen extrapolated the h1 window
	// (30000*24*1%/82000) it would read ~8.8x higher — the whole point of
	// FeePaceH24, so pin it tightly.
	if cand.FeeTVLDayPct < 8.9 || cand.FeeTVLDayPct > 9.1 {
		t.Errorf("fee pace = %v, want ~9.0 (from 24h volume, not the h1 extrapolation)", cand.FeeTVLDayPct)
	}
}

// A pool with one spiky hour but weak realized 24h volume is exactly what the
// mature thesis must not buy. Under Fresh's h1 extrapolation it looks like a
// monster; under FeePaceH24 it fails the pace gate.
func TestMatureIgnoresSpikyHour(t *testing.T) {
	now := time.Now()
	p := maturePool(now)
	p.VolumeH1USD = 200000 // h1 extrapolation => 200000*24*1%/82000 = 58%/day
	p.VolumeH24USD = 40000 // realized        => 40000*1%/82000     = 0.49%/day

	if _, reason := Screen(p, Mature, now); !strings.HasPrefix(reason, "fee/TVL pace") {
		t.Errorf("spiky hour should fail the mature pace gate, got reject=%q", reason)
	}
	// The same pool under an h1-pace mode sails through the pace gate — proving
	// the flag, not the thresholds, is what rejects it.
	h1Mode := Mature
	h1Mode.FeePaceH24 = false
	if _, reason := Screen(p, h1Mode, now); strings.HasPrefix(reason, "fee/TVL pace") {
		t.Errorf("under h1 pacing the same pool should clear the pace gate, got %q", reason)
	}
}

// MaxAge 0 must mean "no ceiling", not "reject everything". Mature relies on it:
// a pool printing fees for a month is more proven, not less.
func TestMatureHasNoAgeCeiling(t *testing.T) {
	now := time.Now()
	p := maturePool(now)
	p.CreatedAt = now.Add(-30 * 24 * time.Hour)

	if _, reason := Screen(p, Mature, now); reason != "" {
		t.Errorf("30-day-old pool should pass MaxAge=0, got reject: %s", reason)
	}
}

func TestScreenMatureRejects(t *testing.T) {
	now := time.Now()
	cases := []struct {
		name   string
		mutate func(*Pool)
		want   string
	}{
		// The age floor is what partitions the venue: anything younger belongs
		// to Fresh, and a pool must never signal in both modes.
		{"younger than fresh window", func(p *Pool) { p.CreatedAt = now.Add(-12 * time.Hour) }, "too-young"},
		{"reserve floor", func(p *Pool) { p.ReserveUSD = 9000 }, "reserve"},
		{"reserve cap", func(p *Pool) { p.ReserveUSD = 900000 }, "reserve"},
		{"fee pace floor", func(p *Pool) { p.VolumeH24USD = 40000 }, "fee/TVL pace"},
		{"txn floor", func(p *Pool) { p.TxH1 = gtTxWindow{Buys: 30, Sells: 20, Buyers: 46} }, "txns"},
		{"buyer floor", func(p *Pool) { p.TxH1 = gtTxWindow{Buys: 70, Sells: 40, Buyers: 15} }, "buyers"},
		// The live DATABEAR case: 22%/day fee pace, ~8000% APR, and dumping. The
		// yield is being paid by a collapsing price — a high APR is not a reason
		// to skip the momentum gates.
		{"high yield but dumping", func(p *Pool) { p.ChangeH1Pct = -18.3 }, "1h"},
		{"high yield but downtrending", func(p *Pool) { p.ChangeH24Pct = -37.6 }, "24h"},
	}
	for _, c := range cases {
		p := maturePool(now)
		c.mutate(&p)
		cand, reason := Screen(p, Mature, now)
		if reason == "" {
			t.Errorf("%s: expected reject, candidate passed (score %.0f)", c.name, cand.Score)
			continue
		}
		if !strings.HasPrefix(reason, c.want) {
			t.Errorf("%s: reason = %q, want prefix %q", c.name, reason, c.want)
		}
	}
}

// The gateway orders token0/token1 by ADDRESS, not by role, so WETH lands on
// either side. Getting this wrong would make Screen reject half the venue as
// "non-WETH quote".
func TestToPoolOrientsWETHAsQuote(t *testing.T) {
	weth := uniToken{Address: WETH, Symbol: "WETH", Decimals: 18}
	meow := uniToken{Address: "0x21028be78e8f521214d24328715c1a8aadbac5a8", Symbol: "MEOW", Decimals: 18}

	cases := []struct {
		name           string
		token0, token1 uniToken
	}{
		{"weth is token1", meow, weth},
		{"weth is token0", weth, meow},
	}
	for _, c := range cases {
		p, ok := toPool(uniPool{
			Address:            "0xabc",
			CreatedAtTimestamp: 1781812885,
			FeeTier:            10000,
			TotalLiquidity:     &uniAmount{Value: 82000},
			CumulativeVolume:   &uniAmount{Value: 740000},
			Token0:             c.token0,
			Token1:             c.token1,
		}, "v3")
		if !ok {
			t.Fatalf("%s: toPool rejected a valid pool", c.name)
		}
		if !strings.EqualFold(p.QuoteAddress, WETH) {
			t.Errorf("%s: quote = %q, want WETH on the quote side", c.name, p.QuoteAddress)
		}
		if p.BaseSymbol != "MEOW" {
			t.Errorf("%s: base symbol = %q, want MEOW", c.name, p.BaseSymbol)
		}
	}
}

// v4 gateway entries: the id lives in poolId (bytes32), native-ETH sides come
// back with an EMPTY address and must normalize to the NativeETH sentinel, and
// USDG must be recognized as the quote even when address-ordering puts it on
// token1 with a memecoin on token0.
func TestToPoolV4(t *testing.T) {
	usdg := uniToken{Address: USDG, Symbol: "USDG", Decimals: 6}
	cash := uniToken{Address: "0x020bfC650A365f8BB26819deAAbF3E21291018b4", Symbol: "CASHCAT", Decimals: 18}
	eth := uniToken{Symbol: "ETH", Decimals: 18} // gateway sends null address for native ETH

	p, ok := toPool(uniPool{
		PoolID:             "0xee0d95c4644e0847eca78d5f5333abe1ab798b159fdd01712327258a755d4423",
		CreatedAtTimestamp: 1783526658,
		FeeTier:            5000,
		Token0:             cash,
		Token1:             usdg,
	}, "v4")
	if !ok {
		t.Fatal("toPool rejected a valid v4 pool")
	}
	if p.Protocol != "v4" || p.Dex != "uniswap-v4" {
		t.Errorf("protocol fields wrong: protocol=%q dex=%q", p.Protocol, p.Dex)
	}
	if !strings.HasPrefix(p.Address, "0xee0d95c4") {
		t.Errorf("v4 address should be the poolId, got %q", p.Address)
	}
	if p.QuoteSymbol != "USDG" || p.BaseSymbol != "CASHCAT" {
		t.Errorf("USDG should orient to the quote side: base=%q quote=%q", p.BaseSymbol, p.QuoteSymbol)
	}
	if p.QuoteDecimals != 6 {
		t.Errorf("USDG quote decimals = %d, want 6", p.QuoteDecimals)
	}

	p, ok = toPool(uniPool{
		PoolID:             "0x54f7883914619af9105355bf83ed678bcf9f63560218ac61c9963b9503d0ba32",
		CreatedAtTimestamp: 1783521035,
		FeeTier:            460,
		Token0:             eth,
		Token1:             usdg,
	}, "v4")
	if !ok {
		t.Fatal("toPool rejected a valid native-ETH v4 pool")
	}
	// Both sides are quote assets here (ETH/USDG); whichever wins, the empty
	// native address must have been normalized to the sentinel, never "".
	if p.BaseAddress == "" || p.QuoteAddress == "" {
		t.Errorf("native ETH side not normalized: base=%q quote=%q", p.BaseAddress, p.QuoteAddress)
	}
	if p.FeePct != 0.046 {
		t.Errorf("v4 feeTier 460 => %v%%, want 0.046%%", p.FeePct)
	}
}

// A hooked v4 pool must carry its hook through toPool so prefilter/Screen can
// reject it — a dropped hook field would read as hookless and pass.
func TestToPoolCarriesHook(t *testing.T) {
	hooked := uniPool{
		PoolID:             "0xabc0000000000000000000000000000000000000000000000000000000000001",
		CreatedAtTimestamp: 1781812885,
		FeeTier:            3000,
		IsDynamicFee:       true,
	}
	hooked.Hook = &struct {
		Address string `json:"address"`
	}{Address: "0x4e3468951D49f2EEa976eD0D6e75fFCb44a9a544"}

	p, ok := toPool(hooked, "v4")
	if !ok {
		t.Fatal("toPool rejected the pool")
	}
	if p.Hook == "" || !p.DynamicFee {
		t.Errorf("hook/dynamic-fee lost in mapping: hook=%q dynamic=%v", p.Hook, p.DynamicFee)
	}
}

// feeTier arrives in hundredths of a bip (10000 = 1%), while every gate and the
// emitted payload speak percent.
func TestToPoolFeeTierToPercent(t *testing.T) {
	cases := []struct {
		tier float64
		want float64
	}{
		{10000, 1},
		{3000, 0.3},
		{500, 0.05},
		{100, 0.01},
	}
	for _, c := range cases {
		p, ok := toPool(uniPool{Address: "0xabc", CreatedAtTimestamp: 1781812885, FeeTier: c.tier}, "v3")
		if !ok {
			t.Fatalf("tier %v: toPool rejected a valid pool", c.tier)
		}
		if p.FeePct != c.want {
			t.Errorf("feeTier %v => %v%%, want %v%%", c.tier, p.FeePct, c.want)
		}
	}
}

// A nil money wrapper (a pool the gateway has not finished indexing) must read
// as zero, not panic.
func TestToPoolNilAmounts(t *testing.T) {
	p, ok := toPool(uniPool{Address: "0xabc", CreatedAtTimestamp: 1781812885, FeeTier: 10000}, "v3")
	if !ok {
		t.Fatal("toPool rejected a pool with nil amounts")
	}
	if p.ReserveUSD != 0 || p.VolumeH24USD != 0 {
		t.Errorf("nil amounts should read 0, got reserve=%v vol24h=%v", p.ReserveUSD, p.VolumeH24USD)
	}
}

func TestToPoolRejectsUnusable(t *testing.T) {
	if _, ok := toPool(uniPool{CreatedAtTimestamp: 1781812885}, "v3"); ok {
		t.Error("pool with no address should be rejected")
	}
	// No creation timestamp means no age, and every mature gate is age-relative.
	if _, ok := toPool(uniPool{Address: "0xabc"}, "v3"); ok {
		t.Error("pool with no createdAtTimestamp should be rejected")
	}
	// A v4 entry carries its id in poolId, not address.
	if _, ok := toPool(uniPool{Address: "0xabc", CreatedAtTimestamp: 1781812885}, "v4"); ok {
		t.Error("v4 pool with no poolId should be rejected")
	}
}

// prefilter must be a strict SUBSET of Screen: it may only shrink the
// enrichment batch, never reject something Screen would have kept. It runs
// BEFORE enrichment, so it must not read h1 flow, FDV or price-change fields —
// they are all still zero at that point.
func TestPrefilterIgnoresUnenrichedFields(t *testing.T) {
	now := time.Now()
	p := maturePool(now)
	p.VolumeH1USD = 0
	p.TxH1 = gtTxWindow{}
	p.FdvUSD = 0
	p.ChangeM5Pct, p.ChangeH1Pct, p.ChangeH6Pct, p.ChangeH24Pct = 0, 0, 0, 0

	if !prefilter(p, Mature, now) {
		t.Error("prefilter must pass a pool whose h1/FDV fields are not yet enriched")
	}
}

func TestPrefilterRejects(t *testing.T) {
	now := time.Now()
	cases := []struct {
		name   string
		mutate func(*Pool)
	}{
		{"non-quote-asset quote", func(p *Pool) { p.QuoteAddress = "0x1111111111111111111111111111111111111111" }},
		{"v4 hooked pool", func(p *Pool) { p.Hook = "0x4e3468951D49f2EEa976eD0D6e75fFCb44a9a544" }},
		{"v4 dynamic fee", func(p *Pool) { p.DynamicFee = true }},
		{"younger than min age", func(p *Pool) { p.CreatedAt = now.Add(-2 * time.Hour) }},
		{"reserve floor", func(p *Pool) { p.ReserveUSD = 9000 }},
		{"reserve cap", func(p *Pool) { p.ReserveUSD = 900000 }},
		{"fee tier floor", func(p *Pool) { p.FeePct = 0.01 }},
		{"fee pace floor", func(p *Pool) { p.VolumeH24USD = 40000 }},
	}
	for _, c := range cases {
		p := maturePool(now)
		c.mutate(&p)
		if prefilter(p, Mature, now) {
			t.Errorf("%s: expected prefilter to reject", c.name)
		}
	}
}
