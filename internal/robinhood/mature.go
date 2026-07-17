package robinhood

import (
	"fmt"
	"log"
	"net/http"
	"strings"
	"time"
)

// gatewayURL is Uniswap's own interface GraphQL gateway — the backend behind
// app.uniswap.org's Explore ▸ Pools table. Keyless and unauthenticated
// (verified 2026-07-14), and it speaks `chain: ROBINHOOD` natively.
//
// It exists here because GeckoTerminal's new_pools feed CANNOT serve the
// mature thesis: it is a launch feed, and a pool that has been printing fees
// for three days scrolled off it long ago. Conversely the gateway cannot serve
// the Fresh thesis — see topPoolsFirst.
const gatewayURL = "https://interface.gateway.uniswap.org/v1/graphql"

// topPoolsFirst caps the gateway page (100 is also the schema's hard max on
// `first`). The number is deliberately larger than the venue needs: each
// leaderboard returns the ENTIRE indexed universe per protocol — 69 v3 and 80
// v4 pools on 2026-07-14, sorted by TVL descending, with ZERO pools younger
// than 24h. The gateway is a TVL leaderboard with an indexing lag, not a
// discovery feed. That is exactly the half of the venue GeckoTerminal is blind
// to, and exactly why Fresh must keep using new_pools.
const topPoolsFirst = 100

// maxEnrich bounds the GeckoTerminal enrichment call. GT's /pools/multi/ takes
// up to 30 comma-separated addresses in ONE request, which is the whole point
// of the prefilter: the keyless GT tier throttles at roughly 4 req/min (see
// discover.go), so a mature cycle must cost exactly one GT call, never a
// per-pool fan-out.
const maxEnrich = 30

// feeTierDenom converts a Uniswap v3 feeTier (hundredths of a bip: 500, 3000,
// 10000) to a percent (0.05, 0.3, 1.0).
const feeTierDenom = 10000.0

var gatewayClient = &http.Client{Timeout: 15 * time.Second}

// topPoolsQuery asks for everything the gateway can give us about a pool.
// Notably absent, and the reason enrichFromGT exists: h1 volume, the
// buys/sells/buyers/sellers breakdown, price-change windows, and FDV.
// `cumulativeVolume` rejects an HOUR duration on this schema — DAY is the
// finest window available.
const topPoolsQuery = `query TopV3Pools($chain: Chain!, $first: Int!) {
  topV3Pools(chain: $chain, first: $first) {
    address
    createdAtTimestamp
    feeTier
    totalLiquidity { value }
    cumulativeVolume(duration: DAY) { value }
    token0 { address symbol decimals }
    token1 { address symbol decimals }
  }
}`

// topV4PoolsQuery is the v4 sibling. There is no unified topPools query on
// this schema (verified 2026-07-14: FieldUndefined) — v2/v3/v4 each get their
// own root field. v4 adds the fields the protocol adds: the bytes32 poolId
// (pools live inside the singleton PoolManager, so there is no per-pool
// address), the hook, and the dynamic-fee flag. Native-ETH sides come back
// with a null token address.
const topV4PoolsQuery = `query TopV4Pools($chain: Chain!, $first: Int!) {
  topV4Pools(chain: $chain, first: $first) {
    poolId
    createdAtTimestamp
    feeTier
    isDynamicFee
    hook { address }
    totalLiquidity { value }
    cumulativeVolume(duration: DAY) { value }
    token0 { address symbol decimals }
    token1 { address symbol decimals }
  }
}`

// uniToken is one side of a gateway pool.
type uniToken struct {
	Address  string `json:"address"`
	Symbol   string `json:"symbol"`
	Decimals int    `json:"decimals"`
}

// uniAmount is the gateway's {value} money wrapper. Nil for pools it has not
// finished indexing — read it through val().
type uniAmount struct {
	Value float64 `json:"value"`
}

// uniPool is one entry of topV3Pools or topV4Pools. The v4-only fields
// (PoolID, IsDynamicFee, Hook) stay zero on v3 entries.
type uniPool struct {
	Address            string     `json:"address"`
	PoolID             string     `json:"poolId"` // v4: bytes32, no pool contract exists
	CreatedAtTimestamp int64      `json:"createdAtTimestamp"`
	FeeTier            float64    `json:"feeTier"`
	IsDynamicFee       bool       `json:"isDynamicFee"`
	TotalLiquidity     *uniAmount `json:"totalLiquidity"`
	CumulativeVolume   *uniAmount `json:"cumulativeVolume"`
	Token0             uniToken   `json:"token0"`
	Token1             uniToken   `json:"token1"`
	Hook               *struct {
		Address string `json:"address"`
	} `json:"hook"`
}

func (a *uniAmount) val() float64 {
	if a == nil {
		return 0
	}
	return a.Value
}

// FetchMaturePools discovers established fee-printing pools: pools too old for
// the Fresh window that are still generating outsized fee/TVL. It is the
// mature-mode analog of FetchNewPools and returns Pools ready for Screen.
//
// Two hops, three HTTP calls per cycle, on purpose:
//
//  1. Uniswap's gateway returns the venue's whole indexed universe — one call
//     per protocol (v3 + v4, no unified query exists) — with TVL, fee tier,
//     age, 24h volume and (v4) hook/dynamic-fee flags, but no h1 flow and no
//     FDV.
//  2. A LOCAL prefilter (no I/O) cuts that to the pools that could plausibly
//     pass, then ONE GeckoTerminal /pools/multi/ call fills in the h1
//     buys/sells/buyers, price-change windows and FDV that Screen gates on
//     (GT accepts v4 bytes32 poolIds in the same batch as v3 addresses).
//
// The prefilter is what keeps step 2 to a single request; without it this would
// fan out per-pool and burn the GT budget the Fresh mode depends on.
func FetchMaturePools(mp ModeParams, now time.Time) ([]Pool, error) {
	// Two gateway calls, one per protocol — no unified query exists. A v4
	// failure degrades to a v3-only cycle (logged) rather than blanking the
	// mode, mirroring discover.go's tolerated partial pages; only both
	// universes failing is fatal.
	rawV3, errV3 := fetchTopPools("v3")
	rawV4, errV4 := fetchTopPools("v4")
	if errV3 != nil && errV4 != nil {
		return nil, fmt.Errorf("gateway v3: %v; v4: %v", errV3, errV4)
	}
	for proto, err := range map[string]error{"v3": errV3, "v4": errV4} {
		if err != nil {
			log.Printf("robinhood: mature %s universe fetch failed (continuing with the other): %v", proto, err)
		}
	}

	shortlist := make([]Pool, 0, maxEnrich)
	add := func(raw []uniPool, protocol string) {
		for _, up := range raw {
			if len(shortlist) >= maxEnrich {
				// Each universe is TVL-sorted descending, so truncation drops
				// that protocol's SMALLEST survivors — the ones furthest from
				// the liquidity we want anyway. v3 fills first by convention.
				return
			}
			p, ok := toPool(up, protocol)
			if !ok {
				continue
			}
			if !prefilter(p, mp, now) {
				continue
			}
			shortlist = append(shortlist, p)
		}
	}
	add(rawV3, "v3")
	add(rawV4, "v4")
	if len(shortlist) == 0 {
		return nil, nil
	}

	if err := enrichFromGT(shortlist); err != nil {
		// Hard failure, unlike discover.go's tolerated partial pages: every
		// remaining Screen gate (txns, buyers, no-sells honeypot shape,
		// momentum, FDV) reads a field that ONLY the enrichment supplies.
		// Passing un-enriched pools on would silently gate on zeros.
		return nil, fmt.Errorf("mature enrich: %w", err)
	}
	return shortlist, nil
}

// fetchTopPools runs one protocol's gateway leaderboard query ("v3" or "v4")
// and returns the raw pool list.
func fetchTopPools(protocol string) ([]uniPool, error) {
	op, query := "TopV3Pools", topPoolsQuery
	if protocol == "v4" {
		op, query = "TopV4Pools", topV4PoolsQuery
	}
	var data struct {
		TopV3Pools []uniPool `json:"topV3Pools"`
		TopV4Pools []uniPool `json:"topV4Pools"`
	}
	err := gatewayQuery(op, query, map[string]any{
		"chain": strings.ToUpper(Chain),
		"first": topPoolsFirst,
	}, &data)
	if err != nil {
		return nil, err
	}
	pools := data.TopV3Pools
	if protocol == "v4" {
		pools = data.TopV4Pools
	}
	if len(pools) == 0 {
		return nil, fmt.Errorf("uniswap gateway returned no %s pools", protocol)
	}
	return pools, nil
}

// toPool maps a gateway pool onto the venue's Pool, orienting it so the base
// side is the token and the quote side is a whitelisted quote asset. The
// gateway orders token0 / token1 by address, not by role, so the quote asset
// lands on either side — Screen's quote check would reject half the universe
// if we assumed token1. Native-ETH sides arrive with a null address and are
// normalized to the NativeETH sentinel so the whitelist can name them.
//
// h1 flow, price changes and FDV stay zero here; enrichFromGT fills them.
func toPool(up uniPool, protocol string) (Pool, bool) {
	addr := up.Address
	if protocol == "v4" {
		addr = up.PoolID
	}
	if addr == "" || up.CreatedAtTimestamp == 0 {
		return Pool{}, false
	}

	t0, t1 := up.Token0, up.Token1
	if t0.Address == "" {
		t0.Address = NativeETH
	}
	if t1.Address == "" {
		t1.Address = NativeETH
	}

	feePct := up.FeeTier / feeTierDenom
	p := Pool{
		Address:    addr,
		Dex:        "uniswap-" + protocol,
		Protocol:   protocol,
		CreatedAt:  time.Unix(up.CreatedAtTimestamp, 0).UTC(),
		Hook:       hookAddress(up.Hook),
		DynamicFee: up.IsDynamicFee,

		BaseAddress:   t0.Address,
		BaseSymbol:    t0.Symbol,
		BaseDecimals:  t0.Decimals,
		QuoteAddress:  t1.Address,
		QuoteSymbol:   t1.Symbol,
		QuoteDecimals: t1.Decimals,

		FeePct:       feePct,
		ReserveUSD:   up.TotalLiquidity.val(),
		VolumeH24USD: up.CumulativeVolume.val(),
	}
	// Best-effort here — prefilter rejects pools with no quote-asset side.
	p, _ = orientQuote(p)
	p.Name = fmt.Sprintf("%s / %s %g%%", p.BaseSymbol, p.QuoteSymbol, feePct)
	return p, true
}

// prefilter applies only the gates the gateway alone can answer, to shrink the
// enrichment batch. It is deliberately a SUBSET of Screen — never a substitute:
// anything that survives here still faces the full Screen once enriched, so a
// prefilter bug can only cost recall, never let a bad pool through.
func prefilter(p Pool, mp ModeParams, now time.Time) bool {
	if !quoteAssets[strings.ToLower(p.QuoteAddress)] {
		return false
	}
	// v4 hard rejects, duplicated from Screen because they are free here and
	// every prefilter cut saves enrichment-batch room for a pool that can win.
	if p.Hook != "" || p.DynamicFee {
		return false
	}
	age := now.Sub(p.CreatedAt)
	if age < mp.MinAge {
		return false
	}
	if mp.MaxAge > 0 && age > mp.MaxAge {
		return false
	}
	if p.ReserveUSD < mp.MinReserveUSD {
		return false
	}
	if mp.MaxReserveUSD > 0 && p.ReserveUSD > mp.MaxReserveUSD {
		return false
	}
	if p.FeePct < mp.MinFeePct {
		return false
	}
	// Fee pace from the 24h window — the same number Screen recomputes for a
	// FeePaceH24 mode, so this rejects nothing Screen would have kept.
	if p.ReserveUSD > 0 {
		feeTVLDay := (p.VolumeH24USD * p.FeePct / 100) / p.ReserveUSD * 100
		if feeTVLDay < mp.MinFeeTVLDay {
			return false
		}
	}
	return true
}

// enrichFromGT fills the fields the Uniswap gateway does not expose — h1
// volume, the h1/h24 buys/sells/buyers/sellers breakdown, the price-change
// windows and FDV/market cap — from ONE GeckoTerminal /pools/multi/ call.
// Pools mutate in place, matched by address (GT lowercases; the gateway
// checksums — compare case-insensitively).
//
// A pool GT does not know about keeps its zero values and will fail Screen's
// txn/buyer gates, which is the correct outcome: no flow data, no trade.
func enrichFromGT(pools []Pool) error {
	addrs := make([]string, len(pools))
	for i, p := range pools {
		addrs[i] = p.Address
	}
	url := fmt.Sprintf("https://api.geckoterminal.com/api/v2/networks/%s/pools/multi/%s",
		Chain, strings.Join(addrs, ","))

	gr, err := fetchPage(url)
	if err != nil {
		return err
	}

	byAddr := make(map[string]gtPoolAttrs, len(gr.Data))
	for _, gp := range gr.Data {
		byAddr[strings.ToLower(gp.Attrs.Address)] = gp.Attrs
	}

	for i := range pools {
		a, ok := byAddr[strings.ToLower(pools[i].Address)]
		if !ok {
			continue
		}
		pools[i].FdvUSD = pfloat(a.FdvUSD)
		pools[i].McapUSD = pfloat(a.MarketCapUSD)
		pools[i].VolumeH1USD = pfloat(a.VolumeUSD.H1)
		pools[i].TxH1 = a.Transactions.H1
		pools[i].TxH24 = a.Transactions.H24
		pools[i].ChangeM5Pct = pfloat(a.PriceChangePct.M5)
		pools[i].ChangeH1Pct = pfloat(a.PriceChangePct.H1)
		pools[i].ChangeH6Pct = pfloat(a.PriceChangePct.H6)
		pools[i].ChangeH24Pct = pfloat(a.PriceChangePct.H24)
		// Prefer GT's reserve and 24h volume over the gateway's: Screen's
		// fee-pace and liquidity gates then read the SAME source the Fresh mode
		// gates on, so a threshold means one thing across both modes.
		if v := pfloat(a.ReserveUSD); v > 0 {
			pools[i].ReserveUSD = v
		}
		if v := pfloat(a.VolumeUSD.H24); v > 0 {
			pools[i].VolumeH24USD = v
		}
		if a.Name != "" {
			pools[i].Name = a.Name
		}
	}
	return nil
}
