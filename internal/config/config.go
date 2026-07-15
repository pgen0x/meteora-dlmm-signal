package config

import (
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/meteora-dlmm-trading-bot/internal/robinhood"
)

// Config holds all runtime settings, sourced from environment variables.
type Config struct {
	// Meteora discovery
	DiscoverURL  string        // base discovery endpoint
	PollInterval time.Duration // how often to poll each timeframe

	// Hermes webhook sink
	WebhookURL    string
	WebhookSecret string

	// Redis dedup (optional; empty RedisAddr -> in-memory dedup)
	RedisAddr    string
	RedisSeenKey string
	SeenTTL      time.Duration
	// Turnover dedups on a shorter window: its positions live minutes, not
	// hours, so a still-qualifying pool must be able to re-signal once the
	// prior cycle ends (pool/symbol cooldowns still gate fee-dead re-entries).
	TurnoverSeenTTL time.Duration
	// Casual gets the same treatment at a gentler setting: positions live
	// ~30m-2h and the monitor's close cooldown lapses in 1-2h, but the full
	// SEEN_TTL silenced a proven pool for the rest of the day. 6h lets it
	// re-compete after the cooldown clears without the re-signal spam a 1-2h
	// window would cause (77% of screen passes are dedup re-qualifiers).
	CasualSeenTTL time.Duration

	// Screening thresholds per mode are defined in the meteora package;
	// only the enable toggles live here.
	EnableCasual   bool
	EnableMultiday bool
	EnableTurnover bool

	// EnableMomentumGate fetches DexScreener momentum to reject downtrends
	// before emitting (matches the Python downtrend gate). Best-effort.
	EnableMomentumGate bool

	// EnableAuditGate fetches the Jupiter token audit (bot-holder %, global
	// fees) for every fresh candidate and hard-rejects bot-farmed tokens
	// before emitting. Best-effort, fail-open like the momentum gate.
	EnableAuditGate bool

	// LoneMinScore is the conviction floor for single-candidate batches: when
	// a cycle produces exactly one fresh pool, it must score at least this to
	// be emitted. Prevents "only option so deploy it" entries on weak solo
	// candidates. 0 disables the gate.
	LoneMinScore float64

	// EnableGmgnGate fetches the GMGN token snapshot (smart-money holder
	// count, insider/bundler volume share, dev track record) for every fresh
	// candidate and attaches it to the payload. Hard-rejects candidates whose
	// insider ("rat") or bundler volume share exceeds the caps below — the
	// strongest pre-rug signals available (three -100% rug closes drove the
	// journal's entire net loss). Missing fields still pass (fail-open);
	// requires GmgnAPIKey (empty key disables the fetch regardless of the
	// toggle). A cap <= 0 disables that check (enrichment stays on).
	EnableGmgnGate    bool
	GmgnAPIKey        string
	GmgnMaxRatPct     float64
	GmgnMaxBundlerPct float64

	// EnablePVPCheck searches for an established same-symbol rival token with
	// its own live DLMM pool and flags contested candidates (is_pvp + rival
	// stats) in the payload. Advisory only — never rejects. Best-effort,
	// fail-open like the momentum/audit gates.
	EnablePVPCheck bool

	// EnableRobinhood turns on the Robinhood Chain venue: GeckoTerminal
	// new-pool discovery + screening (internal/robinhood). Phase 1 is
	// signal-only — robinhood batches ALWAYS go to the webhook sink, never to
	// DeployCmd, because the deploy pipeline only speaks Solana (see
	// docs/ROBINHOOD_CHAIN_PLAN.md). Off by default.
	EnableRobinhood bool
	// EnableRobinhoodMature turns on the venue's SECOND mode (rh-mature):
	// established pools still printing outsized fee/TVL, discovered through
	// Uniswap's own interface gateway rather than GeckoTerminal. Independent of
	// EnableRobinhood on purpose — the two modes share every safety gate but no
	// discovery source, and either can run alone. Off by default.
	EnableRobinhoodMature bool
	// RobinhoodDiscoverURL overrides the GeckoTerminal new_pools endpoint
	// (empty = the package default). The public tier allows 30 req/min.
	// Applies to the Fresh mode only; rh-mature has its own source.
	RobinhoodDiscoverURL string
	// RobinhoodSeenTTL is the venue's dedup window. Fresh-pool signals age out
	// of the thesis within a day; 6h lets a still-qualifying pool re-signal.
	RobinhoodSeenTTL time.Duration
	// RobinhoodMinHolders is the Blockscout holder-count floor per candidate
	// (fail-open when the fetch fails; 0 disables). New-chain tokens
	// accumulate holders fast — 50 filters single-wallet theater without
	// demanding Solana-scale (500+) adoption.
	RobinhoodMinHolders int
	// RobinhoodWebhook forwards robinhood batches to the webhook sink. Off by
	// default (observe-only: batches are journaled to the log): the live
	// Hermes subscription prompt only understands Solana DLMM payloads, and
	// an EVM candidate reaching it could trigger a nonsense deploy attempt.
	// Enable once the subscription prompt handles the robinhood schema.
	RobinhoodWebhook bool

	// RobinhoodDeployEnabled switches the venue to direct-deploy, mirroring
	// Solana's DEPLOY_CMD mode: instead of observing/forwarding, the daemon
	// picks the highest-scoring candidate in each batch and mints it directly
	// via RobinhoodExecutorCmd (uni_executor.js), bypassing the webhook
	// entirely. There is no monitor/exit automation for this venue yet
	// (docs/ROBINHOOD_CHAIN_PLAN.md Phase 3) — positions stay open until
	// closed by hand, so RobinhoodMaxOpenPositions is the only safety brake.
	// Off by default; requires RobinhoodExecutorCmd.
	RobinhoodDeployEnabled bool
	// RobinhoodDeployModes limits direct-deploy to the listed modes; batches
	// from other modes fall back to the observe/webhook sink instead. The
	// deploy toggle alone is all-or-nothing, and the fresh feed's live record
	// (uni_closes.jsonl 2026-07-13/14: 9 of 10 closes were emergency stop
	// losses at a median −49% within ~1 minute) showed why mature-only deploy
	// with fresh kept as an observe journal must be expressible. Keys are the
	// mode names with the "rh-" prefix stripped ("fresh", "mature").
	RobinhoodDeployModes map[string]bool
	// RobinhoodExecutorCmd is the whitespace-split command line for
	// uni_executor.js, e.g.
	// "node /home/ubuntu/.hermes/profiles/<profile>/skills/solana-dlmm/scripts/uni_executor.js".
	// Wallet keys stay in the profile .env — the executor loads them itself.
	RobinhoodExecutorCmd string
	// RobinhoodV4ExecutorCmd is the same for uni_v4_executor.js, the v4
	// sibling (docs/ROBINHOOD_CHAIN_PLAN.md Phase 7). Empty (default) keeps
	// v4 candidates observe-only: they are journaled/forwarded upstream but
	// excluded from deploy, exactly the pre-Phase-7 behavior.
	RobinhoodV4ExecutorCmd string
	// RobinhoodDeployTimeout bounds one uni_executor.js invocation
	// (swap + mint can take a few blocks even at Robinhood Chain's ~100ms pace).
	RobinhoodDeployTimeout time.Duration
	// RobinhoodSize sizes each deploy dynamically from the live WETH balance
	// (robinhood.ComputeDeployAmount) — the venue's port of the Solana
	// pipeline's compute_deploy_amount. Replaces the old fixed
	// ROBINHOOD_DEPLOY_AMOUNT_WETH, which minted a flat 0.003 WETH regardless of
	// wallet size (~17% of a 0.0174 WETH stack) and never grew with the balance.
	RobinhoodSize robinhood.SizeParams
	// RobinhoodSizeUSDG sizes USDG-quoted v4 deploys from the live USDG
	// balance — same shape, dollar units (USDG's 6 decimals are already
	// applied by the executor's balance output). Separate params because a
	// sensible WETH floor (~0.003 ≈ $8) and a sensible dollar floor are
	// different numbers, and sharing one config would silently misprice
	// whichever asset the operator wasn't thinking about.
	RobinhoodSizeUSDG robinhood.SizeParams
	// RobinhoodMinGasEth is the native-ETH floor required to deploy. Unlike
	// Solana — where SOL is gas AND quote, so one reserve covers both — this
	// venue pays gas in ETH but LPs in WETH, so a wallet flush with WETH can
	// still be unable to pay for the mint. Fail closed below this.
	RobinhoodMinGasEth float64
	// RobinhoodDeployStrategy is the uni_executor.js mint strategy:
	// "balanced_tight" (two-sided, swaps half) or "weth_below" (one-sided).
	RobinhoodDeployStrategy string
	RobinhoodRangePct       float64
	RobinhoodSlippagePct    float64
	// RobinhoodMaxOpenPositions caps concurrent NPM positions this venue will
	// hold. Checked via a live `positions` count before every deploy attempt
	// (fail-closed on any read error) since nothing closes positions
	// automatically yet. Keep this low until Phase 3 monitor exists.
	RobinhoodMaxOpenPositions int
	// RobinhoodIndicatorGate runs the supertrend_or_rsi entry-timing check
	// (internal/robinhood/indicators.go, the Go port of local_indicators.py)
	// on each deploy pick, skipping candidates whose token is in a confirmed
	// downtrend. Fail-open on missing candle data. On by default — the same
	// check already gates Solana entries via dlmm_pipeline.py.
	RobinhoodIndicatorGate bool

	// DeployCmd switches the daemon to direct-deploy mode: instead of
	// forwarding each batch to the Hermes agent webhook (LLM pick, observed at
	// 19-54 min/decision), the daemon runs this command with
	// `--from-batch <payload JSON> --mode <mode>` appended and the pipeline
	// picks + deploys deterministically in seconds. Point it at the skill's
	// pipeline, e.g. `python3 <profile>/skills/solana-dlmm/scripts/dlmm_pipeline.py`.
	// Empty (default) keeps the webhook flow. Whitespace-split; no spaces in paths.
	DeployCmd string
	// DeployTimeout bounds one direct-deploy run (pre-swap + on-chain deploy
	// can take a couple of minutes on congested RPC).
	DeployTimeout time.Duration
	// ReportCmd, when set in direct-deploy mode, receives a short outcome
	// report on stdin after each run — e.g. `hermes send -t telegram` (no LLM,
	// reuses the gateway's bot credentials). Empty = log only.
	ReportCmd string
	// ReportRejects also delivers REJECT outcomes to ReportCmd. Off by default:
	// re-signalling modes produce rejects every few cycles and the journal
	// already logs them; deploys are always reported.
	ReportRejects bool
}

func getenv(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func getbool(key string, def bool) bool {
	v := os.Getenv(key)
	if v == "" {
		return def
	}
	b, err := strconv.ParseBool(v)
	if err != nil {
		return def
	}
	return b
}

func getint(key string, def int) int {
	v := os.Getenv(key)
	if v == "" {
		return def
	}
	n, err := strconv.Atoi(v)
	if err != nil {
		return def
	}
	return n
}

func getfloat(key string, def float64) float64 {
	v := os.Getenv(key)
	if v == "" {
		return def
	}
	f, err := strconv.ParseFloat(v, 64)
	if err != nil {
		return def
	}
	return f
}

func getdur(key string, def time.Duration) time.Duration {
	v := os.Getenv(key)
	if v == "" {
		return def
	}
	d, err := time.ParseDuration(v)
	if err != nil {
		return def
	}
	return d
}

// getmodes parses a comma-separated mode list into a set. Entries are
// lowercased and the "rh-" prefix is stripped, so "mature", "rh-mature" and
// "RH-Mature" all name the same mode.
func getmodes(key, def string) map[string]bool {
	v := os.Getenv(key)
	if v == "" {
		v = def
	}
	set := make(map[string]bool)
	for _, m := range strings.Split(v, ",") {
		m = strings.TrimPrefix(strings.ToLower(strings.TrimSpace(m)), "rh-")
		if m != "" {
			set[m] = true
		}
	}
	return set
}

// Load builds a Config from the environment with sane public defaults.
func Load() Config {
	return Config{
		DiscoverURL:               getenv("METEORA_DISCOVER_URL", "https://pool-discovery-api.datapi.meteora.ag/pools"),
		PollInterval:              getdur("POLL_INTERVAL", 60*time.Second),
		WebhookURL:                getenv("HERMES_WEBHOOK_URL", "http://127.0.0.1:8646/webhooks/dlmm-signal"),
		WebhookSecret:             getenv("HERMES_WEBHOOK_SECRET", "dlmm-signal-secret-change-me"),
		RedisAddr:                 getenv("REDIS_ADDR", ""),
		RedisSeenKey:              getenv("REDIS_SEEN_KEY", "dlmm:signal:seen_pools"),
		SeenTTL:                   getdur("SEEN_TTL", 24*time.Hour),
		TurnoverSeenTTL:           getdur("TURNOVER_SEEN_TTL", 2*time.Hour),
		CasualSeenTTL:             getdur("CASUAL_SEEN_TTL", 6*time.Hour),
		EnableCasual:              getbool("ENABLE_CASUAL", true),
		EnableMultiday:            getbool("ENABLE_MULTIDAY", true),
		EnableTurnover:            getbool("ENABLE_TURNOVER", false), // experimental — see meteora.Turnover
		EnableMomentumGate:        getbool("ENABLE_MOMENTUM_GATE", true),
		EnableAuditGate:           getbool("ENABLE_AUDIT_GATE", true),
		EnableGmgnGate:            getbool("ENABLE_GMGN_GATE", true),
		GmgnAPIKey:                getenv("GMGN_API_KEY", ""),
		GmgnMaxRatPct:             getfloat("GMGN_MAX_RAT_PCT", 40),
		GmgnMaxBundlerPct:         getfloat("GMGN_MAX_BUNDLER_PCT", 40),
		LoneMinScore:              getfloat("LONE_MIN_SCORE", 50),
		EnablePVPCheck:            getbool("ENABLE_PVP_CHECK", true),
		EnableRobinhood:           getbool("ROBINHOOD_ENABLED", false),
		EnableRobinhoodMature:     getbool("ROBINHOOD_MATURE", false),
		RobinhoodDiscoverURL:      getenv("ROBINHOOD_DISCOVER_URL", ""),
		RobinhoodWebhook:          getbool("ROBINHOOD_WEBHOOK", false),
		RobinhoodDeployEnabled:    getbool("ROBINHOOD_DEPLOY_ENABLED", false),
		RobinhoodDeployModes:      getmodes("ROBINHOOD_DEPLOY_MODES", "fresh,mature"),
		RobinhoodExecutorCmd:      getenv("ROBINHOOD_EXECUTOR_CMD", ""),
		RobinhoodV4ExecutorCmd:    getenv("ROBINHOOD_V4_EXECUTOR_CMD", ""),
		RobinhoodDeployTimeout:    getdur("ROBINHOOD_DEPLOY_TIMEOUT", 2*time.Minute),
		RobinhoodSize: robinhood.SizeParams{
			// Same 45% pct as the Solana pipeline. Floor is the old fixed size —
			// a position smaller than that isn't worth its gas + round-trip swap.
			// Ceil bounds a single position while the venue is still young; raise
			// it as the wallet and the venue's close journal grow.
			Reserve: getfloat("ROBINHOOD_DEPLOY_RESERVE_WETH", 0.002),
			Pct:     getfloat("ROBINHOOD_DEPLOY_PCT", 0.45),
			Floor:   getfloat("ROBINHOOD_DEPLOY_FLOOR_WETH", 0.003),
			Ceil:    getfloat("ROBINHOOD_DEPLOY_CEIL_WETH", 0.05),
		},
		RobinhoodSizeUSDG: robinhood.SizeParams{
			// Dollar units. Floor ≈ the WETH floor's dollar value; ceil bounds a
			// single USDG position at roughly the WETH ceil's dollar value.
			Reserve: getfloat("ROBINHOOD_DEPLOY_RESERVE_USDG", 5),
			Pct:     getfloat("ROBINHOOD_DEPLOY_PCT_USDG", 0.45),
			Floor:   getfloat("ROBINHOOD_DEPLOY_FLOOR_USDG", 8),
			Ceil:    getfloat("ROBINHOOD_DEPLOY_CEIL_USDG", 150),
		},
		RobinhoodMinGasEth:      getfloat("ROBINHOOD_MIN_GAS_ETH", 0.0002),
		RobinhoodDeployStrategy: getenv("ROBINHOOD_DEPLOY_STRATEGY", "balanced_tight"),
		RobinhoodRangePct:         getfloat("ROBINHOOD_RANGE_PCT", 10),
		RobinhoodSlippagePct:      getfloat("ROBINHOOD_SLIPPAGE_PCT", 5),
		RobinhoodMaxOpenPositions: getint("ROBINHOOD_MAX_OPEN_POSITIONS", 1),
		RobinhoodIndicatorGate:    getbool("ROBINHOOD_INDICATOR_GATE", true),
		RobinhoodSeenTTL:          getdur("ROBINHOOD_SEEN_TTL", 6*time.Hour),
		RobinhoodMinHolders:       getint("ROBINHOOD_MIN_HOLDERS", 50),
		DeployCmd:                 getenv("DEPLOY_CMD", ""),
		DeployTimeout:             getdur("DEPLOY_TIMEOUT", 5*time.Minute),
		ReportCmd:                 getenv("REPORT_CMD", ""),
		ReportRejects:             getbool("REPORT_REJECTS", false),
	}
}
