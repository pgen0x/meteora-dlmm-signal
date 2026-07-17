package robinhood

import (
	"encoding/json"
	"fmt"
	"io"
	"log"
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
// At the observed ~7 launches/min, 3 pages ≈ 8 minutes of history — enough
// for a pool to clear Fresh.MinAge (3m) before scrolling out of reach.
// Why not more: the keyless GT tier 429s well below its documented 30 req/min
// (likely per-IP budget shared across this VM's egress) — observed 429s from
// request ~5 onward regardless of 0.3-1.2s spacing, so the real budget is
// ~4 req/min. 3 pages + periodic trending stays inside it.
const newPoolPages = 3

// trendingEvery fetches trending_pools only every Nth cycle: it changes
// slowly, mostly yields too-old rejects, and each skipped fetch buys budget
// for the new_pools pages that carry the fresh-band thesis.
const trendingEvery = 5

// cycleCount tracks FetchNewPools invocations for the trending cadence.
// Unsynchronized on purpose: the scanner calls this from one goroutine.
var cycleCount int

var discoverClient = &http.Client{Timeout: 15 * time.Second}

// feePctRe captures a trailing fee-tier suffix in a GeckoTerminal pool name,
// e.g. "CALLIE / WETH 0.3%" or "USDG / XIAO 87%". v4 names often omit the
// suffix entirely — fillV4Meta overwrites the parse with the gateway's
// authoritative feeTier for those.
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
// returns them decoded and unit-normalized, restricted to Uniswap v3 and v4
// pools (v4 entries carry a bytes32 poolId in the address field and get their
// hook / fee metadata from one extra gateway call — see fillV4Meta). Page
// errors after the first successful page are tolerated: a partial view still
// yields a usable cycle.
func FetchNewPools(baseURL string) ([]Pool, error) {
	if baseURL == "" {
		baseURL = DefaultDiscoverURL
	}
	urls := make([]string, 0, newPoolPages+1)
	for page := 1; page <= newPoolPages; page++ {
		urls = append(urls, fmt.Sprintf("%s?include=base_token%%2Cquote_token&page=%d", baseURL, page))
	}
	if cycleCount%trendingEvery == 0 {
		urls = append(urls, trendingURL+"?include=base_token%2Cquote_token&page=1")
	}
	cycleCount++

	tokens := map[string]gtToken{}
	var data []gtPool
	failed := 0
	for i, u := range urls {
		gr, err := fetchPage(u)
		if err != nil {
			if i == 0 {
				return nil, err
			}
			// Tolerated (a partial view still yields a usable cycle) but
			// LOGGED: silent page drops made fetched= swing 63→5 between
			// cycles and read as launch-velocity noise instead of GT throttling.
			failed++
			log.Printf("robinhood: page %d/%d fetch failed (continuing partial): %v", i+1, len(urls), err)
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
		// Space page requests out: the public tier throttles bursts well
		// before the documented 30 req/min average (observed: 429s on pages
		// 5-6 even at 300ms spacing; 1.2s clears a 60s cycle with margin).
		if i < len(urls)-1 {
			time.Sleep(1200 * time.Millisecond)
		}
	}
	if failed > 0 {
		log.Printf("robinhood: %d/%d discovery pages failed this cycle", failed, len(urls))
	}

	seen := map[string]bool{}
	pools := make([]Pool, 0, len(data))
	for _, gp := range data {
		if seen[gp.Attrs.Address] {
			continue
		}
		seen[gp.Attrs.Address] = true
		var protocol string
		switch {
		case strings.HasPrefix(gp.Relationships.Dex.Data.ID, "uniswap-v3"):
			protocol = "v3"
		case strings.HasPrefix(gp.Relationships.Dex.Data.ID, "uniswap-v4"):
			protocol = "v4" // Address is the bytes32 poolId, not a contract
		default:
			continue // v2 / bankr / virtuals — no executor will ever speak these
		}
		created, err := time.Parse(time.RFC3339, gp.Attrs.PoolCreatedAt)
		if err != nil {
			continue // unusable without an age; new_pools always sets it
		}
		base := tokens[gp.Relationships.BaseToken.Data.ID]
		quote := tokens[gp.Relationships.QuoteToken.Data.ID]

		pools = append(pools, Pool{
			Address:       gp.Attrs.Address,
			Name:          gp.Attrs.Name,
			Dex:           gp.Relationships.Dex.Data.ID,
			Protocol:      protocol,
			CreatedAt:     created,
			BaseAddress:   base.Attrs.Address,
			BaseSymbol:    base.Attrs.Symbol,
			BaseDecimals:  base.Attrs.Decimals,
			QuoteAddress:  quote.Attrs.Address,
			QuoteSymbol:   quote.Attrs.Symbol,
			QuoteDecimals: quote.Attrs.Decimals,
			FeePct:        parseFeePct(gp.Attrs.Name),
			ReserveUSD:    pfloat(gp.Attrs.ReserveUSD),
			FdvUSD:        pfloat(gp.Attrs.FdvUSD),
			McapUSD:       pfloat(gp.Attrs.MarketCapUSD),
			VolumeH1USD:   pfloat(gp.Attrs.VolumeUSD.H1),
			VolumeH24USD:  pfloat(gp.Attrs.VolumeUSD.H24),
			TxH1:          gp.Attrs.Transactions.H1,
			TxH24:         gp.Attrs.Transactions.H24,
			ChangeM5Pct:   pfloat(gp.Attrs.PriceChangePct.M5),
			ChangeH1Pct:   pfloat(gp.Attrs.PriceChangePct.H1),
			ChangeH6Pct:   pfloat(gp.Attrs.PriceChangePct.H6),
			ChangeH24Pct:  pfloat(gp.Attrs.PriceChangePct.H24),
		})
	}
	return fillV4Meta(pools), nil
}

// fillV4Meta resolves hook / dynamic-fee / true fee tier for the batch's v4
// pools with one aliased gateway call (GeckoTerminal carries none of them, and
// v4 names often omit the fee suffix parseFeePct depends on).
//
// Fail-closed per pool: a v4 pool whose meta cannot be resolved this cycle is
// dropped, because an unverified hook must never pass the hooked-pool gate by
// looking hookless. This is the venue's second deliberate fail-closed
// divergence (the first: GMGN positive honeypot detection). Dropped pools are
// not marked seen, so a live pool retries next cycle.
func fillV4Meta(pools []Pool) []Pool {
	var ids []string
	for _, p := range pools {
		if p.Protocol == "v4" {
			ids = append(ids, p.Address)
		}
	}
	if len(ids) == 0 {
		return pools
	}

	meta, err := fetchV4Meta(ids)
	if err != nil {
		log.Printf("robinhood: v4 meta fetch failed, dropping %d v4 pool(s) this cycle (fail-closed): %v", len(ids), err)
		meta = map[string]v4Meta{}
	}

	kept := pools[:0]
	dropped := 0
	for _, p := range pools {
		if p.Protocol != "v4" {
			kept = append(kept, p)
			continue
		}
		m, ok := meta[strings.ToLower(p.Address)]
		if !ok {
			dropped++
			continue
		}
		p.FeePct = m.FeeTier / feeTierDenom
		p.Hook = hookAddress(m.Hook)
		p.DynamicFee = m.IsDynamicFee
		kept = append(kept, p)
	}
	if dropped > 0 {
		log.Printf("robinhood: dropped %d v4 pool(s) with unresolved meta this cycle", dropped)
	}
	return kept
}
