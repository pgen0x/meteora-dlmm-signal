package meteora

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"time"
)

// Jupiter datapi token audit. One extra HTTP call per FRESH candidate (after
// dedup, like the momentum gate) so already-seen pools never re-hit it.
// Everything here fails OPEN: a missing field, HTTP error or unknown mint must
// never reject a pool — the discovery API already gated the hard fundamentals.
const jupAssetSearchURL = "https://datapi.jup.ag/v1/assets/search"

// Audit gate threshold (screening thresholds live in this package by
// convention). Bot-holder ceiling ported from the reference bot's post-recon hard
// filter. the reference bot also hard-rejects on global fees < 30 SOL ("bundled/scam"),
// but Jupiter's `fees` figure runs slightly off the accurate (GMGN) number, so
// the daemon only SHIPS global_fees_sol in the payload and leaves that reject
// to the agent prompt, where the value is visible next to the decision.
const maxBotHoldersPct = 30.0

// AuditInfo is the subset of the Jupiter asset audit the screen uses.
// Pointers distinguish "absent from the API" (nil, fail-open) from zero.
type AuditInfo struct {
	BotHoldersPct *float64
	TopHoldersPct *float64
	GlobalFeesSOL *float64
	Dev           string // deployer wallet — feeds the pipeline's dev blocklist
}

// jupAsset mirrors the fields we read from one /assets/search result.
type jupAsset struct {
	ID    string   `json:"id"`
	Dev   string   `json:"dev"`
	Fees  *float64 `json:"fees"`
	Audit *struct {
		TopHoldersPercentage *float64 `json:"topHoldersPercentage"`
		BotHoldersPercentage *float64 `json:"botHoldersPercentage"`
	} `json:"audit"`
}

var auditClient = &http.Client{Timeout: 8 * time.Second}

// FetchAudit returns the Jupiter audit for a mint. ok=false means the data
// could not be fetched (treat as pass — fail-open, same contract as GetMomentum).
func FetchAudit(mint string) (*AuditInfo, bool) {
	req, err := http.NewRequest(http.MethodGet, jupAssetSearchURL+"?query="+url.QueryEscape(mint), nil)
	if err != nil {
		return nil, false
	}
	// Cloudflare 403s Go's default User-Agent; a curl UA passes.
	req.Header.Set("User-Agent", "curl/8.5.0")
	req.Header.Set("Accept", "application/json")
	resp, err := auditClient.Do(req)
	if err != nil {
		return nil, false
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, false
	}

	var assets []jupAsset
	if err := json.NewDecoder(resp.Body).Decode(&assets); err != nil || len(assets) == 0 {
		return nil, false
	}

	// Exact mint match required; the search endpoint can return lookalikes.
	asset := assets[0]
	for _, a := range assets {
		if a.ID == mint {
			asset = a
			break
		}
	}
	if asset.ID != mint {
		return nil, false
	}

	info := &AuditInfo{GlobalFeesSOL: asset.Fees, Dev: asset.Dev}
	if asset.Audit != nil {
		info.BotHoldersPct = asset.Audit.BotHoldersPercentage
		info.TopHoldersPct = asset.Audit.TopHoldersPercentage
	}
	return info, true
}

// AuditReject returns a non-empty reason when the audit hard-fails a token.
// nil fields pass (fail-open).
func AuditReject(a *AuditInfo) string {
	if a == nil {
		return ""
	}
	if a.BotHoldersPct != nil && *a.BotHoldersPct > maxBotHoldersPct {
		return fmt.Sprintf("bot holders %.1f%% > %.0f%%", *a.BotHoldersPct, maxBotHoldersPct)
	}
	return ""
}

// ApplyAudit attaches the audit figures to the outgoing candidate payload so
// the agent judges with them on-screen instead of re-fetching.
func (c *Candidate) ApplyAudit(a *AuditInfo) {
	if a == nil {
		return
	}
	c.BotHoldersPct = a.BotHoldersPct
	c.GlobalFeesSOL = a.GlobalFeesSOL
	c.Dev = a.Dev
}
