package robinhood

import "testing"

func TestEnrichCopycat(t *testing.T) {
	batch := []*Candidate{
		{Pool: "0xaaaa000001", BaseSymbol: "CALLIE"},
		{Pool: "0xbbbb000002", BaseSymbol: "callie"}, // case/space-insensitive collision
		{Pool: "0xcccc000003", BaseSymbol: "PEPE"},    // unique -> clean
		{Pool: "0xdddd000004", BaseSymbol: " CALLIE "},
		{Pool: "0xeeee000005", BaseSymbol: ""}, // empty ticker never collides
		{Pool: "0xffff000006", BaseSymbol: ""},
	}

	flagged := EnrichCopycat(batch)
	if flagged != 3 {
		t.Fatalf("flagged = %d, want 3 (the three CALLIE variants)", flagged)
	}

	for i, c := range batch {
		wantCopycat := c.BaseSymbol != "" && normSym(c.BaseSymbol) == "CALLIE"
		if c.IsCopycat != wantCopycat {
			t.Errorf("batch[%d] %q: IsCopycat = %v, want %v", i, c.BaseSymbol, c.IsCopycat, wantCopycat)
		}
		if wantCopycat && c.CopycatCount != 3 {
			t.Errorf("batch[%d] %q: CopycatCount = %d, want 3", i, c.BaseSymbol, c.CopycatCount)
		}
		if c.BaseSymbol == "" && c.IsCopycat {
			t.Errorf("batch[%d]: empty ticker must not be flagged", i)
		}
	}
}

func TestEnrichCopycatNoCollision(t *testing.T) {
	batch := []*Candidate{
		{Pool: "0x1", BaseSymbol: "A"},
		{Pool: "0x2", BaseSymbol: "B"},
	}
	if n := EnrichCopycat(batch); n != 0 {
		t.Fatalf("flagged = %d, want 0", n)
	}
	for _, c := range batch {
		if c.IsCopycat {
			t.Errorf("%q wrongly flagged", c.BaseSymbol)
		}
	}
}

// normSym mirrors EnrichCopycat's key normalization for the test assertion
// (strip surrounding whitespace, uppercase); adequate for these fixtures which
// carry no internal spaces.
func normSym(s string) string {
	out := ""
	for _, r := range s {
		if r == ' ' {
			continue
		}
		if r >= 'a' && r <= 'z' {
			r -= 'a' - 'A'
		}
		out += string(r)
	}
	return out
}
