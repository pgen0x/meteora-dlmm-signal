package robinhood

import (
	"log"
	"strings"
)

// Copycat (same-symbol collision) detection. Meme launches on the venue spawn
// several tokens under one hot ticker in quick succession — observed directly
// as two CALLIE pools qualifying in a single discovery cycle. Only one is the
// token attention is flowing to; the others split liquidity and are where an
// LP ends up holding the loser. This is the venue analog of the Solana
// EnrichPVP guard, but detection is INTRA-BATCH (no network call): a rival that
// shows up in the same cycle IS the contest, and fresh launches have no
// established external pool to search for anyway.
//
// Advisory, like is_pvp: it never rejects a candidate. It flags every member of
// a colliding same-symbol group (is_copycat + copycat_count) so payload
// consumers can compare, and the autonomous picker (scanner.robinhoodDeploy)
// demotes copycats below any clean candidate.
//
// Limitation: same-cycle only. Two same-symbol launches in DIFFERENT cycles are
// deduped independently and this never sees them together — cross-cycle copycat
// detection would need a persistent symbol memory (not built; the observed
// quirk was same-cycle).

// EnrichCopycat flags every candidate whose (normalized) symbol is shared by at
// least one other candidate in the batch. Returns the number of candidates
// flagged. O(n) over a post-gate batch — no I/O.
func EnrichCopycat(batch []*Candidate) int {
	groups := map[string][]*Candidate{}
	for _, c := range batch {
		sym := strings.ToUpper(strings.TrimSpace(c.BaseSymbol))
		if sym == "" {
			continue // an empty ticker isn't a meaningful collision key
		}
		groups[sym] = append(groups[sym], c)
	}

	flagged := 0
	for sym, members := range groups {
		if len(members) < 2 {
			continue
		}
		for _, c := range members {
			c.IsCopycat = true
			c.CopycatCount = len(members)
		}
		flagged += len(members)
		log.Printf("robinhood: copycat guard: %d pools share ticker %q this cycle — %s",
			len(members), sym, copycatPoolList(members))
	}
	return flagged
}

// copycatPoolList renders the colliding pools for the guard log line.
func copycatPoolList(members []*Candidate) string {
	parts := make([]string, 0, len(members))
	for _, c := range members {
		short := c.Pool
		if len(short) > 10 {
			short = short[:10]
		}
		parts = append(parts, short)
	}
	return strings.Join(parts, ", ")
}
