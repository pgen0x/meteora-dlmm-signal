package config

import (
	"os"
	"strconv"
	"time"
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
	// candidate and attaches it to the payload. Advisory only — never
	// rejects. Best-effort, fail-open; requires GmgnAPIKey (empty key
	// disables the fetch regardless of the toggle).
	EnableGmgnGate bool
	GmgnAPIKey     string

	// EnablePVPCheck searches for an established same-symbol rival token with
	// its own live DLMM pool and flags contested candidates (is_pvp + rival
	// stats) in the payload. Advisory only — never rejects. Best-effort,
	// fail-open like the momentum/audit gates.
	EnablePVPCheck bool
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

// Load builds a Config from the environment with sane public defaults.
func Load() Config {
	return Config{
		DiscoverURL:        getenv("METEORA_DISCOVER_URL", "https://pool-discovery-api.datapi.meteora.ag/pools"),
		PollInterval:       getdur("POLL_INTERVAL", 60*time.Second),
		WebhookURL:         getenv("HERMES_WEBHOOK_URL", "http://127.0.0.1:8646/webhooks/dlmm-signal"),
		WebhookSecret:      getenv("HERMES_WEBHOOK_SECRET", "dlmm-signal-secret-change-me"),
		RedisAddr:          getenv("REDIS_ADDR", ""),
		RedisSeenKey:       getenv("REDIS_SEEN_KEY", "dlmm:signal:seen_pools"),
		SeenTTL:            getdur("SEEN_TTL", 24*time.Hour),
		TurnoverSeenTTL:    getdur("TURNOVER_SEEN_TTL", 2*time.Hour),
		CasualSeenTTL:      getdur("CASUAL_SEEN_TTL", 6*time.Hour),
		EnableCasual:       getbool("ENABLE_CASUAL", true),
		EnableMultiday:     getbool("ENABLE_MULTIDAY", true),
		EnableTurnover:     getbool("ENABLE_TURNOVER", false), // experimental — see meteora.Turnover
		EnableMomentumGate: getbool("ENABLE_MOMENTUM_GATE", true),
		EnableAuditGate:    getbool("ENABLE_AUDIT_GATE", true),
		EnableGmgnGate:     getbool("ENABLE_GMGN_GATE", true),
		GmgnAPIKey:         getenv("GMGN_API_KEY", ""),
		LoneMinScore:       getfloat("LONE_MIN_SCORE", 50),
		EnablePVPCheck:     getbool("ENABLE_PVP_CHECK", true),
	}
}
