package cli_test

import (
	"fmt"
	"os"
	"path/filepath"
	"testing"
	"time"
)

// TestMain pins every home-directory source at a throwaway directory before a single
// test runs.
//
// This is not belt-and-braces. On 2026-09-15 the scaffold's stub-verb test invoked every
// verb with no --state-root, and the moment `sync` stopped being a stub it resolved the
// REAL profile, opened the operator's live history repo and committed 5 live stores into
// it. A test that forgets its roots must land in a temp directory, not in production, and
// the only way to guarantee that for code whose whole job is to find the user's home is
// to move the home.
//
// store.HomeDir reads USERPROFILE on Windows and falls back to os.UserHomeDir, which
// reads HOME on unix. Both are pinned, on both platforms, because the fallback order is
// an implementation detail that is allowed to change.
func TestMain(m *testing.M) {
	sandbox, err := os.MkdirTemp("", "ams-cli-home-")
	if err != nil {
		fmt.Fprintf(os.Stderr, "cli tests: cannot create the home sandbox: %v\n", err)
		os.Exit(1)
	}
	for _, key := range []string{"USERPROFILE", "HOME", "HOMEDRIVE", "HOMEPATH"} {
		os.Unsetenv(key)
	}
	// USERPROFILE and HOME both point at the sandbox; HOMEDRIVE/HOMEPATH are cleared so
	// no Windows fallback can reconstruct the real profile behind our back.
	if err := os.Setenv("USERPROFILE", sandbox); err != nil {
		fmt.Fprintf(os.Stderr, "cli tests: %v\n", err)
		os.Exit(1)
	}
	if err := os.Setenv("HOME", sandbox); err != nil {
		fmt.Fprintf(os.Stderr, "cli tests: %v\n", err)
		os.Exit(1)
	}
	for _, sub := range []string{
		filepath.Join(".claude", "projects"),
		filepath.Join(".claude", "state", "automemory"),
	} {
		_ = os.MkdirAll(filepath.Join(sandbox, sub), 0o755)
	}

	code := m.Run()
	_ = os.RemoveAll(sandbox)
	os.Exit(code)
}

// sleeperEnv makes a re-executed test binary block instead of running the suite, which is
// how these tests get a REAL live process id that is not their own.
const sleeperEnv = "AMS_CLI_TEST_SLEEPER"

// TestHelperSleeper is not a test. It is the body of the child process
// TestCLI_LockReleaseRefusesALiveForeignHolder starts: without a genuinely live foreign
// pid, "release refuses a live holder" cannot be told apart from "release refuses
// nothing", because the holder would be the test process itself.
func TestHelperSleeper(t *testing.T) {
	if os.Getenv(sleeperEnv) == "" {
		t.Skip("not the child process")
	}
	time.Sleep(2 * time.Minute)
}
