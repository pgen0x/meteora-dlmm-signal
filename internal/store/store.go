package store

import (
	"context"
	"encoding/json"
	"strings"
	"sync"
	"time"

	redis "github.com/redis/go-redis/v9"
)

// Seen tracks which pools have already been emitted, so each qualifying pool
// fires exactly once until its TTL lapses. Backed by Redis when configured,
// otherwise an in-memory map (single-instance).
type Seen struct {
	rdb *redis.Client
	key string
	ttl time.Duration
	mu  sync.Mutex
	mem map[string]time.Time
}

// New builds a Seen store. addr == "" selects the in-memory backend.
func New(addr, key string, ttl time.Duration) *Seen {
	s := &Seen{key: key, ttl: ttl, mem: make(map[string]time.Time)}
	if addr != "" {
		s.rdb = redis.NewClient(&redis.Options{Addr: addr})
	}
	return s
}

// MarkIfNew atomically records id and reports whether it was newly added
// (true == first time we've seen it, caller should emit a signal).
func (s *Seen) MarkIfNew(ctx context.Context, id string) (bool, error) {
	return s.MarkIfNewTTL(ctx, id, s.ttl)
}

// MarkIfNewTTL is MarkIfNew with a caller-chosen window — the scanner passes a
// shorter TTL for turnover mode so a still-qualifying pool re-signals after a
// fast cycle ends instead of being silenced for the full default window.
func (s *Seen) MarkIfNewTTL(ctx context.Context, id string, ttl time.Duration) (bool, error) {
	if ttl <= 0 {
		ttl = s.ttl
	}
	if s.rdb != nil {
		// One key per pool with its own TTL. A Redis SET can only expire as a
		// whole, so the old SAdd+Expire refreshed the entire set's TTL on every
		// write — the rolling window never lapsed while the scanner kept polling,
		// so once-seen pools were deduped forever and never re-signalled.
		// SetNX gives each pool an independent SEEN_TTL window that actually ages out.
		ok, err := s.rdb.SetNX(ctx, s.key+":"+id, 1, ttl).Result()
		if err != nil {
			return false, err
		}
		return ok, nil
	}

	s.mu.Lock()
	defer s.mu.Unlock()
	now := time.Now()
	// Lazy expiry of the in-memory map (values are expiry deadlines).
	for k, t := range s.mem {
		if now.After(t) {
			delete(s.mem, k)
		}
	}
	if _, ok := s.mem[id]; ok {
		return false, nil
	}
	s.mem[id] = now.Add(ttl)
	return true, nil
}

// PoolCloseStats summarizes a pool's close journal written by dlmm_monitor.py
// (sol:dlmm:history:pool:<pool> — last 10 closes, 30d TTL). Returns ok=false
// when there is no history, no Redis backend, or the read fails: absent data
// must read as "unknown", never as "clean record" (fail-open convention).
func (s *Seen) PoolCloseStats(ctx context.Context, pool string) (closes int, netPnlSOL float64, ok bool) {
	if s.rdb == nil {
		return 0, 0, false
	}
	entries, err := s.rdb.LRange(ctx, "sol:dlmm:history:pool:"+pool, 0, 9).Result()
	if err != nil || len(entries) == 0 {
		return 0, 0, false
	}
	for _, e := range entries {
		var rec struct {
			PnlSOL float64 `json:"pnl_sol"`
		}
		if json.Unmarshal([]byte(e), &rec) != nil {
			continue
		}
		closes++
		netPnlSOL += rec.PnlSOL
	}
	return closes, netPnlSOL, closes > 0
}

// CooldownRemaining reports how long a token symbol is still under the
// monitor's re-entry cooldown (sol:dlmm:cooldown:<SYMBOL>, written by
// dlmm_monitor.py on every close). Zero means not cooling or unknown: the
// in-memory backend and Redis errors read as "no cooldown" (fail-open) —
// the deploy-time pipeline check stays the enforcing layer.
func (s *Seen) CooldownRemaining(ctx context.Context, symbol string) time.Duration {
	if s.rdb == nil || symbol == "" {
		return 0
	}
	d, err := s.rdb.TTL(ctx, "sol:dlmm:cooldown:"+strings.ToUpper(symbol)).Result()
	if err != nil || d <= 0 {
		return 0
	}
	return d
}

// Unmark removes id from the seen set so a failed emit can retry on the next
// poll. Called when webhook delivery fails after MarkIfNew already recorded it.
func (s *Seen) Unmark(ctx context.Context, id string) {
	if s.rdb != nil {
		s.rdb.Del(ctx, s.key+":"+id)
		return
	}
	s.mu.Lock()
	delete(s.mem, id)
	s.mu.Unlock()
}
