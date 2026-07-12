package scanner

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"sort"
	"strings"
	"time"

	"github.com/meteora-dlmm-trading-bot/internal/config"
	"github.com/meteora-dlmm-trading-bot/internal/deploy"
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
	dep  *deploy.Runner
}

// New wires a Scanner from config.
func New(cfg config.Config) *Scanner {
	return &Scanner{
		cfg:  cfg,
		seen: store.New(cfg.RedisAddr, cfg.RedisSeenKey, cfg.SeenTTL),
		fwd:  webhook.New(cfg.WebhookURL, cfg.WebhookSecret),
		dep:  deploy.New(cfg.DeployCmd, cfg.ReportCmd, cfg.DeployTimeout),
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

// directDeploy hands the batch straight to the deterministic picker pipeline
// (DEPLOY_CMD) instead of the agent webhook, then delivers a short outcome
// report via REPORT_CMD. Runs synchronously inside the poll loop: deploys are
// rare relative to poll cycles and serializing them prevents two modes from
// racing the same wallet balance. Returns the number of candidates handed
// over (0 on execution failure, which unmarks the batch for retry).
func (s *Scanner) directDeploy(ctx context.Context, mode string, batch []*meteora.Candidate, batchKeys []string) int {
	body, err := json.Marshal(batch)
	if err != nil {
		log.Printf("scanner[%s]: batch marshal error: %v", mode, err)
		return 0
	}
	out, err := s.dep.Deploy(ctx, body, mode)
	if err != nil {
		// Execution failure (timeout, non-zero exit — e.g. failed on-chain
		// deploy), not a deterministic reject: unmark so the batch retries
		// next cycle, mirroring the webhook-failure path.
		for _, k := range batchKeys {
			s.seen.Unmark(ctx, k)
		}
		log.Printf("scanner[%s]: direct deploy failed for batch of %d (will retry): %v\n%s", mode, len(batch), err, out)
		return 0
	}
	deployed := deploy.Deployed(out)
	summary := deploy.Summarize(out, mode)
	log.Printf("scanner[%s]: direct deploy done (deployed=%v) for %s\n%s", mode, deployed, batchSummary(batch), summary)
	// Journal the per-candidate gate decisions — without these, a run of
	// deterministic rejects is undiagnosable from the logs alone.
	if narrative := deploy.GateNarrative(out); narrative != "" {
		log.Printf("scanner[%s]: conviction narrative:\n%s", mode, narrative)
	}
	if deployed || s.cfg.ReportRejects {
		if rerr := s.dep.Report(ctx, summary); rerr != nil {
			log.Printf("scanner[%s]: report delivery failed: %v", mode, rerr)
		}
	}
	return len(batch)
}

func (s *Scanner) pollMode(ctx context.Context, mp meteora.ModeParams) {
	pools, err := meteora.FetchTopPools(s.cfg.DiscoverURL, mp)
	if err != nil {
		log.Printf("scanner[%s]: fetch error: %v", mp.Mode, err)
		return
	}

	// Per-poll tally so a quiet cycle logs "scanned N, 0 passed" instead of
	// nothing — distinguishes "working, nothing qualified" from "API empty".
	var screened, cooldownBlocked, deduped, momRejected, auditRejected, gmgnRejected, gmgnEnriched, loneHeld, pvpFlagged int
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

		// Re-entry cooldown gate: dlmm_monitor.py sets a per-token cooldown on
		// every close (1-2h base, escalating to 24h/72h on repeat losses).
		// Until now this was only enforced at deploy time, so a batch full of
		// cooling tokens wasted the whole signal and crowded out eligible
		// pools. Checked BEFORE MarkIfNew so the pool re-signals as soon as
		// its cooldown lapses instead of staying silenced for SEEN_TTL.
		if cd := s.seen.CooldownRemaining(ctx, cand.BaseSymbol); cd > 0 {
			cooldownBlocked++
			log.Printf("scanner[%s]: %s (%s) in re-entry cooldown (%s left)",
				mp.Mode, cand.BaseSymbol, cand.Pool[:8], cd.Round(time.Minute))
			continue
		}

		// Dedup BEFORE the momentum fetch so we don't hit DexScreener for
		// pools we've already emitted this window. Turnover uses its own
		// shorter window: fast-cycle positions live minutes, and the default
		// TTL silenced still-qualifying pools long after their cycle ended.
		poolKey := mp.Mode + ":" + cand.Pool
		seenTTL := s.cfg.SeenTTL
		switch mp.Mode {
		case "turnover":
			seenTTL = s.cfg.TurnoverSeenTTL
		case "casual":
			// Casual positions + their close cooldown resolve within hours;
			// see CasualSeenTTL in config for why 24h over-silenced.
			seenTTL = s.cfg.CasualSeenTTL
		}
		fresh, err := s.seen.MarkIfNewTTL(ctx, poolKey, seenTTL)
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

		// GMGN holder-quality gate (fail-open, same seen semantics as
		// momentum/audit). Hard-rejects tokens whose insider ("rat") or
		// bundler volume share exceeds the configured caps — the pre-rug
		// signals none of the other gates can see. Otherwise attaches
		// smart-money/KOL holder counts, insider + bundler volume share and
		// the dev's track record so the agent ranks smart-money-backed pools
		// above bot-farmed ones. A failed fetch just ships the candidate bare.
		if s.cfg.EnableGmgnGate && s.cfg.GmgnAPIKey != "" {
			if g, ok := meteora.FetchGmgn(s.cfg.GmgnAPIKey, cand.BaseMint, time.Now().Unix()); ok {
				if r := meteora.GmgnReject(g, s.cfg.GmgnMaxRatPct, s.cfg.GmgnMaxBundlerPct); r != "" {
					gmgnRejected++
					log.Printf("scanner[%s]: %s (%s) rejected on gmgn: %s", mp.Mode, cand.BaseSymbol, cand.Pool[:8], r)
					continue
				}
				cand.ApplyGmgn(g)
				gmgnEnriched++
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

	// PVP rival check (advisory, fail-open): flag candidates whose symbol is
	// contested by an established rival token with its own live DLMM pool, so
	// the agent weighs the war before entering the weaker side. Runs on the
	// final batch only — post-gate, post-dedup — to keep the request budget
	// bounded (one symbol search per unique symbol per cycle).
	if s.cfg.EnablePVPCheck && len(batch) > 0 {
		pvpFlagged = meteora.EnrichPVP(batch)
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
		if s.dep.Enabled() {
			sent = s.directDeploy(ctx, mp.Mode, batch, batchKeys)
		} else if err := s.fwd.Send("meteora_pool_discovery", batch, time.Now().Unix()); err != nil {
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

	line := fmt.Sprintf("scanner[%s]: cycle done — fetched=%d passed_screen=%d cooldown_blocked=%d deduped=%d mom_rejected=%d audit_rejected=%d gmgn_rejected=%d gmgn_enriched=%d pvp_flagged=%d lone_held=%d sent=%d",
		mp.Mode, len(pools), screened, cooldownBlocked, deduped, momRejected, auditRejected, gmgnRejected, gmgnEnriched, pvpFlagged, loneHeld, sent)
	if len(rejects) > 0 {
		line += " rejects[" + rejectSummary(rejects) + "]"
	}
	log.Print(line)
}
