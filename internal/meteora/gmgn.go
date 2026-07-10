package meteora

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"strconv"
	"time"
)

// GMGN OpenAPI token enrichment. One extra HTTP call per FRESH candidate
// (after dedup, same budget discipline as the momentum/audit gates).
// Advisory only — like the PVP check it NEVER rejects; it attaches
// holder-quality signals (smart-money count, insider/bundler volume share,
// dev track record) the discovery API cannot see, so the agent ranks
// smart-money-backed pools above bot-farmed ones. Everything fails OPEN.
//
// Auth: token routes use "exist auth" — X-APIKEY header plus timestamp +
// client_id query params (timestamp valid ±5s, client_id is an anti-replay
// UUID). No request signature needed for read-only routes. Rate limit is a
// 20 req/s leaky bucket; /v1/token/info has weight 1, far above batch sizes.
const gmgnTokenInfoURL = "https://openapi.gmgn.ai/v1/token/info"

// GmgnInfo is the subset of /v1/token/info the daemon forwards.
// Pointers distinguish "absent from the API" (nil, fail-open) from zero.
type GmgnInfo struct {
	SmartWallets     *int     // wallet_tags_stat.smart_wallets — proven profitable holders
	KolWallets       *int     // wallet_tags_stat.renowned_wallets — tracked KOL/fund holders
	SniperWallets    *int     // wallet_tags_stat.sniper_wallets — launch snipers
	BundlerWallets   *int     // wallet_tags_stat.bundler_wallets — bot-bundled buys
	RatVolumePct     *float64 // stat.top_rat_trader_percentage × 100 — insider volume share
	BundlerVolumePct *float64 // stat.top_bundler_trader_percentage × 100
	Top10Rate        *float64 // stat.top_10_holder_rate × 100 — top-10 supply share
	DevStatus        string   // dev.creator_token_status: creator_hold / creator_sell / ...
	DevTokensCreated *int     // dev.creator_open_count — serial-deployer signal
}

// gmgnTokenInfo mirrors the fields we read from /v1/token/info. The stat
// ratios arrive as JSON strings ("0.0013"), hence string types + parse.
type gmgnTokenInfo struct {
	WalletTags *struct {
		SmartWallets    *int `json:"smart_wallets"`
		RenownedWallets *int `json:"renowned_wallets"`
		SniperWallets   *int `json:"sniper_wallets"`
		BundlerWallets  *int `json:"bundler_wallets"`
	} `json:"wallet_tags_stat"`
	Stat *struct {
		TopRatTraderPct     string `json:"top_rat_trader_percentage"`
		TopBundlerTraderPct string `json:"top_bundler_trader_percentage"`
		Top10HolderRate     string `json:"top_10_holder_rate"`
	} `json:"stat"`
	Dev *struct {
		CreatorTokenStatus string `json:"creator_token_status"`
		CreatorOpenCount   *int   `json:"creator_open_count"`
	} `json:"dev"`
}

// gmgnEnvelope is the OpenAPI response wrapper; code != 0 means API error.
type gmgnEnvelope struct {
	Code int             `json:"code"`
	Data json.RawMessage `json:"data"`
}

var gmgnClient = &http.Client{Timeout: 8 * time.Second}

// gmgnClientID returns a random v4-format UUID for the anti-replay param.
func gmgnClientID() string {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		return ""
	}
	b[6] = (b[6] & 0x0f) | 0x40
	b[8] = (b[8] & 0x3f) | 0x80
	s := hex.EncodeToString(b[:])
	return s[:8] + "-" + s[8:12] + "-" + s[12:16] + "-" + s[16:20] + "-" + s[20:]
}

// parseRatioPct converts a "0.1615"-style ratio string to a percent pointer.
// Unparseable or empty input returns nil (fail-open).
func parseRatioPct(s string) *float64 {
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

// FetchGmgn returns the GMGN holder-quality snapshot for a mint. ok=false
// means the data could not be fetched (treat as pass — fail-open, same
// contract as GetMomentum/FetchAudit). nowUnix comes from the caller: the
// API validates the timestamp within ±5s, and clock reads stay at the edges.
func FetchGmgn(apiKey, mint string, nowUnix int64) (*GmgnInfo, bool) {
	if apiKey == "" {
		return nil, false
	}
	cid := gmgnClientID()
	if cid == "" {
		return nil, false
	}
	q := url.Values{}
	q.Set("chain", "sol")
	q.Set("address", mint)
	q.Set("timestamp", fmt.Sprintf("%d", nowUnix))
	q.Set("client_id", cid)

	req, err := http.NewRequest(http.MethodGet, gmgnTokenInfoURL+"?"+q.Encode(), nil)
	if err != nil {
		return nil, false
	}
	req.Header.Set("X-APIKEY", apiKey)
	req.Header.Set("Accept", "application/json")
	resp, err := gmgnClient.Do(req)
	if err != nil {
		return nil, false
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, false
	}

	var env gmgnEnvelope
	if err := json.NewDecoder(resp.Body).Decode(&env); err != nil || env.Code != 0 {
		return nil, false
	}
	var ti gmgnTokenInfo
	if err := json.Unmarshal(env.Data, &ti); err != nil {
		return nil, false
	}

	info := &GmgnInfo{}
	if ti.WalletTags != nil {
		info.SmartWallets = ti.WalletTags.SmartWallets
		info.KolWallets = ti.WalletTags.RenownedWallets
		info.SniperWallets = ti.WalletTags.SniperWallets
		info.BundlerWallets = ti.WalletTags.BundlerWallets
	}
	if ti.Stat != nil {
		info.RatVolumePct = parseRatioPct(ti.Stat.TopRatTraderPct)
		info.BundlerVolumePct = parseRatioPct(ti.Stat.TopBundlerTraderPct)
		info.Top10Rate = parseRatioPct(ti.Stat.Top10HolderRate)
	}
	if ti.Dev != nil {
		info.DevStatus = ti.Dev.CreatorTokenStatus
		info.DevTokensCreated = ti.Dev.CreatorOpenCount
	}
	return info, true
}

// GmgnReject returns a non-empty reason when the snapshot shows disqualifying
// holder quality: insider ("rat trader") or bundler volume share above the
// caps. These two are the strongest pre-rug signals GMGN exposes — all three
// -100% closes in the journal were tokens whose volume was dominated by
// insiders/bundlers that discovery-side gates couldn't see. Nil fields never
// reject (fail-open, same contract as every other gate); a cap <= 0 disables
// that check.
func GmgnReject(g *GmgnInfo, maxRatPct, maxBundlerPct float64) string {
	if g == nil {
		return ""
	}
	if maxRatPct > 0 && g.RatVolumePct != nil && *g.RatVolumePct > maxRatPct {
		return fmt.Sprintf("insider volume %.1f%% > %.1f%%", *g.RatVolumePct, maxRatPct)
	}
	if maxBundlerPct > 0 && g.BundlerVolumePct != nil && *g.BundlerVolumePct > maxBundlerPct {
		return fmt.Sprintf("bundler volume %.1f%% > %.1f%%", *g.BundlerVolumePct, maxBundlerPct)
	}
	return ""
}

// ApplyGmgn attaches the GMGN snapshot to the outgoing candidate payload so
// the agent judges holder quality on-screen instead of re-fetching it.
func (c *Candidate) ApplyGmgn(g *GmgnInfo) {
	if g == nil {
		return
	}
	c.GmgnSmartWallets = g.SmartWallets
	c.GmgnKolWallets = g.KolWallets
	c.GmgnSniperWallets = g.SniperWallets
	c.GmgnBundlerWallets = g.BundlerWallets
	c.GmgnRatVolumePct = g.RatVolumePct
	c.GmgnBundlerVolumePct = g.BundlerVolumePct
	c.GmgnTop10Pct = g.Top10Rate
	c.GmgnDevStatus = g.DevStatus
	c.GmgnDevTokensCreated = g.DevTokensCreated
}
