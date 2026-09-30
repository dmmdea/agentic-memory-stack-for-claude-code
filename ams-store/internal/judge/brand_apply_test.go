package judge

import (
	"testing"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/brand"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/testutil"
)

// A migrated fact carries the brand its workspace resolves to (contract C3).
func TestMigrate_CarriesTheResolvedBrand(t *testing.T) {
	lines, facts := bigStore(60)
	f := newFixture(t, "ws", lines, facts, testutil.Mem0OK)
	m, err := brand.Parse([]byte(`{"rules":[{"pattern":"^ws$","brand":"alpha"}]}`))
	if err != nil {
		t.Fatal(err)
	}

	res := f.apply([]Decision{migrate("fact3.md")}, func(o *Options) { o.Brand = m })

	if res.Migrated != 1 {
		t.Fatalf("migrated = %d, want 1", res.Migrated)
	}
	posts := f.mem.Posts()
	if len(posts) != 1 || posts[0].Metadata["brand"] != "alpha" {
		t.Fatalf("posts = %+v, want one write tagged brand=alpha", posts)
	}
}

// A brand map that routes nothing leaves the write exactly as it was before the map
// existed: no brand key, no error.
func TestMigrate_NoBrandWhenTheMapRoutesNothing(t *testing.T) {
	missing, err := brand.Load("")
	if err != nil {
		t.Fatal(err)
	}
	for name, m := range map[string]*brand.Map{"nil": nil, "empty": missing} {
		t.Run(name, func(t *testing.T) {
			lines, facts := bigStore(60)
			f := newFixture(t, "ws", lines, facts, testutil.Mem0OK)
			res := f.apply([]Decision{migrate("fact3.md")}, func(o *Options) { o.Brand = m })
			if res.Migrated != 1 {
				t.Fatalf("migrated = %d, want 1", res.Migrated)
			}
			for _, p := range f.mem.Posts() {
				if _, has := p.Metadata["brand"]; has {
					t.Errorf("a write carried brand %q although the map routes nothing", p.Metadata["brand"])
				}
			}
		})
	}
}
