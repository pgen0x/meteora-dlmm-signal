package meteora

import (
	"fmt"
	"math"
)

// ModeParams are the per-mode screening thresholds, ported verbatim from
// dlmm_pipeline.py MODE_DEFAULTS / SOUL.md section 9.
type ModeParams struct {
	Mode            string
	Timeframe       string  // discovery timeframe to query
	TfMinutes       float64 // Timeframe in minutes (degen-score window normalization)
	MinTVL          float64 // MIN_TVL_USD
	MinFeeTVL       float64 // MIN_FEE_TVL_24H (percent)
	MinMcap         float64 // MIN_MCAP_USD
	MinHolders      int     // MIN_HOLDERS
	MinDailyFee     float64 // absolute daily-fee floor (USD)
	MinOrganic      float64 // shared MIN_ORGANIC_SCORE
	MinQuoteOrganic float64 // quote-token organic floor (ported from Meridian)
	MinBinStep      int     // DLMM bin-step floor (0 disables the gate)
	MaxBinStep      int     // DLMM bin-step ceiling (0 disables the gate)
}

// Casual and Multiday mirror the two isolated budgets in the pipeline.
// Bin-step band (80–125) ported from Meridian config; tune per strategy.
//
// DIVERGENCE from dlmm_pipeline.py: casual MinFeeTVL is 0.1, not the upstream
// 0.3. The API's fee_tvl_ratio is scoped to the queried timeframe, so for the
// 30m casual window 0.3 demanded a ~14.4%/day fee pace — live probe (2026-07-05)
// showed the 30m median ratio at ~0.01%, so 0 of 50 pools passed most cycles.
// 0.1 (~4.8%/day pace) still sits ~5x above multiday's 1%/day bar.
var (
	Casual = ModeParams{
		Mode: "casual", Timeframe: "30m", TfMinutes: 30,
		MinTVL: 5000, MinFeeTVL: 0.1, MinMcap: 250000, MinHolders: 500,
		MinDailyFee: 20, MinOrganic: 60, MinQuoteOrganic: 60,
		MinBinStep: 80, MaxBinStep: 125,
	}
	Multiday = ModeParams{
		Mode: "multiday", Timeframe: "24h", TfMinutes: 1440,
		MinTVL: 50000, MinFeeTVL: 1.0, MinMcap: 1000000, MinHolders: 1000,
		MinDailyFee: 150, MinOrganic: 60, MinQuoteOrganic: 60,
		MinBinStep: 80, MaxBinStep: 125,
	}
)

// Degen Score targets — each liquidity-relative sub-score saturates here.
// Ported from Meridian; inputs are normalized to a 30m reference window.
const (
	degenRefMinutes      = 30.0
	degenTargetVolRatio  = 20.0    // (30m) volume/active_tvl for a full trading sub-score
	degenTargetLpCount   = 40.0    // (30m) unique_lps + positions_created for a full LP sub-score
	degenTargetFeeRatio  = 0.20    // (30m) fee/active_tvl for a full fee sub-score
	degenTargetLiquidity = 20000.0 // active_tvl ($) for full liquidity sub-score (not TF-scaled)
)

// SkipReason is returned (non-empty) when a pool fails a gate, for logging.
// A returned Candidate is only valid when reason == "".
func Screen(p Pool, mp ModeParams) (*Candidate, string) {
	// Orientation: exactly one side must be SOL.
	var base, quote Token
	var solIsX bool
	switch {
	case p.TokenY.Address == SolMint:
		base, quote, solIsX = p.TokenX, p.TokenY, false
	case p.TokenX.Address == SolMint:
		base, quote, solIsX = p.TokenY, p.TokenX, true
	default:
		return nil, "non-SOL pool"
	}

	// Authoritative API risk flags (ported from Meridian) — cheaper than parsing
	// the warnings array and caught before any threshold math.
	if p.HasCriticalWarnings {
		return nil, "base token critical warnings"
	}
	if p.QuoteHasCriticalWarnings {
		return nil, "quote token critical warnings"
	}
	if p.HasHighSingleOwnership {
		return nil, "base token high single ownership"
	}
	if p.HasHighSupplyConcentration {
		return nil, "base token high supply concentration"
	}

	if p.TVL < mp.MinTVL {
		return nil, fmt.Sprintf("TVL $%.0f < $%.0f", p.TVL, mp.MinTVL)
	}
	if p.FeeTVLRatio < mp.MinFeeTVL {
		return nil, fmt.Sprintf("fee/TVL %.2f%% < %.2f%%", p.FeeTVLRatio, mp.MinFeeTVL)
	}
	dailyFeeUSD := p.TVL * p.FeeTVLRatio / 100.0
	if dailyFeeUSD < mp.MinDailyFee {
		return nil, fmt.Sprintf("daily fees $%.0f < $%.0f", dailyFeeUSD, mp.MinDailyFee)
	}
	if p.Volatility <= 0 {
		return nil, "volatility <= 0"
	}
	if p.Volatility > 15 {
		return nil, fmt.Sprintf("volatility %.2f > 15 (IL risk)", p.Volatility)
	}
	if base.OrganicScore < mp.MinOrganic {
		return nil, fmt.Sprintf("organic %.0f < %.0f", base.OrganicScore, mp.MinOrganic)
	}
	if mp.MinQuoteOrganic > 0 && quote.OrganicScore < mp.MinQuoteOrganic {
		return nil, fmt.Sprintf("quote organic %.0f < %.0f", quote.OrganicScore, mp.MinQuoteOrganic)
	}
	if base.MarketCap < mp.MinMcap {
		return nil, fmt.Sprintf("mcap $%.0f < $%.0f", base.MarketCap, mp.MinMcap)
	}
	if base.Holders < mp.MinHolders {
		return nil, fmt.Sprintf("holders %d < %d", base.Holders, mp.MinHolders)
	}
	if p.FeeTVLRatioChangePct < -40.0 {
		return nil, fmt.Sprintf("yield declining %.0f%%", p.FeeTVLRatioChangePct)
	}

	// Supply-concentration safety gates.
	if base.TopHoldersPct > 60.0 {
		return nil, fmt.Sprintf("top10 own %.1f%% (>60%%)", base.TopHoldersPct)
	}
	if base.DevBalancePct > 20.0 {
		return nil, fmt.Sprintf("dev owns %.1f%% (>20%%)", base.DevBalancePct)
	}

	// Authority gates.
	if base.HasFreezeAuth {
		return nil, "freeze authority enabled"
	}
	if base.HasMintAuth {
		return nil, "mint authority enabled"
	}

	// Verified + Jupiter shield, fail-open when absent.
	if !boolOr(base.Verified, true) {
		return nil, "not verified"
	}
	jupShield := base.JupShieldVerified
	if jupShield == nil {
		jupShield = base.JupShield
	}
	if !boolOr(jupShield, true) {
		return nil, "failed Jupiter shield"
	}

	// Critical / warning severity gate.
	for _, w := range base.Warnings {
		if w.Severity == "critical" || w.Severity == "warning" {
			return nil, "warning: " + w.Message
		}
	}

	// Bin-step band gate (ported from Meridian). 0 endpoints disable each side.
	binStep := p.DlmmParams.BinStep
	if mp.MinBinStep > 0 && binStep < mp.MinBinStep {
		return nil, fmt.Sprintf("bin_step %d < %d", binStep, mp.MinBinStep)
	}
	if mp.MaxBinStep > 0 && binStep > mp.MaxBinStep {
		return nil, fmt.Sprintf("bin_step %d > %d", binStep, mp.MaxBinStep)
	}

	// Degen Score (0..100) replaces the old additive score: geometric mean of
	// four liquidity-relative sub-scores (trading / LP / fee / liquidity), so a
	// high score requires balance — no single metric can dominate. Falls back to
	// the additive score when the API omits the liquidity-relative inputs.
	score := degenScore(p, mp.TfMinutes)
	if score <= 0 {
		score = base.OrganicScore + (p.FeeActiveTVLRatio * 10) - (p.Volatility * 1.5)
		if p.FeeTVLRatioChangePct > 30 {
			score += 10
		}
	}

	return &Candidate{
		Mode:                 mp.Mode,
		Timeframe:            mp.Timeframe,
		Pool:                 p.PoolAddress,
		Name:                 p.Name,
		BaseMint:             base.Address,
		BaseSymbol:           base.Symbol,
		SolIsX:               solIsX,
		TVL:                  p.TVL,
		FeeTVLRatio:          p.FeeTVLRatio,
		FeeActiveTVLRatio:    p.FeeActiveTVLRatio,
		FeeTVLRatioChangePct: p.FeeTVLRatioChangePct,
		DailyFeeUSD:          dailyFeeUSD,
		Volatility:           p.Volatility,
		BinStep:              p.DlmmParams.BinStep,
		OrganicScore:         base.OrganicScore,
		Mcap:                 base.MarketCap,
		Holders:              base.Holders,
		TopHoldersPct:        base.TopHoldersPct,
		DevBalancePct:        base.DevBalancePct,
		Score:                score,
	}, ""
}

// degenScore returns a pool's 0..100 efficiency score: the geometric mean of
// four liquidity-relative sub-scores (trading, LP activity, fees, liquidity).
// Any zero sub-score => 0, enforcing balance across all four. Window-dependent
// inputs are normalized to a 30m reference so the targets stay valid across
// timeframes. Returns 0 when active_tvl is missing (caller falls back).
func degenScore(p Pool, tfMinutes float64) float64 {
	la := p.ActiveTVL
	if la <= 0 {
		la = p.TVL
	}
	if la <= 0 || tfMinutes <= 0 {
		return 0
	}
	tfScale := degenRefMinutes / tfMinutes

	tradingRatio := p.VolumeActiveTVLRatio * tfScale
	feeRatio := p.FeeActiveTVLRatio * tfScale
	lpActivity := (p.UniqueLPs + p.PositionsCreated) * tfScale

	sTrading := clamp01(tradingRatio / degenTargetVolRatio)
	sLp := clamp01(lpActivity / degenTargetLpCount)
	sFees := clamp01(feeRatio / degenTargetFeeRatio)
	sLiq := clamp01(math.Log10(la) / math.Log10(degenTargetLiquidity))

	return math.Pow(sTrading*sLp*sFees*sLiq, 0.25) * 100
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
