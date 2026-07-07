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
	if s.cfg.EnableTurnover {
		out = append(out, meteora.Turnover)
	}
	return out
}

// Run blocks, polling on PollInterval until ctx is cancelled.
func (s *Scanner) Run(ctx context.Context) {
	log.Printf("scanner: started (interval=%v, casual=%v, multiday=%v, turnover=%v, momentum=%v)",
		s.cfg.PollInterval, s.cfg.EnableCasual, s.cfg.EnableMultiday, s.cfg.EnableTurnover, s.cfg.EnableMomentumGate)

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
	var screened, deduped, momRejected, auditRejected, loneHeld int
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

		// Jupiter audit gate (best-effort, fail-open, same seen semantics as
		// momentum). Hard-rejects bot-farmed tokens; otherwise enriches the
		// candidate with bot % / global fees so the agent judges with the
		// audit on-screen instead of re-fetching it.
		if s.cfg.EnableAuditGate {
			if a, ok := meteora.FetchAudit(cand.BaseMint); ok {
				if r := meteora.AuditReject(a); r != "" {
					auditRejected++
					log.Printf("scanner[%s]: %s (%s) rejected on audit: %s", mp.Mode, cand.BaseSymbol, cand.Pool[:8], r)
					continue
				}
				cand.ApplyAudit(a)
			}
		}

		// Pool memory summary: surface this pool's journaled close record so
		// the agent weighs a mixed history when picking (the pipeline's
		// deterministic ">=2 closes net negative" skip still applies at
		// deploy time — this is the advisory layer on top).
		if n, pnl, ok := s.seen.PoolCloseStats(ctx, cand.Pool); ok {
			cand.PriorCloses = &n
			cand.PriorNetPnlSOL = &pnl
		}

		batch = append(batch, cand)
		batchKeys = append(batchKeys, poolKey)
	}

	// Lone-candidate conviction gate: a single-pool batch removes the agent's
	// ability to compare, and "only option" must not read as "good option".
	// A solo candidate ships only when its score clears the conviction floor.
	// The pool is unmarked so it can ride a future, richer batch (or re-pass
	// alone once its score improves) instead of being silenced for SEEN_TTL.
	if len(batch) == 1 && s.cfg.LoneMinScore > 0 && batch[0].Score < s.cfg.LoneMinScore {
		log.Printf("scanner[%s]: lone candidate %s (%s) score %.1f < %.1f — held back",
			mp.Mode, batch[0].BaseSymbol, batch[0].Pool[:8], batch[0].Score, s.cfg.LoneMinScore)
		s.seen.Unmark(ctx, batchKeys[0])
		loneHeld++
		batch, batchKeys = nil, nil
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

	line := fmt.Sprintf("scanner[%s]: cycle done — fetched=%d passed_screen=%d deduped=%d mom_rejected=%d audit_rejected=%d lone_held=%d sent=%d",
		mp.Mode, len(pools), screened, deduped, momRejected, auditRejected, loneHeld, sent)
	if len(rejects) > 0 {
		line += " rejects[" + rejectSummary(rejects) + "]"
	}
	log.Print(line)
}
