package meteora

import (
	"encoding/json"
	"log"
	"net/http"
	"net/url"
	"sort"
	"strings"
)

// PVP (same-symbol rival) detection. Meme launches often spawn several tokens
// under one ticker; when an ESTABLISHED rival token (real holders, real fees,
// its own live DLMM pool) shares the candidate's symbol, the two are fighting
// for the same liquidity and attention — entering the weaker side is how you
// end up LPing the loser. Ported from Meridian's enrichPvpRisk.
//
// This only FLAGS candidates (is_pvp + rival stats in the payload) so the
// agent can compare the pick against its rival; it never rejects. Everything
// fails open: search errors or missing fields leave the candidate unflagged.
const meteoraPoolSearchURL = "https://dlmm.datapi.meteora.ag/pools"

// Rival legitimacy thresholds (screening thresholds live in this package by
// convention, values ported from Meridian). A "rival" below these is a dust
// copycat, not a war.
const (
	pvpRivalLimit       = 2      // strongest same-symbol assets to consider
	pvpMinActiveTVL     = 5000.0 // rival's DLMM pool must hold this much TVL
	pvpMinHolders       = 500.0  // rival token holder floor
	pvpMinGlobalFeesSOL = 30.0   // rival token global fees floor (SOL)
)

// pvpAsset mirrors the fields we read from a Jupiter /assets/search result
// when searching by SYMBOL (unlike audit.go's by-mint lookup).
type pvpAsset struct {
	ID          string  `json:"id"`
	Symbol      string  `json:"symbol"`
	Name        string  `json:"name"`
	HolderCount float64 `json:"holderCount"`
	Fees        float64 `json:"fees"`
	Liquidity   float64 `json:"liquidity"`
}

// rivalPoolSearch mirrors the Meteora DLMM pool-search response (a different
// API from the discovery endpoint — field is `address`, not `pool_address`).
type rivalPoolSearch struct {
	Data []struct {
		Address string  `json:"address"`
		TVL     float64 `json:"tvl"`
		TokenX  struct {
			Address string `json:"address"`
		} `json:"token_x"`
		TokenY struct {
			Address string `json:"address"`
		} `json:"token_y"`
	} `json:"data"`
}

func searchAssetsBySymbol(symbol string) []pvpAsset {
	req, err := http.NewRequest(http.MethodGet, jupAssetSearchURL+"?query="+url.QueryEscape(symbol), nil)
	if err != nil {
		return nil
	}
	// Cloudflare 403s Go's default User-Agent; a curl UA passes (see audit.go).
	req.Header.Set("User-Agent", "curl/8.5.0")
	req.Header.Set("Accept", "application/json")
	resp, err := auditClient.Do(req)
	if err != nil {
		return nil
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil
	}
	var assets []pvpAsset
	if err := json.NewDecoder(resp.Body).Decode(&assets); err != nil {
		return nil
	}
	return assets
}

// findRivalPool returns the rival token's strongest live DLMM pool, if any
// clears the TVL floor. A rival token without its own pool is not a PVP
// threat — there is no second pool splitting the LP flow.
func findRivalPool(mint string) (address string, tvl float64, ok bool) {
	q := url.Values{}
	q.Set("query", mint)
	q.Set("sort_by", "tvl:desc")
	q.Set("filter_by", "tvl>5000")
	req, err := http.NewRequest(http.MethodGet, meteoraPoolSearchURL+"?"+q.Encode(), nil)
	if err != nil {
		return "", 0, false
	}
	req.Header.Set("User-Agent", "curl/8.5.0")
	resp, err := auditClient.Do(req)
	if err != nil {
		return "", 0, false
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return "", 0, false
	}
	var out rivalPoolSearch
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return "", 0, false
	}
	for _, p := range out.Data {
		if (p.TokenX.Address == mint || p.TokenY.Address == mint) && p.TVL >= pvpMinActiveTVL {
			return p.Address, p.TVL, true
		}
	}
	return "", 0, false
}

// EnrichPVP flags every batch candidate whose symbol is contested by an
// established rival token with its own live DLMM pool. Returns how many were
// flagged. One Jupiter search per unique symbol (cached across the batch),
// plus one pool lookup per legitimate rival — the batch is small (post-gate),
// so this stays within the same request budget as the audit gate.
func EnrichPVP(batch []*Candidate) int {
	cache := map[string][]pvpAsset{}
	flagged := 0
	for _, c := range batch {
		symbol := strings.ToUpper(strings.TrimSpace(c.BaseSymbol))
		if symbol == "" || c.BaseMint == "" {
			continue
		}
		assets, seen := cache[symbol]
		if !seen {
			assets = searchAssetsBySymbol(symbol)
			cache[symbol] = assets
		}

		var rivals []pvpAsset
		for _, a := range assets {
			if strings.ToUpper(strings.TrimSpace(a.Symbol)) == symbol && a.ID != "" && a.ID != c.BaseMint {
				rivals = append(rivals, a)
			}
		}
		sort.Slice(rivals, func(i, j int) bool { return rivals[i].Liquidity > rivals[j].Liquidity })
		if len(rivals) > pvpRivalLimit {
			rivals = rivals[:pvpRivalLimit]
		}

		for _, r := range rivals {
			if r.HolderCount < pvpMinHolders || r.Fees < pvpMinGlobalFeesSOL {
				continue
			}
			pool, tvl, ok := findRivalPool(r.ID)
			if !ok {
				continue
			}
			c.IsPVP = true
			c.PVPRivalName = r.Name
			c.PVPRivalMint = r.ID
			c.PVPRivalPool = pool
			c.PVPRivalTVL = tvl
			c.PVPRivalHolders = r.HolderCount
			c.PVPRivalFeesSOL = r.Fees
			flagged++
			log.Printf("meteora: PVP guard: %s (%s) contested by rival %s (%s) pool tvl $%.0f",
				c.BaseSymbol, c.Pool[:8], r.Name, r.ID[:8], tvl)
			break
		}
	}
	return flagged
}
