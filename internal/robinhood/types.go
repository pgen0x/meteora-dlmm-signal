// Package robinhood discovers and screens newly-created Uniswap v3 pools on
// Robinhood Chain (chain ID 4663) via the GeckoTerminal public API, mirroring
// the internal/meteora poll ▸ screen ▸ dedup ▸ forward flow for the EVM venue.
//
// Phase 1 scope (docs/ROBINHOOD_CHAIN_PLAN.md): signal-only — candidates are
// forwarded to the webhook sink for observation; the direct-deploy runner is
// never invoked for this venue until the EVM executor lands (Phase 2).
package robinhood

import (
	"encoding/json"
	"time"
)

// Chain is the canonical venue slug used in dedup keys, signal payloads and
// GMGN OpenAPI calls (verified live 2026-07-13: chain=robinhood is the only
// accepted spelling; "rh" and "4663" return code 40000300).
const Chain = "robinhood"

// WETH is the canonical wrapped-ether address on Robinhood Chain — the quote
// side we require, playing the role SolMint plays on the Solana venue.
// Observed as the quote of 16/20 pools on a live new_pools page (2026-07-13).
const WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"

// gtTxWindow is the buys/sells breakdown for one time window.
type gtTxWindow struct {
	Buys    int `json:"buys"`
	Sells   int `json:"sells"`
	Buyers  int `json:"buyers"`
	Sellers int `json:"sellers"`
}

// gtPoolAttrs mirrors the attribute block of one GeckoTerminal pool entry.
// Numeric fields arrive as JSON strings ("9016.95") and are parsed by the
// screen; missing/unparseable values become 0 (each gate documents whether
// zero passes or rejects).
type gtPoolAttrs struct {
	Address       string `json:"address"`
	Name          string `json:"name"`
	PoolCreatedAt string `json:"pool_created_at"` // RFC3339
	ReserveUSD    string `json:"reserve_in_usd"`
	FdvUSD        string `json:"fdv_usd"`
	MarketCapUSD  string `json:"market_cap_usd"`

	PriceChangePct struct {
		M5  string `json:"m5"`
		H1  string `json:"h1"`
		H6  string `json:"h6"`
		H24 string `json:"h24"`
	} `json:"price_change_percentage"`

	Transactions struct {
		H1  gtTxWindow `json:"h1"`
		H24 gtTxWindow `json:"h24"`
	} `json:"transactions"`

	VolumeUSD struct {
		H1  string `json:"h1"`
		H24 string `json:"h24"`
	} `json:"volume_usd"`
}

// gtRel is one JSON:API relationship stub ({"data":{"id":..,"type":..}}).
type gtRel struct {
	Data struct {
		ID   string `json:"id"`
		Type string `json:"type"`
	} `json:"data"`
}

// gtPool is one entry of the new_pools data array.
type gtPool struct {
	ID            string      `json:"id"`
	Attrs         gtPoolAttrs `json:"attributes"`
	Relationships struct {
		BaseToken  gtRel `json:"base_token"`
		QuoteToken gtRel `json:"quote_token"`
		Dex        gtRel `json:"dex"`
	} `json:"relationships"`
}

// gtToken is one included token resource (from ?include=base_token,quote_token).
type gtToken struct {
	ID    string `json:"id"`
	Type  string `json:"type"`
	Attrs struct {
		Address  string `json:"address"`
		Name     string `json:"name"`
		Symbol   string `json:"symbol"`
		Decimals int    `json:"decimals"`
	} `json:"attributes"`
}

// gtResponse is the top-level new_pools envelope. Included resources are kept
// raw and decoded per-type: the array mixes tokens and dexes.
type gtResponse struct {
	Data     []gtPool          `json:"data"`
	Included []json.RawMessage `json:"included"`
}

// Pool is a decoded, unit-normalized GeckoTerminal pool ready for screening.
type Pool struct {
	Address   string // v3 pool contract address (v4 pools carry a bytes32 id and are filtered out)
	Name      string // "CALLIE / WETH 0.3%"
	Dex       string // "uniswap-v3-robinhood"
	CreatedAt time.Time

	BaseAddress  string
	BaseSymbol   string
	BaseDecimals int
	QuoteAddress string
	QuoteSymbol  string

	FeePct     float64 // parsed from the trailing "0.3%" in Name; 0 = unknown
	ReserveUSD float64
	FdvUSD     float64
	McapUSD    float64

	VolumeH1USD  float64
	VolumeH24USD float64
	TxH1         gtTxWindow
	TxH24        gtTxWindow

	ChangeM5Pct  float64
	ChangeH1Pct  float64
	ChangeH6Pct  float64
	ChangeH24Pct float64
}

// Candidate is a screened, qualifying Robinhood Chain pool ready to emit.
// The payload shape is documented in docs/SIGNAL_SCHEMA.md under
// "robinhood_pool_discovery" — keep the two in sync.
type Candidate struct {
	Chain     string  `json:"chain"` // always "robinhood"
	Mode      string  `json:"mode"`
	Pool      string  `json:"pool"`
	Dex       string  `json:"dex"` // "uniswap-v3"
	Name      string  `json:"name"`
	CreatedAt string  `json:"created_at"` // RFC3339
	AgeMin    float64 `json:"age_minutes"`

	BaseAddress  string `json:"base_address"`
	BaseSymbol   string `json:"base_symbol"`
	BaseDecimals int    `json:"base_decimals"`
	QuoteAddress string `json:"quote_address"`
	QuoteSymbol  string `json:"quote_symbol"`

	FeePct       float64 `json:"fee_pct"`
	ReserveUSD   float64 `json:"reserve_usd"`
	FdvUSD       float64 `json:"fdv_usd"`
	McapUSD      float64 `json:"mcap_usd"`
	VolumeH1USD  float64 `json:"volume_h1_usd"`
	VolumeH24USD float64 `json:"volume_h24_usd"`
	FeeTVLDayPct float64 `json:"fee_tvl_day_pct"` // projected daily fee/TVL %, from h1 volume pace
	TxH1         int     `json:"tx_h1"`
	BuyersH1     int     `json:"buyers_h1"`
	SellersH1    int     `json:"sellers_h1"`
	ChangeM5Pct  float64 `json:"change_m5_pct"`
	ChangeH1Pct  float64 `json:"change_h1_pct"`
	Score        float64 `json:"score"`

	// Blockscout enrichment (safety.go). Pointer + omitempty: absent means the
	// fetch failed (fail-open) — consumers treat missing as unknown, not zero.
	Holders *int `json:"holders,omitempty"`

	// GMGN security enrichment (safety.go). Absent = fetch failed or field
	// null/-1 (unknown) — fail-open. A POSITIVE honeypot/blacklist detection
	// rejects at screen time and never reaches the payload.
	GmgnSellTaxPct *float64 `json:"gmgn_sell_tax_pct,omitempty"`
	GmgnBuyTaxPct  *float64 `json:"gmgn_buy_tax_pct,omitempty"`
	GmgnOpenSource *bool    `json:"gmgn_open_source,omitempty"`
	GmgnLaunchpad  string   `json:"gmgn_launchpad,omitempty"`

	// GMGN holder-quality enrichment — same semantics as the Solana venue's
	// gmgn_* fields (see internal/meteora/gmgn.go).
	GmgnSmartWallets     *int     `json:"gmgn_smart_wallets,omitempty"`
	GmgnBundlerWallets   *int     `json:"gmgn_bundler_wallets,omitempty"`
	GmgnRatVolumePct     *float64 `json:"gmgn_rat_volume_pct,omitempty"`
	GmgnBundlerVolumePct *float64 `json:"gmgn_bundler_volume_pct,omitempty"`
	GmgnTop10Pct         *float64 `json:"gmgn_top10_pct,omitempty"`
	GmgnDevStatus        string   `json:"gmgn_dev_status,omitempty"`
}
