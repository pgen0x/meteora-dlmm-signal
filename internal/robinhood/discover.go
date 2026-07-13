package robinhood

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"regexp"
	"strconv"
	"strings"
	"time"
)

// DefaultDiscoverURL is the GeckoTerminal new_pools endpoint for the venue.
// Public tier is rate-limited to 30 req/min across ALL GeckoTerminal calls —
// newPoolPages+1 requests per cycle stays far under it, but never fan out
// per-pool calls here.
const DefaultDiscoverURL = "https://api.geckoterminal.com/api/v2/networks/robinhood/new_pools"

// trendingURL complements new_pools: launch velocity on this chain is so high
// (~7 pools/min observed 2026-07-13) that one new_pools page spans ~2-3
// minutes — a pool old enough to clear MinAge has already scrolled off.
// Paginating buys ~10 minutes of history; trending catches the older
// (age <= MaxAge) pools that gained real traction after scrolling off.
const trendingURL = "https://api.geckoterminal.com/api/v2/networks/robinhood/trending_pools"

// newPoolPages is how many new_pools pages to fetch per cycle (20 pools/page).
// At the observed ~7 launches/min, 5 pages ≈ 13 minutes of history — deep
// enough that a pool can clear Fresh.MinAge before scrolling out of reach.
const newPoolPages = 5

var discoverClient = &http.Client{Timeout: 15 * time.Second}

// feePctRe captures a trailing fee-tier suffix in a GeckoTerminal pool name,
// e.g. "CALLIE / WETH 0.3%" or "USDG / XIAO 87%" (v4 pools can carry odd
// tiers; the dex filter removes those before the value matters).
var feePctRe = regexp.MustCompile(`([0-9]+(?:\.[0-9]+)?)%\s*$`)

// parseFeePct extracts the fee tier percent from a pool name; 0 = unknown.
func parseFeePct(name string) float64 {
	m := feePctRe.FindStringSubmatch(name)
	if m == nil {
		return 0
	}
	f, err := strconv.ParseFloat(m[1], 64)
	if err != nil {
		return 0
	}
	return f
}

// pfloat parses a GeckoTerminal string-encoded number; empty/invalid = 0.
func pfloat(s string) float64 {
	if s == "" {
		return 0
	}
	f, err := strconv.ParseFloat(s, 64)
	if err != nil {
		return 0
	}
	return f
}

// fetchPage retrieves and decodes one GeckoTerminal pools page (new_pools or
// trending_pools — same JSON:API schema).
func fetchPage(url string) (*gtResponse, error) {
	req, err := http.NewRequest(http.MethodGet, url, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Accept", "application/json")
	resp, err := discoverClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		io.Copy(io.Discard, io.LimitReader(resp.Body, 512))
		return nil, fmt.Errorf("geckoterminal status %d", resp.StatusCode)
	}
	var gr gtResponse
	if err := json.NewDecoder(resp.Body).Decode(&gr); err != nil {
		return nil, fmt.Errorf("geckoterminal decode: %w", err)
	}
	return &gr, nil
}

// FetchNewPools queries GeckoTerminal for the venue's pools — newPoolPages
// pages of new_pools plus one trending_pools page, deduped by address — and
// returns them decoded and unit-normalized, restricted to Uniswap v3 pools
// (the only dex the Phase 2 executor will speak; v4 entries carry bytes32
// pool IDs, not contract addresses). Page errors after the first successful
// page are tolerated: a partial view still yields a usable cycle.
func FetchNewPools(baseURL string) ([]Pool, error) {
	if baseURL == "" {
		baseURL = DefaultDiscoverURL
	}
	urls := make([]string, 0, newPoolPages+1)
	for page := 1; page <= newPoolPages; page++ {
		urls = append(urls, fmt.Sprintf("%s?include=base_token%%2Cquote_token&page=%d", baseURL, page))
	}
	urls = append(urls, trendingURL+"?include=base_token%2Cquote_token&page=1")

	tokens := map[string]gtToken{}
	var data []gtPool
	var firstErr error
	for i, u := range urls {
		gr, err := fetchPage(u)
		if err != nil {
			if i == 0 {
				return nil, err
			}
			if firstErr == nil {
				firstErr = err
			}
			continue
		}
		data = append(data, gr.Data...)
		// Index included token resources by JSON:API id for relationship lookup.
		for _, raw := range gr.Included {
			var t gtToken
			if err := json.Unmarshal(raw, &t); err == nil && t.Type == "token" {
				tokens[t.ID] = t
			}
		}
	}

	seen := map[string]bool{}
	pools := make([]Pool, 0, len(data))
	for _, gp := range data {
		if seen[gp.Attrs.Address] {
			continue
		}
		seen[gp.Attrs.Address] = true
		if !strings.HasPrefix(gp.Relationships.Dex.Data.ID, "uniswap-v3") {
			continue
		}
		created, err := time.Parse(time.RFC3339, gp.Attrs.PoolCreatedAt)
		if err != nil {
			continue // unusable without an age; new_pools always sets it
		}
		base := tokens[gp.Relationships.BaseToken.Data.ID]
		quote := tokens[gp.Relationships.QuoteToken.Data.ID]

		pools = append(pools, Pool{
			Address:      gp.Attrs.Address,
			Name:         gp.Attrs.Name,
			Dex:          gp.Relationships.Dex.Data.ID,
			CreatedAt:    created,
			BaseAddress:  base.Attrs.Address,
			BaseSymbol:   base.Attrs.Symbol,
			BaseDecimals: base.Attrs.Decimals,
			QuoteAddress: quote.Attrs.Address,
			QuoteSymbol:  quote.Attrs.Symbol,
			FeePct:       parseFeePct(gp.Attrs.Name),
			ReserveUSD:   pfloat(gp.Attrs.ReserveUSD),
			FdvUSD:       pfloat(gp.Attrs.FdvUSD),
			McapUSD:      pfloat(gp.Attrs.MarketCapUSD),
			VolumeH1USD:  pfloat(gp.Attrs.VolumeUSD.H1),
			VolumeH24USD: pfloat(gp.Attrs.VolumeUSD.H24),
			TxH1:         gp.Attrs.Transactions.H1,
			TxH24:        gp.Attrs.Transactions.H24,
			ChangeM5Pct:  pfloat(gp.Attrs.PriceChangePct.M5),
			ChangeH1Pct:  pfloat(gp.Attrs.PriceChangePct.H1),
			ChangeH6Pct:  pfloat(gp.Attrs.PriceChangePct.H6),
			ChangeH24Pct: pfloat(gp.Attrs.PriceChangePct.H24),
		})
	}
	return pools, nil
}
