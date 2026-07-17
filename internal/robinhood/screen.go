package robinhood

import (
	"fmt"
	"math"
	"time"
)

// ModeParams are the per-mode screening thresholds for the Robinhood Chain
// venue. Unlike the Solana modes (verbatim ports of dlmm_pipeline.py), these
// are FIRST-PASS values chosen from the 2026-07-13 spike sample and exist to
// be recalibrated from Phase 1 signal-only journals — expect churn.
type ModeParams struct {
	Mode string

	MinAge time.Duration // dodge the first sniper/MEV minutes of a launch
	MaxAge time.Duration // stay inside the mode's thesis window (0 disables)

	MinReserveUSD float64 // liquidity floor: LP fees on dust reserves round to zero
	MaxReserveUSD float64 // ceiling biases small pools where our share matters (0 disables)
	MinFeePct     float64 // v3 fee tier floor; memecoin launches sit at 1% (Noxa default)
	MinFeeTVLDay  float64 // projected daily fee/TVL % floor (volume pace x fee tier)
	MinTxH1       int     // swaps in the last hour (wash guard with MinBuyersH1)
	MinBuyersH1   int     // unique buyers in the last hour
	MinFdvUSD     float64 // FDV sanity floor
	MaxFdvUSD     float64 // FDV sanity ceiling (0 disables): fake-priced pools show absurd FDV

	// FeePaceH24 measures the fee/TVL pace over the realized 24h volume instead
	// of extrapolating the h1 window out to a day. Modes selecting for
	// SUSTAINED fee generation must set it: under h1 extrapolation one busy
	// hour mints a 24x-inflated daily rate, which is precisely the pool a
	// mature mode must not buy. Fresh leaves it false — a pool minutes old has
	// no 24h history to measure, so extrapolating is the only option it has.
	FeePaceH24 bool
}

// Fresh is the starter mode: young Uniswap v3 WETH pools already showing
// two-sided flow. One mode only until the signal-only journals justify more.
var Fresh = ModeParams{
	Mode:   "rh-fresh",
	MinAge: 3 * time.Minute,
	MaxAge: 24 * time.Hour,
	// 8000 (initial guess, set by analogy to Solana casual mode) killed 73% of
	// all pools before any other gate ran — live sample 2026-07-13 showed
	// median reserve ~$3,959, only 6/16 pools clearing $8k. 2500 lets the
	// bulk of real launches through to the gates that actually matter
	// (txn/buyer counts, honeypot shape, GMGN security) — see
	// docs/ROBINHOOD_CHAIN_PLAN.md calibration notes.
	MinReserveUSD: 2500,
	MaxReserveUSD: 500000,
	MinFeePct:     0.25,
	MinFeeTVLDay:  5.0, // ~5%/day pace, between casual (~4.8) and turnover (~7.2) bars
	MinTxH1:       30,
	MinBuyersH1:   12,
	MinFdvUSD:     20000,
	MaxFdvUSD:     50_000_000,
}

// Mature is the second mode: pools PAST the launch window that are still
// printing outsized fees on real liquidity. It exists because Fresh and
// GeckoTerminal's new_pools feed are structurally blind to them — a pool
// scrolls off new_pools within minutes, and Fresh.MaxAge rejects anything over
// 24h. The live 2026-07-14 sample made the gap concrete: 19 of 62 indexed v3
// pools cleared a 5%/day fee pace inside the reserve band, and every one was
// 66-144h old (DATABEAR/WETH: $65k TVL, $1.44M 24h volume, 1% tier = 22%/day,
// roughly 8000% APR). Fed by FetchMaturePools (Uniswap's gateway), not
// FetchNewPools.
//
// FIRST-PASS values like Fresh's — expect churn once the journals land.
var Mature = ModeParams{
	Mode: "rh-mature",

	// Starts exactly where Fresh ends, so the two modes partition the age axis
	// and no pool can signal twice. No ceiling: a pool that has printed fees
	// for a week is MORE proven, not less — the fee-pace gate is what expires a
	// stale pool, and it does so on evidence rather than on a clock.
	MinAge: 24 * time.Hour,
	MaxAge: 0,

	// 12500 tracks the floor of what Uniswap's gateway actually indexes (~$12.6k
	// on the live sample) — below it we would gate on pools the discovery source
	// cannot see anyway. It is also 5x Fresh's floor on purpose: Fresh accepts
	// thin books because it is paid for launch-window volatility, while this
	// mode holds for days and needs an exit.
	MinReserveUSD: 12500,
	MaxReserveUSD: 500000,
	MinFeePct:     0.25,

	// 8%/day (~2900% APR) against Fresh's 5%. A mature pool competes on
	// SUSTAINED yield and has a full 24h of history to prove it, so the bar
	// should be higher than the one a minutes-old pool clears on extrapolation.
	// Sample check: 19/62 pools cleared 5%, only 5 cleared 8% — a shortlist,
	// which is what the single-position cap wants.
	MinFeeTVLDay: 8.0,
	FeePaceH24:   true,

	// Higher flow floors than Fresh (30/12): these pools have hours of history,
	// so a quiet hour here is real evidence of decay rather than a cold start.
	MinTxH1:     60,
	MinBuyersH1: 20,

	MinFdvUSD: 20000,
	MaxFdvUSD: 50_000_000,
}

// Score saturation targets, degen-score analogs computed over the h1 window.
const (
	targetTurnoverH1 = 3.0     // h1 volume / reserve for a full trading sub-score
	targetBuyersH1   = 60.0    // h1 unique buyers for a full participation sub-score
	targetFeeDayPct  = 25.0    // projected daily fee/TVL % for a full fee sub-score
	targetReserveUSD = 30000.0 // reserve ($) for a full liquidity sub-score
)

// Screen applies the venue gates to one pool. A non-empty reason means the
// pool failed; the Candidate is only valid when reason == "". now comes from
// the caller — clock reads stay at the edges, matching the repo convention.
func Screen(p Pool, mp ModeParams, now time.Time) (*Candidate, string) {
	// Quote side must be a whitelisted quote asset (WETH, USDG, or v4 native
	// ETH) — the venue analog of the SOL-side requirement. orientQuote also
	// repairs orientation: sources put the quote asset base-side for USDG
	// pools, and rejecting on the raw quote field would drop that universe.
	p, ok := orientQuote(p)
	if !ok {
		return nil, fmt.Sprintf("quote not WETH/USDG/ETH (%s/%s)", p.BaseSymbol, p.QuoteSymbol)
	}

	// v4-only hard gates. A hook can block or skim withdrawals (its behavior
	// lives in the 14 permission bits of the hook address — the Cork exploit
	// class), and a dynamic fee invalidates the fee-pace math below — both
	// reject outright. Costs almost nothing: 79/80 top v4 pools were hookless
	// on the 2026-07-14 sample.
	if p.Hook != "" {
		return nil, fmt.Sprintf("v4 hooked pool (%s)", p.Hook)
	}
	if p.DynamicFee {
		return nil, "v4 dynamic fee"
	}

	// Distinct reason prefixes on purpose: the cycle tally collapses reasons
	// to their prefix, and "too fresh to trust" vs "past the thesis window"
	// need separate counts to diagnose coverage (see the 2026-07-13 smoke
	// runs where 57/57 landed in one opaque "age" bucket).
	age := now.Sub(p.CreatedAt)
	if age < mp.MinAge {
		return nil, fmt.Sprintf("too-young %dm < %dm", int(age.Minutes()), int(mp.MinAge.Minutes()))
	}
	if mp.MaxAge > 0 && age > mp.MaxAge {
		return nil, fmt.Sprintf("too-old %.1fh > %.1fh", age.Hours(), mp.MaxAge.Hours())
	}

	if p.ReserveUSD < mp.MinReserveUSD {
		return nil, fmt.Sprintf("reserve $%.0f < $%.0f", p.ReserveUSD, mp.MinReserveUSD)
	}
	if mp.MaxReserveUSD > 0 && p.ReserveUSD > mp.MaxReserveUSD {
		return nil, fmt.Sprintf("reserve $%.0f > $%.0f cap", p.ReserveUSD, mp.MaxReserveUSD)
	}
	if p.FeePct < mp.MinFeePct {
		return nil, fmt.Sprintf("fee tier %.2f%% < %.2f%%", p.FeePct, mp.MinFeePct)
	}

	// Daily fee/TVL. Neither source exposes a fee field, but v3 fees are
	// deterministic (volume x tier), so this is exact for the window it reads.
	// Fresh extrapolates the h1 window (it has no more history); FeePaceH24
	// modes read the realized 24h volume instead — see the field's comment.
	feeTVLDay := 0.0
	if p.ReserveUSD > 0 {
		dayVolume := p.VolumeH1USD * 24
		if mp.FeePaceH24 {
			dayVolume = p.VolumeH24USD
		}
		feeTVLDay = (dayVolume * p.FeePct / 100) / p.ReserveUSD * 100
	}
	if feeTVLDay < mp.MinFeeTVLDay {
		return nil, fmt.Sprintf("fee/TVL pace %.1f%%/d < %.1f%%/d", feeTVLDay, mp.MinFeeTVLDay)
	}

	txH1 := p.TxH1.Buys + p.TxH1.Sells
	if txH1 < mp.MinTxH1 {
		return nil, fmt.Sprintf("txns %d < %d", txH1, mp.MinTxH1)
	}
	if p.TxH1.Buyers < mp.MinBuyersH1 {
		return nil, fmt.Sprintf("buyers %d < %d", p.TxH1.Buyers, mp.MinBuyersH1)
	}

	// Honeypot heuristic, pre-GMGN: real two-sided flow must include sells.
	// Many buys and literally zero sells over an hour is the classic
	// cannot-sell shape; reject before spending safety-gate budget on it.
	if p.TxH1.Buys >= 10 && p.TxH1.Sells == 0 {
		return nil, fmt.Sprintf("no sells (%d buys, 0 sells h1)", p.TxH1.Buys)
	}

	if p.FdvUSD < mp.MinFdvUSD {
		return nil, fmt.Sprintf("fdv $%.0f < $%.0f", p.FdvUSD, mp.MinFdvUSD)
	}
	if mp.MaxFdvUSD > 0 && p.FdvUSD > mp.MaxFdvUSD {
		return nil, fmt.Sprintf("fdv $%.0f > $%.0f cap", p.FdvUSD, mp.MaxFdvUSD)
	}

	// Momentum gates on GeckoTerminal's own windows — same thresholds as the
	// Solana venue's DexScreener gate (meteora.MomentumReject), no extra HTTP.
	if p.ChangeM5Pct <= -5 {
		return nil, fmt.Sprintf("5m %.1f%% <= -5%% (dumping)", p.ChangeM5Pct)
	}
	if p.ChangeH1Pct <= -15 {
		return nil, fmt.Sprintf("1h %.1f%% <= -15%% (dumping)", p.ChangeH1Pct)
	}
	if p.ChangeH6Pct <= -12 {
		return nil, fmt.Sprintf("6h %.1f%% <= -12%% (downtrend)", p.ChangeH6Pct)
	}
	if p.ChangeH24Pct <= -25 {
		return nil, fmt.Sprintf("24h %.1f%% <= -25%% (downtrend)", p.ChangeH24Pct)
	}

	protocol := p.Protocol
	if protocol == "" {
		protocol = "v3" // pre-v4 callers (and tests) never set the field
	}
	return &Candidate{
		Chain:        Chain,
		Mode:         mp.Mode,
		Pool:         p.Address,
		Dex:          "uniswap-" + protocol,
		Protocol:     protocol,
		Name:         p.Name,
		CreatedAt:    p.CreatedAt.UTC().Format(time.RFC3339),
		AgeMin:       age.Minutes(),
		BaseAddress:  p.BaseAddress,
		BaseSymbol:   p.BaseSymbol,
		BaseDecimals: p.BaseDecimals,
		QuoteAddress: p.QuoteAddress,
		QuoteSymbol:  p.QuoteSymbol,
		FeePct:       p.FeePct,
		ReserveUSD:   p.ReserveUSD,
		FdvUSD:       p.FdvUSD,
		McapUSD:      p.McapUSD,
		VolumeH1USD:  p.VolumeH1USD,
		VolumeH24USD: p.VolumeH24USD,
		FeeTVLDayPct: feeTVLDay,
		TxH1:         txH1,
		BuyersH1:     p.TxH1.Buyers,
		SellersH1:    p.TxH1.Sellers,
		ChangeM5Pct:  p.ChangeM5Pct,
		ChangeH1Pct:  p.ChangeH1Pct,
		Score:        score(p, feeTVLDay),
	}, ""
}

// score is the venue's 0..100 efficiency score: geometric mean of four
// sub-scores (turnover, participation, fee pace, liquidity), mirroring the
// Solana degen score's balance-enforcing shape — any zero sub-score zeroes
// the whole score.
func score(p Pool, feeTVLDay float64) float64 {
	if p.ReserveUSD <= 0 {
		return 0
	}
	sTurnover := clamp01((p.VolumeH1USD / p.ReserveUSD) / targetTurnoverH1)
	sBuyers := clamp01(float64(p.TxH1.Buyers) / targetBuyersH1)
	sFees := clamp01(feeTVLDay / targetFeeDayPct)
	sLiq := clamp01(math.Log10(p.ReserveUSD) / math.Log10(targetReserveUSD))
	return math.Pow(sTurnover*sBuyers*sFees*sLiq, 0.25) * 100
}

func clamp01(x float64) float64 {
	if math.IsNaN(x) || math.IsInf(x, 0) || x < 0 {
		return 0
	}
	if x > 1 {
		return 1
	}
	return x
}
