package robinhood

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"strings"
)

// v4Meta is the per-pool metadata only Uniswap's gateway can supply for v4:
// GeckoTerminal has no hook or dynamic-fee field anywhere, and v4 pool names
// in its feed often omit the fee-tier suffix that parseFeePct reads for v3
// (arbitrary v4 tiers like 0.046% are simply left out of the name).
type v4Meta struct {
	FeeTier      float64 `json:"feeTier"`
	IsDynamicFee bool    `json:"isDynamicFee"`
	Hook         *struct {
		Address string `json:"address"`
	} `json:"hook"`
}

// gatewayQuery POSTs one GraphQL operation to Uniswap's interface gateway and
// decodes the "data" object into out. Per-field errors are logged, not fatal —
// the gateway returns usable data alongside them (same policy as the mature
// fetch, which this generalizes).
func gatewayQuery(operation, query string, vars map[string]any, out any) error {
	body, err := json.Marshal(map[string]any{
		"operationName": operation,
		"query":         query,
		"variables":     vars,
	})
	if err != nil {
		return err
	}

	req, err := http.NewRequest(http.MethodPost, gatewayURL, bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	// The gateway wants a browser-shaped Origin; without it the edge can 403.
	req.Header.Set("Origin", "https://app.uniswap.org")

	resp, err := gatewayClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		io.Copy(io.Discard, io.LimitReader(resp.Body, 512))
		return fmt.Errorf("uniswap gateway status %d", resp.StatusCode)
	}

	var envelope struct {
		Data   json.RawMessage `json:"data"`
		Errors []struct {
			Message string `json:"message"`
		} `json:"errors"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&envelope); err != nil {
		return fmt.Errorf("uniswap gateway decode: %w", err)
	}
	if len(envelope.Errors) > 0 {
		log.Printf("robinhood: uniswap gateway %s returned %d field error(s), first: %s",
			operation, len(envelope.Errors), envelope.Errors[0].Message)
	}
	if len(envelope.Data) == 0 {
		return fmt.Errorf("uniswap gateway %s returned no data", operation)
	}
	return json.Unmarshal(envelope.Data, out)
}

// fetchV4Meta resolves fee tier, hook and dynamic-fee status for a set of v4
// poolIds in ONE gateway call, using GraphQL aliases (p0..pN) — the gateway
// has no batch endpoint, but alias fan-in keeps this at one POST per cycle
// regardless of how many v4 pools the launch feed carried.
//
// The returned map is keyed by lowercase poolId; ids the gateway does not know
// are absent. Callers treat a missing entry as "meta unknown" and drop the
// pool for the cycle (fail-closed): an unverified hook must never read as
// hookless, and the pool simply retries next cycle if it is still alive.
func fetchV4Meta(poolIDs []string) (map[string]v4Meta, error) {
	if len(poolIDs) == 0 {
		return map[string]v4Meta{}, nil
	}

	var q strings.Builder
	q.WriteString("query V4PoolMeta($chain: Chain!) {")
	for i, id := range poolIDs {
		fmt.Fprintf(&q, " p%d: v4Pool(chain: $chain, poolId: %q) { feeTier isDynamicFee hook { address } }", i, id)
	}
	q.WriteString(" }")

	aliased := map[string]*v4Meta{}
	err := gatewayQuery("V4PoolMeta", q.String(), map[string]any{
		"chain": strings.ToUpper(Chain),
	}, &aliased)
	if err != nil {
		return nil, err
	}

	out := make(map[string]v4Meta, len(aliased))
	for alias, m := range aliased {
		if m == nil {
			continue // gateway does not index this poolId (yet)
		}
		var idx int
		if _, err := fmt.Sscanf(alias, "p%d", &idx); err != nil || idx < 0 || idx >= len(poolIDs) {
			continue
		}
		out[strings.ToLower(poolIDs[idx])] = *m
	}
	return out, nil
}

// hookAddress flattens the gateway's nullable hook object; "" = hookless.
func hookAddress(h *struct {
	Address string `json:"address"`
}) string {
	if h == nil {
		return ""
	}
	return h.Address
}
