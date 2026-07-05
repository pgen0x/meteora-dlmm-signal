package scanner

import (
	"context"
	"fmt"
	"log"
	"sort"
	"strings"
	"time"

	"github.com/meteora-dlmm-trading-bot/internal/config"
	"github.com/meteora-dlmm-trading-bot/internal/meteora"
	"github.com/meteora-dlmm-trading-bot/internal/store"
	"github.com/meteora-dlmm-trading-bot/internal/webhook"
)

// batchSummary renders a compact "SYM(score)" list for one log line.
func batchSummary(batch []*meteora.Candidate) string {
	parts := make([]string, 0, len(batch))
	for _, c := range batch {
		parts = append(parts, fmt.Sprintf("%s(%.0f)", c.BaseSymbol, c.Score))
	}
	return strings.Join(parts, ", ")
}

// reasonKey collapses a Screen reject reason to its stable prefix (the text
// before the first number or colon) so per-pool reasons group into a tally.
// "fee/TVL 0.02% < 0.10%" -> "fee/TVL", "non-SOL pool" -> "non-SOL_pool".
func reasonKey(reason string) string {
	if i := strings.IndexAny(reason, "0123456789:"); i >= 0 {
		reason = reason[:i]
	}
	reason = strings.TrimRight(reason, " $<>=(%-")
	if reason == "" {
		return "other"
	}
	return strings.ReplaceAll(reason, " ", "_")
}

// rejectSummary renders a reject tally as "k=v k=v", highest count first.
func rejectSummary(rejects map[string]int) string {
	keys := make([]string, 0, len(rejects))
	for k := range rejects {
		keys = append(keys, k)
	}
	sort.Slice(keys, func(i, j int) bool {
		if rejects[keys[i]] != rejects[keys[j]] {
			return rejects[keys[i]] > rejects[keys[j]]
		}
		return keys[i] < keys[j]
	})
	parts := make([]string, 0, len(keys))
	for _, k := range keys {
		parts = append(parts, fmt.Sprintf("%s=%d", k, rejects[k]))
	}
	return strings.Join(parts, " ")
}

// Scanner polls the Meteora discovery API for each enabled mode, screens pools,
// dedups, and forwards newly-qualifying pools to the Hermes webhook.
type Scanner struct {
	cfg  config.Config
	seen *store.Seen
	fwd  *webhook.Forwarder
}

// New wires a Scanner from config.
func New(cfg config.Config) *Scanner {
	return &Scanner{
		cfg:  cfg,
		seen: store.New(cfg.RedisAddr, cfg.RedisSeenKey, cfg.SeenTTL),
		fwd:  webhook.New(cfg.WebhookURL, cfg.WebhookSecret),
	}
}

// modes returns the enabled screening modes.
func (s *Scanner) modes() []meteora.ModeParams {
	var out []meteora.ModeParams
	if s.cfg.EnableCasual {
		out = append(out, meteora.Casual)
	}
	if s.cfg.EnableMultiday {
		out = append(out, meteora.Multiday)
	}
	return out
}

// Run blocks, polling on PollInterval until ctx is cancelled.
func (s *Scanner) Run(ctx context.Context) {
	log.Printf("scanner: started (interval=%v, casual=%v, multiday=%v, momentum=%v)",
		s.cfg.PollInterval, s.cfg.EnableCasual, s.cfg.EnableMultiday, s.cfg.EnableMomentumGate)

	ticker := time.NewTicker(s.cfg.PollInterval)
	defer ticker.Stop()

	s.pollAll(ctx)
	for {
		select {
		case <-ticker.C:
			s.pollAll(ctx)
		case <-ctx.Done():
			log.Println("scanner: stopped")
			return
		}
	}
}

func (s *Scanner) pollAll(ctx context.Context) {
	for _, mp := range s.modes() {
		s.pollMode(ctx, mp)
	}
}

func (s *Scanner) pollMode(ctx context.Context, mp meteora.ModeParams) {
	pools, err := meteora.FetchTopPools(s.cfg.DiscoverURL, mp)
	if err != nil {
		log.Printf("scanner[%s]: fetch error: %v", mp.Mode, err)
		return
	}

	// Per-poll tally so a quiet cycle logs "scanned N, 0 passed" instead of
	// nothing — distinguishes "working, nothing qualified" from "API empty".
	var screened, deduped, momRejected int
	rejects := map[string]int{}

	// Batch mode: collect every fresh, momentum-passing candidate this cycle and
	// emit ONE signal carrying the whole array. The agent then compares the set,
	// picks the strongest pool + strategy, and deploys — instead of first-come
	// per-pool sends where a mediocre early pool grabs a slot the best pool wanted.
	var batch []*meteora.Candidate
	var batchKeys []string
	for _, p := range pools {
		cand, reason := meteora.Screen(p, mp)
		if reason != "" {
			rejects[reasonKey(reason)]++ // per-pool detail too noisy; tallied per gate
			continue
		}
		screened++

		// Dedup BEFORE the momentum fetch so we don't hit DexScreener for
		// pools we've already emitted this window.
		poolKey := mp.Mode + ":" + cand.Pool
		fresh, err := s.seen.MarkIfNew(ctx, poolKey)
		if err != nil {
			log.Printf("scanner[%s]: seen store error: %v", mp.Mode, err)
			continue
		}
		if !fresh {
			deduped++
			continue
		}

		// Momentum / downtrend gate (best-effort, fail-open). Momentum-rejected
		// pools stay marked seen (no unmark) so we don't re-hit DexScreener for
		// them every cycle within the SEEN_TTL window.
		if s.cfg.EnableMomentumGate {
			if m, ok := meteora.GetMomentum(cand.BaseMint); ok {
				if r := meteora.MomentumReject(m); r != "" {
					momRejected++
					log.Printf("scanner[%s]: %s (%s) rejected on momentum: %s", mp.Mode, cand.BaseSymbol, cand.Pool[:8], r)
					continue
				}
			}
		}

		batch = append(batch, cand)
		batchKeys = append(batchKeys, poolKey)
	}

	sent := 0
	if len(batch) > 0 {
		if err := s.fwd.Send("meteora_pool_discovery", batch, time.Now().Unix()); err != nil {
			// Delivery failed — unmark the whole batch so these pools retry on the
			// next poll instead of being silently dropped for the SEEN_TTL window.
			for _, k := range batchKeys {
				s.seen.Unmark(ctx, k)
			}
			log.Printf("scanner[%s]: webhook error for batch of %d (will retry): %v", mp.Mode, len(batch), err)
		} else {
			sent = len(batch)
			log.Printf("scanner[%s]: SIGNAL batch sent %d pools: %s", mp.Mode, sent, batchSummary(batch))
		}
	}

	line := fmt.Sprintf("scanner[%s]: cycle done — fetched=%d passed_screen=%d deduped=%d mom_rejected=%d sent=%d",
		mp.Mode, len(pools), screened, deduped, momRejected, sent)
	if len(rejects) > 0 {
		line += " rejects[" + rejectSummary(rejects) + "]"
	}
	log.Print(line)
}
