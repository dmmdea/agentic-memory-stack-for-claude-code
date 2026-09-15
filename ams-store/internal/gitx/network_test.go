package gitx

import (
	"context"
	"errors"
	"path/filepath"
	"strings"
	"testing"
)

// TestGitX_NetworkSubcommandRefusedWithoutSSHHardening is the structural half of
// DESIGN:184. The rule is not "sync remembers to set GIT_SSH_COMMAND" - that rule was
// already written down and merge.Engine's Fetch/Push drifted from it anyway. What is
// enforced here is that git cannot be reached over the network from this module at all
// unless the hardened environment is on the call, so the next call site that forgets is
// refused instead of dialling with the ambient ssh config.
func TestGitX_NetworkSubcommandRefusedWithoutSSHHardening(t *testing.T) {
	dir := tempRepo(t)
	ctx := context.Background()

	// Every verb that can open a socket, in the shapes this module could plausibly use.
	cases := [][]string{
		{"fetch", "--prune", "hub"},
		{"push", "hub", "refs/heads/main:refs/heads/main"},
		{"ls-remote", "--heads", "hub", "main"},
		{"pull", "hub", "main"},
		{"clone", "--bare", "hub", filepath.Join(t.TempDir(), "clone.git")},
		{"remote", "update"},
		// A global option before the subcommand must not hide it.
		{"-c", "protocol.version=2", "fetch", "hub"},
	}
	for _, args := range cases {
		_, err := Run(ctx, Options{Dir: dir}, args...)
		if err == nil {
			t.Fatalf("git %s ran with no GIT_SSH_COMMAND: a network call must be refused", strings.Join(args, " "))
		}
		if !errors.Is(err, ErrUnhardenedNetwork) {
			t.Fatalf("git %s failed for the wrong reason: want ErrUnhardenedNetwork, got %v",
				strings.Join(args, " "), err)
		}
	}
}

// TestGitX_LocalCallsAreUntouchedByTheNetworkGuard is the negative half: the guard must
// not make the local plumbing - which is every call on the gate's critical path - depend
// on an ssh environment it has no use for.
func TestGitX_LocalCallsAreUntouchedByTheNetworkGuard(t *testing.T) {
	dir := tempRepo(t)
	ctx := context.Background()

	for _, args := range [][]string{
		{"rev-parse", "--git-dir"},
		{"config", "user.name", "automemory"},
		{"hash-object", "-w", "--stdin"},
		// `remote add` is local: only `remote update` dials.
		{"remote", "add", "hub", filepath.Join(t.TempDir(), "hub.git")},
	} {
		if _, err := Run(ctx, Options{Dir: dir, StdinRaw: []byte("x\n")}, args...); err != nil {
			t.Fatalf("local call git %s was refused: %v", strings.Join(args, " "), err)
		}
	}
}

// TestGitX_NetworkCallWithHardeningIsNotRefused pins that the guard reads the environment
// the caller actually built, not the absence of one: a call carrying GIT_SSH_COMMAND gets
// through to git and fails, if at all, for git's own reasons.
func TestGitX_NetworkCallWithHardeningIsNotRefused(t *testing.T) {
	dir := tempRepo(t)
	ctx := context.Background()

	_, err := Run(ctx, Options{Dir: dir, ExtraEnv: NetworkEnv(filepath.Join(t.TempDir(), "state"))},
		"fetch", "--prune", "no-such-remote-configured")
	if err == nil {
		t.Fatal("fetching an undefined remote should have failed in git")
	}
	if errors.Is(err, ErrUnhardenedNetwork) {
		t.Fatalf("a hardened network call was refused by the guard: %v", err)
	}
}

// TestGitX_SSHCommandNeverPromptsAndNeverHangs pins the option set itself (DESIGN:184).
func TestGitX_SSHCommandNeverPromptsAndNeverHangs(t *testing.T) {
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
	env := NetworkEnv(filepath.Join("state", "root"))
	if len(env) != 1 || !strings.HasPrefix(env[0], "GIT_SSH_COMMAND=") {
		t.Fatalf("NetworkEnv must be exactly the hardened GIT_SSH_COMMAND, got %v", env)
	}
}

// TestGitX_NetworkSubcommandReadsPastGlobalOptions pins the argv scan itself, because the
// guard is only as good as its ability to find the subcommand: --git-dir, --work-tree and
// `-c key=value` all sit in front of it on real calls.
func TestGitX_NetworkSubcommandReadsPastGlobalOptions(t *testing.T) {
	for _, tc := range []struct {
		args []string
		want string
	}{
		{args: []string{"--git-dir=/x", "--work-tree=/y", "fetch", "--prune", "hub"}, want: "fetch"},
		{args: []string{"-c", "protocol.version=2", "push", "hub", "main"}, want: "push"},
		{args: []string{"-C", "fetch", "rev-parse", "HEAD"}, want: ""},
		{args: []string{"remote", "update"}, want: "remote update"},
		{args: []string{"remote", "add", "hub", "somewhere"}, want: ""},
		{args: []string{"--git-dir=/x", "ls-remote", "--heads", "hub"}, want: "ls-remote"},
		{args: []string{"merge-tree", "--write-tree", "a", "b"}, want: ""},
		{args: []string{"commit", "-q", "-m", "push"}, want: ""},
	} {
		if got := NetworkSubcommand(tc.args); got != tc.want {
			t.Fatalf("NetworkSubcommand(%v) = %q, want %q", tc.args, got, tc.want)
		}
	}
}
