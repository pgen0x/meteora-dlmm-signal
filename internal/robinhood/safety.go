package robinhood

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"time"
)

// Safety gates for the EVM venue. On Solana the #1 rug vector is bot-farmed
// holders; on an EVM chain it is the honeypot / sell-tax contract, so this
// venue DIVERGES from the repo's fail-open convention in one precise way:
//
//   - missing / unfetchable data          -> pass (fail-open, as everywhere)
//   - POSITIVE detection (honeypot=1,
//     blacklist=1, sell tax over cap)     -> hard reject (fail-closed)
//
// GoPlus and honeypot.is do not support chain 4663 (verified 2026-07-13);
// GMGN's OpenAPI does, so it is the primary security source, with Blockscout
// supplying holder counts.

const (
	gmgnSecurityURL  = "https://openapi.gmgn.ai/v1/token/security"
	gmgnTokenInfoURL = "https://openapi.gmgn.ai/v1/token/info"
	blockscoutAPI    = "https://robinhoodchain.blockscout.com/api/v2/tokens/"

	// MaxSellTaxPct rejects tokens whose GMGN-reported sell tax exceeds this.
	// Any nonzero sell tax on a fresh memecoin is suspect; 10% is the ceiling
	// beyond which even an honest token can't be LP'd profitably.
	MaxSellTaxPct = 10.0
)

var safetyClient = &http.Client{Timeout: 8 * time.Second}

// Security is the subset of GMGN /v1/token/security the venue acts on.
// Tri-state ints mirror the API: -1 unknown, 0 negative, 1 positive.
type Security struct {
	Honeypot   int      // data.honeypot
	Blacklist  int      // data.blacklist
	SellTaxPct *float64 // data.sell_tax x 100, nil when absent/unparseable
	BuyTaxPct  *float64
	OpenSource *bool
}

// TokenInfo is the holder-quality subset of GMGN /v1/token/info, the same
// fields the Solana venue gates on (rat/bundler volume share) plus the
// launchpad attribution the EVM route exposes.
type TokenInfo struct {
	SmartWallets     *int
	BundlerWallets   *int
	RatVolumePct     *float64
	BundlerVolumePct *float64
	Top10Pct         *float64
	DevStatus        string
	Launchpad        string
}

// clientID returns a random v4-format UUID for GMGN's anti-replay param.
func clientID() string {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		return ""
	}
	b[6] = (b[6] & 0x0f) | 0x40
	b[8] = (b[8] & 0x3f) | 0x80
	s := hex.EncodeToString(b[:])
	return s[:8] + "-" + s[8:12] + "-" + s[12:16] + "-" + s[16:20] + "-" + s[20:]
}

// gmgnGet performs one exist-auth GMGN OpenAPI call and decodes data into out.
// ok=false on any transport/API error (caller fails open).
func gmgnGet(endpoint, apiKey, address string, nowUnix int64, out interface{}) bool {
	if apiKey == "" {
		return false
	}
	cid := clientID()
	if cid == "" {
		return false
	}
	q := url.Values{}
	q.Set("chain", Chain)
	q.Set("address", address)
	q.Set("timestamp", fmt.Sprintf("%d", nowUnix))
	q.Set("client_id", cid)

	req, err := http.NewRequest(http.MethodGet, endpoint+"?"+q.Encode(), nil)
	if err != nil {
		return false
	}
	req.Header.Set("X-APIKEY", apiKey)
	req.Header.Set("Accept", "application/json")
	resp, err := safetyClient.Do(req)
	if err != nil {
		return false
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		io.Copy(io.Discard, io.LimitReader(resp.Body, 512))
		return false
	}
	var env struct {
		Code int             `json:"code"`
		Data json.RawMessage `json:"data"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&env); err != nil || env.Code != 0 {
		return false
	}
	return json.Unmarshal(env.Data, out) == nil
}

// ratioPct converts a "0.28"-style ratio string to a percent pointer;
// empty/unparseable returns nil (fail-open).
func ratioPct(s string) *float64 {
	if s == "" {
		return nil
	}
	f, err := strconv.ParseFloat(s, 64)
	if err != nil {
		return nil
	}
	pct := f * 100
	return &pct
}

// FetchSecurity returns the GMGN contract-security snapshot for a token.
// ok=false means unavailable — caller passes the candidate through (fail-open).
func FetchSecurity(apiKey, address string, nowUnix int64) (*Security, bool) {
	var d struct {
		Honeypot   int    `json:"honeypot"`  // -1 unknown / 0 no / 1 yes
		Blacklist  int    `json:"blacklist"` // same tri-state
		SellTax    string `json:"sell_tax"`
		BuyTax     string `json:"buy_tax"`
		OpenSource *int   `json:"open_source"`
	}
	if !gmgnGet(gmgnSecurityURL, apiKey, address, nowUnix, &d) {
		return nil, false
	}
	sec := &Security{
		Honeypot:   d.Honeypot,
		Blacklist:  d.Blacklist,
		SellTaxPct: ratioPct(d.SellTax),
		BuyTaxPct:  ratioPct(d.BuyTax),
	}
	if d.OpenSource != nil {
		v := *d.OpenSource == 1
		sec.OpenSource = &v
	}
	return sec, true
}

// SecurityReject returns a non-empty reason on a POSITIVE security detection.
// Unknown values (-1 / nil) never reject — the fail-closed arm only fires on
// affirmative evidence, keeping the fail-open contract for missing data.
func SecurityReject(s *Security) string {
	if s == nil {
		return ""
	}
	if s.Honeypot == 1 {
		return "honeypot detected"
	}
	if s.Blacklist == 1 {
		return "blacklist function detected"
	}
	if s.SellTaxPct != nil && *s.SellTaxPct > MaxSellTaxPct {
		return fmt.Sprintf("sell tax %.1f%% > %.1f%%", *s.SellTaxPct, MaxSellTaxPct)
	}
	return ""
}

// FetchTokenInfo returns the GMGN holder-quality snapshot. ok=false = fail-open.
func FetchTokenInfo(apiKey, address string, nowUnix int64) (*TokenInfo, bool) {
	var d struct {
		Launchpad  string `json:"launchpad"`
		WalletTags *struct {
			SmartWallets   *int `json:"smart_wallets"`
			BundlerWallets *int `json:"bundler_wallets"`
		} `json:"wallet_tags_stat"`
		Stat *struct {
			TopRatTraderPct     string `json:"top_rat_trader_percentage"`
			TopBundlerTraderPct string `json:"top_bundler_trader_percentage"`
			Top10HolderRate     string `json:"top_10_holder_rate"`
		} `json:"stat"`
		Dev *struct {
			CreatorTokenStatus string `json:"creator_token_status"`
		} `json:"dev"`
	}
	if !gmgnGet(gmgnTokenInfoURL, apiKey, address, nowUnix, &d) {
		return nil, false
	}
	info := &TokenInfo{Launchpad: d.Launchpad}
	if d.WalletTags != nil {
		info.SmartWallets = d.WalletTags.SmartWallets
		info.BundlerWallets = d.WalletTags.BundlerWallets
	}
	if d.Stat != nil {
		info.RatVolumePct = ratioPct(d.Stat.TopRatTraderPct)
		info.BundlerVolumePct = ratioPct(d.Stat.TopBundlerTraderPct)
		info.Top10Pct = ratioPct(d.Stat.Top10HolderRate)
	}
	if d.Dev != nil {
		info.DevStatus = d.Dev.CreatorTokenStatus
	}
	return info, true
}

// HolderQualityReject applies the same insider/bundler volume caps as the
// Solana venue (meteora.GmgnReject). Nil fields never reject; a cap <= 0
// disables that check.
func HolderQualityReject(t *TokenInfo, maxRatPct, maxBundlerPct float64) string {
	if t == nil {
		return ""
	}
	if maxRatPct > 0 && t.RatVolumePct != nil && *t.RatVolumePct > maxRatPct {
		return fmt.Sprintf("insider volume %.1f%% > %.1f%%", *t.RatVolumePct, maxRatPct)
	}
	if maxBundlerPct > 0 && t.BundlerVolumePct != nil && *t.BundlerVolumePct > maxBundlerPct {
		return fmt.Sprintf("bundler volume %.1f%% > %.1f%%", *t.BundlerVolumePct, maxBundlerPct)
	}
	return ""
}

// FetchHolders returns the Blockscout holder count for a token contract.
// ok=false = fetch failed (fail-open). No API key required.
func FetchHolders(address string) (int, bool) {
	req, err := http.NewRequest(http.MethodGet, blockscoutAPI+url.PathEscape(address), nil)
	if err != nil {
		return 0, false
	}
	req.Header.Set("Accept", "application/json")
	resp, err := safetyClient.Do(req)
	if err != nil {
		return 0, false
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		io.Copy(io.Discard, io.LimitReader(resp.Body, 512))
		return 0, false
	}
	var d struct {
		HoldersCount string `json:"holders_count"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&d); err != nil {
		return 0, false
	}
	n, err := strconv.Atoi(d.HoldersCount)
	if err != nil {
		return 0, false
	}
	return n, true
}

// ApplySecurity attaches the security snapshot to the outgoing candidate.
func (c *Candidate) ApplySecurity(s *Security) {
	if s == nil {
		return
	}
	c.GmgnSellTaxPct = s.SellTaxPct
	c.GmgnBuyTaxPct = s.BuyTaxPct
	c.GmgnOpenSource = s.OpenSource
}

// ApplyTokenInfo attaches the holder-quality snapshot to the candidate.
func (c *Candidate) ApplyTokenInfo(t *TokenInfo) {
	if t == nil {
		return
	}
	c.GmgnSmartWallets = t.SmartWallets
	c.GmgnBundlerWallets = t.BundlerWallets
	c.GmgnRatVolumePct = t.RatVolumePct
	c.GmgnBundlerVolumePct = t.BundlerVolumePct
	c.GmgnTop10Pct = t.Top10Pct
	c.GmgnDevStatus = t.DevStatus
	c.GmgnLaunchpad = t.Launchpad
}
