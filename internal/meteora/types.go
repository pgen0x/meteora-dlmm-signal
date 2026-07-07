package meteora

// SolMint is the wrapped-SOL mint. Pools are only considered if one side is SOL.
const SolMint = "So11111111111111111111111111111111111111112"

// Warning is one base-token risk warning from the discovery API.
type Warning struct {
	Severity string `json:"severity"`
	Message  string `json:"message"`
}

// Token is one side of a pool (token_x or token_y) as returned by the
// Meteora pool-discovery API. Field names mirror the JSON exactly.
type Token struct {
	Address       string    `json:"address"`
	Symbol        string    `json:"symbol"`
	OrganicScore  float64   `json:"organic_score"`
	MarketCap     float64   `json:"market_cap"`
	Holders       int       `json:"holders"`
	TopHoldersPct float64   `json:"top_holders_pct"`
	DevBalancePct float64   `json:"dev_balance_pct"`
	HasFreezeAuth bool      `json:"has_freeze_authority"`
	HasMintAuth   bool      `json:"has_mint_authority"`
	Warnings      []Warning `json:"warnings"`

	// Verified / JupShield fail OPEN: the API omits them for some tokens, so a
	// nil pointer means "not provided" -> treated as passing (fail-open). The
	// discovery API's field is `is_verified`; the Python pipeline mapped a
	// non-existent `verified` key so its verified-gate never actually fired.
	// Mapping the real field here tightens token selection.
	Verified *bool `json:"is_verified"`

	// JupShield fields are not returned by the current discovery API, so these
	// stay nil and fail open. Kept for forward-compat if the API adds them.
	JupShieldVerified *bool `json:"jupshield_verified"`
	JupShield         *bool `json:"jup_shield"`
}

// DlmmParams carries the bin step for a pool.
type DlmmParams struct {
	BinStep int `json:"bin_step"`
}

// Pool is one entry from the discovery API data array.
type Pool struct {
	PoolAddress          string     `json:"pool_address"`
	Name                 string     `json:"name"`
	TVL                  float64    `json:"tvl"`
	ActiveTVL            float64    `json:"active_tvl"`
	FeeTVLRatio          float64    `json:"fee_tvl_ratio"`
	FeeActiveTVLRatio    float64    `json:"fee_active_tvl_ratio"`
	FeeTVLRatioChangePct float64    `json:"fee_tvl_ratio_change_pct"`
	VolumeActiveTVLRatio float64    `json:"volume_active_tvl_ratio"`
	VolumeTVLRatio       float64    `json:"volume_tvl_ratio"`
	VolumeWindow         float64    `json:"volume"`
	FeeWindow            float64    `json:"fee"`
	FeePct               float64    `json:"fee_pct"`
	SwapCount            float64    `json:"swap_count"`
	UniqueTraders        float64    `json:"unique_traders"`
	UniqueLPs            float64    `json:"unique_lps"`
	PositionsCreated     float64    `json:"positions_created"`
	Volatility           float64    `json:"volatility"`
	TokenX               Token      `json:"token_x"`
	TokenY               Token      `json:"token_y"`
	DlmmParams           DlmmParams `json:"dlmm_params"`

	// Authoritative risk flags from the discovery API (cheaper + more reliable
	// than parsing the warnings array). Ported from Meridian getRawPoolScreeningRejectReason.
	HasCriticalWarnings        bool `json:"base_token_has_critical_warnings"`
	QuoteHasCriticalWarnings   bool `json:"quote_token_has_critical_warnings"`
	HasHighSingleOwnership     bool `json:"base_token_has_high_single_ownership"`
	HasHighSupplyConcentration bool `json:"base_token_has_high_supply_concentration"`
}

// discoverResponse is the top-level discovery API envelope.
type discoverResponse struct {
	Data []Pool `json:"data"`
}

// Candidate is a screened, qualifying pool ready to emit as a signal.
// It flattens the base-token view the agent needs for its review.
type Candidate struct {
	Mode                 string  `json:"mode"`
	Timeframe            string  `json:"timeframe"`
	Pool                 string  `json:"pool"`
	Name                 string  `json:"name"`
	BaseMint             string  `json:"base_mint"`
	BaseSymbol           string  `json:"base_symbol"`
	SolIsX               bool    `json:"sol_is_x"`
	TVL                  float64 `json:"tvl"`
	FeeTVLRatio          float64 `json:"fee_tvl_ratio"`
	FeeActiveTVLRatio    float64 `json:"fee_active_tvl_ratio"`
	FeeTVLRatioChangePct float64 `json:"fee_tvl_ratio_change_pct"`
	DailyFeeUSD          float64 `json:"daily_fee_usd"`
	Volatility           float64 `json:"volatility"`
	BinStep              int     `json:"bin_step"`
	FeePct               float64 `json:"fee_pct"`
	VolumeTVLRatio       float64 `json:"volume_tvl_ratio"`
	SwapCount            float64 `json:"swap_count"`
	UniqueTraders        float64 `json:"unique_traders"`
	OrganicScore         float64 `json:"organic_score"`
	Mcap                 float64 `json:"mcap"`
	Holders              int     `json:"holders"`
	TopHoldersPct        float64 `json:"top_holders_pct"`
	DevBalancePct        float64 `json:"dev_balance_pct"`
	Score                float64 `json:"score"`

	// Degen Score inputs, exposed so the agent sees WHY a score is high/low
	// instead of trusting an opaque number.
	ActiveTVL            float64 `json:"active_tvl"`
	VolumeActiveTVLRatio float64 `json:"volume_active_tvl_ratio"`
	UniqueLPs            float64 `json:"unique_lps"`
	PositionsCreated     float64 `json:"positions_created"`

	// Jupiter audit enrichment (audit gate). Pointers + omitempty: absent
	// means the audit fetch failed or omitted the field (fail-open) — the
	// agent must treat missing as unknown, not zero.
	BotHoldersPct *float64 `json:"bot_holders_pct,omitempty"`
	GlobalFeesSOL *float64 `json:"global_fees_sol,omitempty"`

	// Pool memory summary from the monitor's close journal
	// (sol:dlmm:history:pool:<pool>). The pipeline still hard-skips pools
	// whose history nets negative; these fields let the agent ALSO weigh a
	// mixed record when picking between candidates. Absent = no history
	// (or in-memory dedup backend without Redis).
	PriorCloses    *int     `json:"prior_closes,omitempty"`
	PriorNetPnlSOL *float64 `json:"prior_net_pnl_sol,omitempty"`

	// PVP (same-symbol rival) flag + rival stats (pvp.go). Absent/false means
	// no established rival found OR the check failed (fail-open) — the flag is
	// advisory for the agent's pick, never a daemon-side reject.
	IsPVP           bool    `json:"is_pvp,omitempty"`
	PVPRivalName    string  `json:"pvp_rival_name,omitempty"`
	PVPRivalMint    string  `json:"pvp_rival_mint,omitempty"`
	PVPRivalPool    string  `json:"pvp_rival_pool,omitempty"`
	PVPRivalTVL     float64 `json:"pvp_rival_tvl,omitempty"`
	PVPRivalHolders float64 `json:"pvp_rival_holders,omitempty"`
	PVPRivalFeesSOL float64 `json:"pvp_rival_fees_sol,omitempty"`
}

// boolOr dereferences an optional bool, returning def when the pointer is nil
// (field absent from the API payload -> fail-open).
func boolOr(p *bool, def bool) bool {
	if p == nil {
		return def
	}
	return *p
}
