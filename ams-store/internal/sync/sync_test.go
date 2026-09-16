package sync

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/store"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/testutil"
)

// bareHub creates a bare repository configured the way the hub is (blueprint section 8):
// a non-fast-forward push is REJECTED, which is what makes the push loop's retry a real
// behaviour rather than a formality.
func bareHub(t *testing.T, dir string) string {
	t.Helper()
	testutil.RequireGit(t)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	mustGit(t, "", "init", "--bare", "-q", "-b", Branch, dir)
	for _, kv := range [][2]string{
		{"receive.denyNonFastForwards", "true"},
		{"receive.denyDeletes", "true"},
		{"core.autocrlf", "false"},
		{"core.safecrlf", "false"},
		{"merge.renames", "false"},
		{"gc.auto", "0"},
	} {
		mustGit(t, "", "--git-dir="+dir, "config", kv[0], kv[1])
	}
	return dir
}

func mustGit(t *testing.T, dir string, args ...string) string {
	t.Helper()
	cmd := exec.Command("git", args...)
	if dir != "" {
		cmd.Dir = dir
	}
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("git %v: %v\n%s", args, err, out)
	}
	return string(out)
}

func tryGit(dir string, args ...string) (string, error) {
	cmd := exec.Command("git", args...)
	if dir != "" {
		cmd.Dir = dir
	}
	out, err := cmd.CombinedOutput()
	return string(out), err
}

// pcFixture builds one PC: a sandbox with one store and an initialized history repo.
func pcFixture(t *testing.T, workspace string, facts map[string]string) (*testutil.Sandbox, Repo, Options) {
	t.Helper()
	testutil.RequireGit(t)
	sb := testutil.NewSandbox(t)
	lines := []string{"# Memory Index", ""}
	for name := range facts {
		lines = append(lines, "- ["+name+"]("+name+")")
	}
	sb.AddStore(workspace, lines, facts)
	roots := store.Roots{ProjectsRoot: sb.ProjectsRoot, StateRoot: sb.StateRoot}
	repo := NewRepo(roots)
	if err := repo.Initialize(context.Background()); err != nil {
		t.Fatalf("Initialize: %v", err)
	}
	opt := Options{
		Roots:     roots,
		MachineID: "pc-" + workspace,
		Policy:    RemotePolicy{AllowLocalPath: true},
		Version:   "test",
	}
	return sb, repo, opt
}

// fakeMerger stands in for internal/merge, which is built in parallel. It records every
// call and runs a hook, so a test can make the hub move underneath the loop.
type fakeMerger struct {
	calls  int
	before func(call int)
	result func(call int) MergeResult
}

func (f *fakeMerger) Merge(_ context.Context, _ MergeOptions) (MergeResult, error) {
	f.calls++
	if f.before != nil {
		f.before(f.calls)
	}
	if f.result != nil {
		return f.result(f.calls), nil
	}
	return MergeResult{UpToDate: true}, nil
}

// TestSync_OfflineCommitThenResume is the spec fixture "offline commit then resume"
// (blueprint section 10.1).
//
// The hub is configured but UNREACHABLE for the first pass. That is the shape that makes
// the test able to fail: with the local commit moved after the fetch - the mutation
// section 10.2 names - the first pass would return with nothing committed, and the work
// done offline would exist only on disk, invisible to history and unrecoverable after any
// later merge.
func TestSync_OfflineCommitThenResume(t *testing.T) {
	sb, repo, opt := pcFixture(t, "ws", map[string]string{
		"a.md": testutil.FactFile("a", "written offline", "project", "body a"),
	})
	ctx := context.Background()

	hubPath := filepath.Join(sb.Root, "hub.git")
	mustGit(t, "", "--git-dir="+repo.GitDir, "remote", "add", HubRemote, filepath.ToSlash(hubPath))

	// --- offline: the hub path does not exist yet, so the fetch cannot succeed.
	first := Once(ctx, opt)
	if first.ExitCode != exitNetwork {
		t.Fatalf("an unreachable hub gave exit %d, want %d", first.ExitCode, exitNetwork)
	}
	if !first.Receipt.Offline {
		t.Fatalf("the receipt does not record the pass as offline: %+v", first.Receipt)
	}
	offlineCommit := first.Receipt.LocalCommit
	if offlineCommit == "" {
		t.Fatal("nothing was committed while offline; history must be kept without connectivity")
	}
	if ok, err := repo.HasFile(ctx, offlineCommit, "ws/memory/a.md"); err != nil || !ok {
		t.Fatalf("the offline commit does not carry the fact file: %v %v", ok, err)
	}

	// --- the hub comes back.
	bareHub(t, hubPath)
	opt.Merger = &fakeMerger{}
	second := Once(ctx, opt)
	if second.ExitCode != exitOK {
		t.Fatalf("the resumed pass gave exit %d (%v)", second.ExitCode, second.Err)
	}
	if !second.Receipt.Pushed {
		t.Fatalf("the resumed pass did not push: %+v", second.Receipt)
	}

	// The work done offline is now on the hub, by the commit id it was made under.
	out, err := tryGit("", "--git-dir="+hubPath, "cat-file", "-e", offlineCommit+"^{commit}")
	if err != nil {
		t.Fatalf("the offline commit never reached the hub: %v\n%s", err, out)
	}
	ls := mustGit(t, "", "--git-dir="+hubPath, "ls-tree", "-r", "--name-only", Branch)
	if !strings.Contains(ls, "ws/memory/a.md") {
		t.Fatalf("the hub tree is missing the fact file:\n%s", ls)
	}
	if strings.Contains(ls, store.IndexName) {
		t.Fatalf("MEMORY.md reached the hub; it is derived, never synced:\n%s", ls)
	}
}

// TestSync_PushLoopUnderConcurrentPush_BoundedAtThree is the spec fixture "push loop
// under a concurrent push" (blueprint section 10.1).
//
// A second PC pushes a new commit before every one of this PC's push attempts, so every
// push is rejected as non-fast-forward. The loop must make exactly three attempts and
// then stop. The mutation "loop forever" turns this test into a hang, which is the
// loudest possible red.
func TestSync_PushLoopUnderConcurrentPush_BoundedAtThree(t *testing.T) {
	sbA, repoA, optA := pcFixture(t, "ws", map[string]string{
		"a.md": testutil.FactFile("a", "from A", "project", "body a"),
	})
	ctx := context.Background()

	hubPath := filepath.Join(sbA.Root, "hub.git")
	bareHub(t, hubPath)
	hubURL := filepath.ToSlash(hubPath)
	mustGit(t, "", "--git-dir="+repoA.GitDir, "remote", "add", HubRemote, hubURL)

	// PC A seeds the hub.
	optA.Merger = &fakeMerger{}
	seed := Once(ctx, optA)
	if !seed.Receipt.Pushed {
		t.Fatalf("the seed push did not happen: %+v (%v)", seed.Receipt, seed.Err)
	}

	// PC B: a second clone of the same hub, used as the concurrent writer.
	sbB, repoB, optB := pcFixture(t, "ws", map[string]string{
		"b.md": testutil.FactFile("b", "from B", "project", "body b"),
	})
	mustGit(t, "", "--git-dir="+repoB.GitDir, "remote", "add", HubRemote, hubURL)
	mustGit(t, "", "--git-dir="+repoB.GitDir, "fetch", HubRemote, Branch)
	mustGit(t, "", "--git-dir="+repoB.GitDir, "--work-tree="+repoB.WorkTree, "reset", "--hard", "-q", "refs/remotes/"+HubRemote+"/"+Branch)

	bPush := func(call int) {
		p := filepath.Join(sbB.ProjectsRoot, "ws", "memory", "b.md")
		body := testutil.FactFile("b", "from B", "project", "body b revision "+itoa(call))
		if err := os.WriteFile(p, []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
		if err := repoB.Stage(ctx, "ws"); err != nil {
			t.Fatal(err)
		}
		if _, err := repoB.Commit(ctx, "concurrent write "+itoa(call), optB.MachineID, "local"); err != nil {
			t.Fatal(err)
		}
		if out, err := tryGit("", "--git-dir="+repoB.GitDir, "--work-tree="+repoB.WorkTree, "push", HubRemote, Branch); err != nil {
			t.Fatalf("the concurrent push failed: %v\n%s", err, out)
		}
	}

	// A's merger is deliberately inert: it models the loser that keeps re-merging and
	// keeps losing. Every push it makes is therefore a non-fast-forward.
	merger := &fakeMerger{before: func(call int) { bPush(call) }}
	optA.Merger = merger

	// New local work on A so it has something to push.
	if err := os.WriteFile(filepath.Join(sbA.ProjectsRoot, "ws", "memory", "a.md"),
		[]byte(testutil.FactFile("a", "from A", "project", "body a revised")), 0o644); err != nil {
		t.Fatal(err)
	}

	res := Once(ctx, optA)
	if res.Receipt.Attempts != MaxPushAttempts {
		t.Fatalf("the loop made %d attempts, want exactly %d", res.Receipt.Attempts, MaxPushAttempts)
	}
	if res.Receipt.Status != StatusExhausted {
		t.Fatalf("status %q, want %q", res.Receipt.Status, StatusExhausted)
	}
	if res.ExitCode != exitNetwork {
		t.Fatalf("exit %d, want %d", res.ExitCode, exitNetwork)
	}
	if merger.calls != MaxPushAttempts {
		t.Fatalf("the merge engine was called %d times, want %d - the loser must re-merge on every attempt",
			merger.calls, MaxPushAttempts)
	}
	// The hub still carries B's work: a losing pusher never clobbers the winner.
	ls := mustGit(t, "", "--git-dir="+hubPath, "ls-tree", "-r", "--name-only", Branch)
	if !strings.Contains(ls, "ws/memory/b.md") {
		t.Fatalf("the hub lost the concurrent writer's file:\n%s", ls)
	}
}

// TestSync_NoRemoteIsAFullSuccessfulOfflinePass: a PC with no hub is not broken. It
// derives, commits locally and exits 0, and the receipt says why.
func TestSync_NoRemoteIsAFullSuccessfulOfflinePass(t *testing.T) {
	_, repo, opt := pcFixture(t, "ws", map[string]string{
		"a.md": testutil.FactFile("a", "d", "project", "body a"),
	})
	res := Once(context.Background(), opt)
	if res.ExitCode != exitOK {
		t.Fatalf("exit %d (%v), want 0", res.ExitCode, res.Err)
	}
	if res.Receipt.Status != StatusLocal {
		t.Fatalf("status %q, want %q", res.Receipt.Status, StatusLocal)
	}
	if res.Receipt.LocalCommit == "" {
		t.Fatal("no local commit was made")
	}
	if ok, err := repo.HasFile(context.Background(), res.Receipt.LocalCommit, "ws/memory/a.md"); err != nil || !ok {
		t.Fatalf("the local commit is missing the fact file: %v %v", ok, err)
	}
}

// TestSync_RefusesARemoteThatIsNotTheHub: the remote policy is a refusal (exit 3), not a
// warning. These stores hold credentials; a second remote is a path off the tailnet.
func TestSync_RefusesARemoteThatIsNotTheHub(t *testing.T) {
	sb, repo, opt := pcFixture(t, "ws", map[string]string{
		"a.md": testutil.FactFile("a", "d", "project", "body a"),
	})
	opt.Policy = RemotePolicy{} // a PC: no local paths permitted
	hubPath := filepath.ToSlash(filepath.Join(sb.Root, "hub.git"))
	bareHub(t, filepath.Join(sb.Root, "hub.git"))
	mustGit(t, "", "--git-dir="+repo.GitDir, "remote", "add", HubRemote, hubPath)

	res := Once(context.Background(), opt)
	if res.ExitCode != exitRefused {
		t.Fatalf("exit %d (%v), want %d", res.ExitCode, res.Err, exitRefused)
	}
	if res.Receipt.Status != StatusRefused {
		t.Fatalf("status %q, want %q", res.Receipt.Status, StatusRefused)
	}
	// It still committed locally first: a refusal to push is not a refusal to remember.
	if res.Receipt.LocalCommit == "" {
		t.Fatal("the refused pass made no local commit")
	}
}

// TestSync_ReceiptCarriesResurrectedAndConflicts: what the merge engine reports has to
// reach the receipt, because lint reads the receipts and the operator reads lint.
func TestSync_ReceiptCarriesResurrectedAndConflicts(t *testing.T) {
	sb, repo, opt := pcFixture(t, "ws", map[string]string{
		"a.md": testutil.FactFile("a", "d", "project", "body a"),
	})
	ctx := context.Background()
	hubPath := filepath.Join(sb.Root, "hub.git")
	bareHub(t, hubPath)
	mustGit(t, "", "--git-dir="+repo.GitDir, "remote", "add", HubRemote, filepath.ToSlash(hubPath))

	// Seed the hub first: with no branch on the hub there is nothing to merge against,
	// and the merge engine is never called.
	opt.Merger = &fakeMerger{}
	if seed := Once(ctx, opt); !seed.Receipt.Pushed {
		t.Fatalf("the seed push did not happen: %+v (%v)", seed.Receipt, seed.Err)
	}
	if err := os.WriteFile(filepath.Join(sb.ProjectsRoot, "ws", "memory", "a.md"),
		[]byte(testutil.FactFile("a", "d", "project", "body a revised")), 0o644); err != nil {
		t.Fatal(err)
	}

	opt.Merger = &fakeMerger{result: func(int) MergeResult {
		return MergeResult{
			UpToDate:           true,
			Resurrected:        []Resurrection{{Path: "ws/memory/a.md", Side: "ours", Reason: "modify/delete"}},
			ConflictsInHistory: []ConflictRef{{Path: "ws/memory/a.md", Commit: "abc1234"}},
		}
	}}
	res := Once(ctx, opt)
	if res.ExitCode != exitConflict {
		t.Fatalf("exit %d, want %d: a conflict recorded in history is advisory-loud", res.ExitCode, exitConflict)
	}
	if len(res.Receipt.Resurrected) != 1 || res.Receipt.Resurrected[0] != "ws/memory/a.md" {
		t.Fatalf("resurrected not carried into the receipt: %+v", res.Receipt.Resurrected)
	}
	if len(res.Receipt.ConflictsInHistory) != 1 || res.Receipt.ConflictsInHistory[0].Commit != "abc1234" {
		t.Fatalf("conflict-in-history not carried into the receipt: %+v", res.Receipt.ConflictsInHistory)
	}
	if !res.Receipt.Pushed {
		t.Fatal("a conflict recorded in history must still push: the work tree is correct")
	}

	rows, err := ReadReceipts(ReceiptPath(opt.Roots.StateRoot), 0)
	if err != nil || len(rows) == 0 {
		t.Fatalf("receipts were not written: %v %v", len(rows), err)
	}
	last := rows[len(rows)-1]
	if last.Machine != opt.MachineID || last.Version != "test" {
		t.Fatalf("the receipt does not attribute the run: %+v", last)
	}
}

// TestSync_ClearsTheDirtyMarkerAfterTheLocalCommit: the marker's job is done once the
// work is in history, not once it is pushed.
func TestSync_ClearsTheDirtyMarkerAfterTheLocalCommit(t *testing.T) {
	_, _, opt := pcFixture(t, "ws", map[string]string{
		"a.md": testutil.FactFile("a", "d", "project", "body a"),
	})
	if err := MarkDirty(opt.Roots.StateRoot); err != nil {
		t.Fatal(err)
	}
	if !IsDirty(opt.Roots.StateRoot) {
		t.Fatal("MarkDirty did not create the marker")
	}
	Once(context.Background(), opt)
	if IsDirty(opt.Roots.StateRoot) {
		t.Fatal("the dirty marker survived a completed pass")
	}
}

// TestSync_SSHCommandNeverPromptsAndNeverHangs pins DESIGN:184. Every option here is
// load-bearing: a prompt on an unattended box hangs forever, and accepting an unknown
// host key on trust is how a redirected tailnet name becomes a silent exfiltration.
func TestSync_SSHCommandNeverPromptsAndNeverHangs(t *testing.T) {
	cmd := SSHCommand(filepath.Join("state", "root"))
	for _, want := range []string{
		"BatchMode=yes",
		"ConnectTimeout=2",
		"StrictHostKeyChecking=yes",
		"UserKnownHostsFile=",
		KnownHostsFile,
	} {
		if !strings.Contains(cmd, want) {
			t.Fatalf("GIT_SSH_COMMAND %q is missing %q", cmd, want)
		}
	}
}

func itoa(i int) string {
	if i == 0 {
		return "0"
	}
	var b [12]byte
	p := len(b)
	for i > 0 {
		p--
		b[p] = byte('0' + i%10)
		i /= 10
	}
	return string(b[p:])
}
