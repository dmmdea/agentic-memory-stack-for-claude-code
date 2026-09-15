package gitx_test

import (
	"context"
	"strings"
	"testing"
	"time"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/gitx"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/testutil"
)

func TestGitX_ParseVersion(t *testing.T) {
	for _, tc := range []struct {
		in              string
		maj, min, patch int
		wantErr         bool
	}{
		{in: "git version 2.38.0", maj: 2, min: 38, patch: 0},
		{in: "git version 2.55.0.windows.2", maj: 2, min: 55, patch: 0},
		{in: "git version 2.45.2\n", maj: 2, min: 45, patch: 2},
		{in: "git version 2.9", maj: 2, min: 9, patch: 0},
		{in: "not a version", wantErr: true},
		{in: "", wantErr: true},
	} {
		v, err := gitx.ParseVersion(tc.in)
		if tc.wantErr {
			if err == nil {
				t.Errorf("ParseVersion(%q) = %v, want an error", tc.in, v)
			}
			continue
		}
		if err != nil {
			t.Errorf("ParseVersion(%q): %v", tc.in, err)
			continue
		}
		if v.Major != tc.maj || v.Minor != tc.min || v.Patch != tc.patch {
			t.Errorf("ParseVersion(%q) = %d.%d.%d, want %d.%d.%d", tc.in, v.Major, v.Minor, v.Patch, tc.maj, tc.min, tc.patch)
		}
	}
}

func TestGitX_VersionAtLeastGuardsTheMergeTreeFloor(t *testing.T) {
	for _, tc := range []struct {
		v    gitx.Version
		want bool
	}{
		{gitx.Version{Major: 2, Minor: 38}, true},
		{gitx.Version{Major: 2, Minor: 55}, true},
		{gitx.Version{Major: 3, Minor: 0}, true},
		{gitx.Version{Major: 2, Minor: 37}, false},
		{gitx.Version{Major: 1, Minor: 99}, false},
	} {
		if got := tc.v.AtLeast(gitx.MinMajor, gitx.MinMinor); got != tc.want {
			t.Errorf("(%s).AtLeast(%d,%d) = %v, want %v", tc.v.String(), gitx.MinMajor, gitx.MinMinor, got, tc.want)
		}
	}
}

func TestGitX_RequireMeetsTheInstalledFloor(t *testing.T) {
	testutil.RequireGit(t)
	v, err := gitx.Require(context.Background())
	if err != nil {
		t.Fatalf("the installed git must be at least %d.%d for merge-tree --write-tree: %v", gitx.MinMajor, gitx.MinMinor, err)
	}
	if !v.AtLeast(gitx.MinMajor, gitx.MinMinor) {
		t.Fatalf("Require returned %s without error", v.String())
	}
}

func TestGitX_RunCapturesStdoutAndZeroExit(t *testing.T) {
	testutil.RequireGit(t)
	res, err := gitx.Run(context.Background(), gitx.Options{}, "--version")
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	if res.Code != 0 {
		t.Errorf("Code = %d, want 0", res.Code)
	}
	if !strings.HasPrefix(res.Stdout, "git version") {
		t.Errorf("Stdout = %q, want a git version line", res.Stdout)
	}
}

// Git's plumbing uses a non-zero exit as signal routinely, so a classifier - not "any
// error is a failure" - is what keeps a normal answer from reading as a breakage.
func TestGitX_OkExitClassifierAcceptsANonZeroCode(t *testing.T) {
	testutil.RequireGit(t)
	s := testutil.NewSandbox(t)
	gitDir := s.InitHistory()

	opt := gitx.Options{GitDir: gitDir, WorkTree: s.ProjectsRoot}
	if _, err := gitx.Run(context.Background(), opt, "rev-parse", "--verify", "--quiet", "refs/heads/nope"); err == nil {
		t.Fatal("a missing ref must be an error under the default classifier")
	}

	opt.OkExit = gitx.OkExitCodes(0, 1)
	res, err := gitx.Run(context.Background(), opt, "rev-parse", "--verify", "--quiet", "refs/heads/nope")
	if err != nil {
		t.Fatalf("exit 1 must be accepted when the classifier says so: %v", err)
	}
	if res.Code != 1 {
		t.Errorf("Code = %d, want 1", res.Code)
	}
}

func TestGitX_ExitErrorCarriesTheResult(t *testing.T) {
	testutil.RequireGit(t)
	s := testutil.NewSandbox(t)
	gitDir := s.InitHistory()
	_, err := gitx.Run(context.Background(), gitx.Options{GitDir: gitDir, WorkTree: s.ProjectsRoot},
		"cat-file", "-e", "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef")
	if err == nil {
		t.Fatal("want an error")
	}
	var ee *gitx.ExitError
	if !asExitError(err, &ee) {
		t.Fatalf("error = %T, want *gitx.ExitError", err)
	}
	if ee.Result.Code == 0 {
		t.Error("ExitError must carry the non-zero code")
	}
	if len(ee.Result.Args) == 0 {
		t.Error("ExitError must carry the argv that produced it")
	}
}

func TestGitX_ArgvIsPassedAsASliceWithGitDirAndWorkTreeFirst(t *testing.T) {
	testutil.RequireGit(t)
	s := testutil.NewSandbox(t)
	gitDir := s.InitHistory()
	// A path with a space proves the argv is a slice, never a joined command line.
	spaced := s.AddStore("ws with space", []string{"- [A](a.md)"}, map[string]string{
		"a.md": testutil.FactFile("a", "d", "", ""),
	})
	_ = spaced
	res, err := gitx.Run(context.Background(), gitx.Options{GitDir: gitDir, WorkTree: s.ProjectsRoot},
		"add", "-A", "-f", "--", "ws with space/memory")
	if err != nil {
		t.Fatalf("Run: %v (stderr: %s)", err, res.Stderr)
	}
	res, err = gitx.Run(context.Background(), gitx.Options{GitDir: gitDir, WorkTree: s.ProjectsRoot},
		"diff", "--cached", "--name-only")
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	if !strings.Contains(res.Stdout, "ws with space/memory/a.md") {
		t.Errorf("staged paths = %q, want the spaced store's fact file", res.Stdout)
	}
}

func TestGitX_CancelledContextDoesNotHang(t *testing.T) {
	testutil.RequireGit(t)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	done := make(chan struct{})
	go func() {
		_, _ = gitx.Run(ctx, gitx.Options{Timeout: time.Second}, "--version")
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(20 * time.Second):
		t.Fatal("Run did not return on a cancelled context: an unset WaitDelay lets a killed process with open pipes hang Wait forever")
	}
}

func asExitError(err error, target **gitx.ExitError) bool {
	for err != nil {
		if e, ok := err.(*gitx.ExitError); ok {
			*target = e
			return true
		}
		u, ok := err.(interface{ Unwrap() error })
		if !ok {
			return false
		}
		err = u.Unwrap()
	}
	return false
}
