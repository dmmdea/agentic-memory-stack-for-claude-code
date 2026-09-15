package merge_test

// The fleet harness: a bare hub plus N simulated PCs, each with its own PROJECTS_ROOT,
// its own out-of-tree history repo and its own machine id. It is the Go form of the
// spec fixtures in the design's Phase 3 section - "three writers", "delete on A,
// untouched on B", and the rest all need more than one PC to mean anything, and a merge
// engine tested against a fake git proves nothing about the git the fleet runs.

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/gitx"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/merge"
)

const hubRemote = "hub"

type fleet struct {
	t   *testing.T
	dir string
	hub string
	pcs map[string]*pc
}

type pc struct {
	t        *testing.T
	name     string
	root     string
	projects string
	stateDir string
	gitDir   string
	machine  string
	eng      *merge.Engine
	clock    time.Time
}

func newFleet(t *testing.T, names ...string) *fleet {
	t.Helper()
	if _, err := exec.LookPath("git"); err != nil {
		t.Skip("git is not on PATH")
	}
	if v, err := gitx.Require(context.Background()); err != nil {
		t.Skipf("installed git does not meet the ams-store floor: %v", err)
	} else {
		t.Logf("git %s", v.String())
	}
	root := t.TempDir()
	f := &fleet{t: t, dir: root, hub: filepath.Join(root, "hub.git"), pcs: map[string]*pc{}}

	// An empty hooks path for every repo in the fixture. The operator's global
	// core.hooksPath carries a pre-push leak scanner that has no business running
	// against a throwaway temp repo, and a test that depends on the developer's global
	// git config is not a test of this engine.
	mustMkdir(t, filepath.Join(root, "nohooks"))

	mustRun(t, root, "git", "init", "-q", "--bare", "-b", "main", f.hub)
	mustRun(t, root, "git", "--git-dir="+f.hub, "config", "receive.denyNonFastForwards", "true")
	mustRun(t, root, "git", "--git-dir="+f.hub, "config", "core.hooksPath", filepath.Join(root, "nohooks"))

	base := time.Date(2026, 9, 15, 12, 0, 0, 0, time.UTC)
	for i, n := range names {
		f.pcs[n] = f.newPC(n, base.Add(time.Duration(i)*time.Minute))
	}
	return f
}

func (f *fleet) newPC(name string, clock time.Time) *pc {
	t := f.t
	t.Helper()
	root := filepath.Join(f.dir, name)
	p := &pc{
		t:        t,
		name:     name,
		root:     root,
		projects: filepath.Join(root, "projects"),
		stateDir: filepath.Join(root, "state"),
		machine:  name + "-000000",
		clock:    clock,
	}
	p.gitDir = filepath.Join(p.stateDir, "history.git")
	mustMkdir(t, p.projects)
	mustMkdir(t, p.stateDir)

	p.eng = &merge.Engine{
		GitDir:    p.gitDir,
		WorkTree:  p.projects,
		MachineID: p.machine,
		// The state root is what the hardened network environment is built from, so a
		// fixture PC that talks to the hub carries it exactly as a real PC does.
		StateRoot: p.stateDir,
		Now:       func() time.Time { return p.clock },
	}
	if err := p.eng.Initialize(context.Background()); err != nil {
		t.Fatalf("%s: Initialize: %v", name, err)
	}
	mustRun(t, root, "git", "--git-dir="+p.gitDir, "config", "core.hooksPath", filepath.Join(f.dir, "nohooks"))
	mustRun(t, root, "git", "--git-dir="+p.gitDir, "remote", "add", hubRemote, f.hub)
	return p
}

// tick moves this PC's clock forward so commit times are deterministic and ordered.
func (p *pc) tick(d time.Duration) { p.clock = p.clock.Add(d) }

func (p *pc) storeDir(ws string) string { return filepath.Join(p.projects, ws, "memory") }

func (p *pc) write(ws, name, content string) {
	p.t.Helper()
	dir := p.storeDir(ws)
	mustMkdir(p.t, dir)
	if err := os.WriteFile(filepath.Join(dir, name), []byte(content), 0o644); err != nil {
		p.t.Fatal(err)
	}
}

func (p *pc) writeRaw(rel, content string) {
	p.t.Helper()
	full := filepath.Join(p.projects, filepath.FromSlash(rel))
	mustMkdir(p.t, filepath.Dir(full))
	if err := os.WriteFile(full, []byte(content), 0o644); err != nil {
		p.t.Fatal(err)
	}
}

func (p *pc) remove(ws, name string) {
	p.t.Helper()
	if err := os.Remove(filepath.Join(p.storeDir(ws), name)); err != nil {
		p.t.Fatal(err)
	}
}

func (p *pc) read(ws, name string) (string, bool) {
	b, err := os.ReadFile(filepath.Join(p.storeDir(ws), name))
	if err != nil {
		return "", false
	}
	return string(b), true
}

func (p *pc) exists(ws, name string) bool {
	_, err := os.Stat(filepath.Join(p.storeDir(ws), name))
	return err == nil
}

// commit stages every store plus the shared state path and commits at this PC's clock.
func (p *pc) commit(message string, workspaces ...string) string {
	p.t.Helper()
	oid, changed, err := p.eng.Commit(context.Background(), merge.CommitOptions{
		Message:    message,
		Kind:       "local",
		Workspaces: workspaces,
		Date:       p.clock,
	})
	if err != nil {
		p.t.Fatalf("%s: commit: %v", p.name, err)
	}
	if !changed {
		return ""
	}
	return oid
}

func (p *pc) fetch() {
	p.t.Helper()
	if err := p.eng.Fetch(context.Background(), hubRemote); err != nil {
		p.t.Fatalf("%s: fetch: %v", p.name, err)
	}
}

func (p *pc) push() merge.PushResult {
	p.t.Helper()
	r, err := p.eng.Push(context.Background(), hubRemote)
	if err != nil {
		p.t.Fatalf("%s: push: %v", p.name, err)
	}
	return r
}

// syncOnce is the merge-side half of `sync --once`: commit locally FIRST, then
// fetch/merge/materialize/push with the bounded loop. sync (task 5) owns the lock, the
// receipts and the remote policy; this is the part the merge engine exports to it.
func (p *pc) syncOnce(message string, mo merge.MaterializeOptions, workspaces ...string) *merge.Report {
	p.t.Helper()
	p.commit(message, workspaces...)
	var last *merge.Report
	for attempt := 0; attempt < 3; attempt++ {
		p.fetch()
		rep, err := p.eng.Round(context.Background(), merge.RoundOptions{
			TheirsRef:   "refs/remotes/" + hubRemote + "/main",
			Materialize: mo,
			Date:        p.clock,
		})
		if err != nil {
			p.t.Fatalf("%s: round: %v", p.name, err)
		}
		last = rep
		res := p.push()
		if res.OK {
			return last
		}
		if !res.NonFastForward {
			p.t.Fatalf("%s: push failed for a reason other than non-fast-forward: %s", p.name, res.Stderr)
		}
	}
	p.t.Fatalf("%s: push loop exhausted after 3 attempts", p.name)
	return last
}

// mergeOnly is fetch + Round WITHOUT the local commit: it models the window the gate
// leaves open, where a live session has written a fact file that has not been committed
// yet. That window is exactly what the materialize live-session guard protects.
func (p *pc) mergeOnly(mo merge.MaterializeOptions) *merge.Report {
	p.t.Helper()
	p.fetch()
	rep, err := p.eng.Round(context.Background(), merge.RoundOptions{
		TheirsRef:   "refs/remotes/" + hubRemote + "/main",
		Materialize: mo,
		Date:        p.clock,
	})
	if err != nil {
		p.t.Fatalf("%s: round: %v", p.name, err)
	}
	return rep
}

// mo builds the materialize options for this PC with no live session and no derive.
func (p *pc) mo() merge.MaterializeOptions {
	return merge.MaterializeOptions{
		StateRoot: p.stateDir,
		Live: merge.Liveness{
			ProjectsRoot: p.projects,
			Now:          func() time.Time { return p.clock },
		},
	}
}

func mustMkdir(t *testing.T, dir string) {
	t.Helper()
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
}

func mustRun(t *testing.T, dir, name string, args ...string) string {
	t.Helper()
	cmd := exec.Command(name, args...)
	cmd.Dir = dir
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("%s %s: %v\n%s", name, strings.Join(args, " "), err, out)
	}
	return string(out)
}

func (p *pc) git(args ...string) string {
	p.t.Helper()
	full := append([]string{"--git-dir=" + p.gitDir, "--work-tree=" + p.projects}, args...)
	return mustRun(p.t, p.root, "git", full...)
}

// fact renders a fact file with the given frontmatter fields and body.
func fact(name, desc, hook, body string, extra ...string) string {
	var b strings.Builder
	b.WriteString("---\n")
	b.WriteString("name: " + name + "\n")
	b.WriteString("description: \"" + desc + "\"\n")
	if hook != "" {
		b.WriteString("hook: \"" + hook + "\"\n")
	}
	for _, e := range extra {
		b.WriteString(e + "\n")
	}
	b.WriteString("metadata:\n  node_type: memory\n  type: project\n  modified: 2026-08-01\n")
	b.WriteString("---\n\n")
	b.WriteString(body)
	if !strings.HasSuffix(body, "\n") {
		b.WriteString("\n")
	}
	return b.String()
}

func longBody(marker string) string {
	lines := make([]string, 0, 12)
	for i := 1; i <= 12; i++ {
		if i == 6 {
			lines = append(lines, marker)
			continue
		}
		lines = append(lines, "body line "+string(rune('a'+i-1)))
	}
	return strings.Join(lines, "\n") + "\n"
}
