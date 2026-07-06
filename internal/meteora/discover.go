package meteora

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"
)

// discoverClient timeout mirrors the Python urllib timeout=15.
var discoverClient = &http.Client{Timeout: 15 * time.Second}

// buildFilters pushes the mode thresholds into the discovery API's filter_by
// query (ported from Meridian discoverPools) so the API returns pre-filtered
// pools instead of us discarding junk client-side. Screen still re-checks every
// gate locally (belt-and-suspenders) since the API filter is best-effort.
func buildFilters(mp ModeParams) string {
	f := []string{
		"pool_type=dlmm",
		"base_token_has_critical_warnings=false",
		"quote_token_has_critical_warnings=false",
		"base_token_has_high_single_ownership=false",
		"base_token_has_high_supply_concentration=false",
		fmt.Sprintf("base_token_market_cap>=%.0f", mp.MinMcap),
		fmt.Sprintf("base_token_holders>=%d", mp.MinHolders),
		fmt.Sprintf("tvl>=%.0f", mp.MinTVL),
		fmt.Sprintf("base_token_organic_score>=%.0f", mp.MinOrganic),
	}
	if mp.MinQuoteOrganic > 0 {
		f = append(f, fmt.Sprintf("quote_token_organic_score>=%.0f", mp.MinQuoteOrganic))
	}
	if mp.MinBinStep > 0 {
		f = append(f, fmt.Sprintf("dlmm_bin_step>=%d", mp.MinBinStep))
	}
	if mp.MaxBinStep > 0 {
		f = append(f, fmt.Sprintf("dlmm_bin_step<=%d", mp.MaxBinStep))
	}
	// Turnover-mode thresholds. CAUTION: the API silently returns zero rows for
	// unknown filter fields (no error), so only fields verified live belong here.
	if mp.MaxTVL > 0 {
		f = append(f, fmt.Sprintf("tvl<=%.0f", mp.MaxTVL))
	}
	if mp.MinFeePct > 0 {
		f = append(f, fmt.Sprintf("fee_pct>=%.2f", mp.MinFeePct))
	}
	if mp.MinVolTVLRatio > 0 {
		f = append(f, fmt.Sprintf("volume_tvl_ratio>=%.2f", mp.MinVolTVLRatio))
	}
	if mp.MinSwapCount > 0 {
		f = append(f, fmt.Sprintf("swap_count>=%.0f", mp.MinSwapCount))
	}
	if mp.MinUniqueTraders > 0 {
		f = append(f, fmt.Sprintf("unique_traders>=%.0f", mp.MinUniqueTraders))
	}
	// fee_tvl_ratio is pushed API-side only for full-universe modes: with
	// category=all it is the core selectivity gate, while the trending modes
	// keep their historical query shape (Screen still gates it locally).
	if mp.MinFeeTVL > 0 && mp.Category == "all" {
		f = append(f, fmt.Sprintf("fee_tvl_ratio>=%.2f", mp.MinFeeTVL))
	}
	return strings.Join(f, "&&")
}

// FetchTopPools pulls the trending pools for a mode, applying the mode's
// thresholds API-side. Mirrors dlmm_pipeline.fetch_top_pools.
func FetchTopPools(baseURL string, mp ModeParams) ([]Pool, error) {
	category := mp.Category
	if category == "" {
		category = "trending"
	}
	reqURL := fmt.Sprintf("%s?page_size=50&timeframe=%s&category=%s&filter_by=%s",
		baseURL, mp.Timeframe, category, url.QueryEscape(buildFilters(mp)))
	if mp.SortBy != "" {
		reqURL += "&sort_by=" + url.QueryEscape(mp.SortBy)
	}
	req, err := http.NewRequest(http.MethodGet, reqURL, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("User-Agent", "Mozilla/5.0")

	resp, err := discoverClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		b, _ := io.ReadAll(io.LimitReader(resp.Body, 200))
		return nil, fmt.Errorf("discover HTTP %d: %s", resp.StatusCode, string(b))
	}

	var out discoverResponse
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return nil, fmt.Errorf("decode discover: %w", err)
	}
	return out.Data, nil
}
