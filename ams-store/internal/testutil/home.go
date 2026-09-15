package testutil

import (
	"fmt"
	"os"
	"path/filepath"
	"testing"
)

// RunWithSandboxHome runs a package's tests with every home-directory source pinned at a
// throwaway directory, and is what a TestMain in any package that can reach
// store.DefaultRoots must call.
//
// This is not belt-and-braces. On 2026-09-15 the scaffold's stub-verb test invoked every
// verb with no --state-root; the moment `sync` stopped being a stub it resolved the REAL
// profile, opened the operator's live history repo and committed five live stores into
// it. Earlier the same day a bare `derive` defaulted to every store on the machine and
// harvested a hook: line into 248 live fact files.
//
// A test that forgets to inject its roots has to land in a temp directory rather than in
// production, and for code whose entire job is to find the user's home the only reliable
// way to guarantee that is to MOVE the home. Injecting a seam would not help: the failures
// above came from call sites that did not use the seam.
//
// store.HomeDir reads USERPROFILE on Windows and falls back to os.UserHomeDir, which reads
// HOME on unix. Both are pinned on both platforms, and HOMEDRIVE/HOMEPATH are cleared, so
// no Windows fallback can reconstruct the real profile behind our back. The fallback order
// is an implementation detail that is allowed to change; this does not depend on it.
func RunWithSandboxHome(m *testing.M) int {
	sandbox, err := os.MkdirTemp("", "ams-home-")
	if err != nil {
		fmt.Fprintf(os.Stderr, "testutil: cannot create the home sandbox: %v\n", err)
		return 1
	}
	defer func() { _ = os.RemoveAll(sandbox) }()

	for _, key := range []string{"USERPROFILE", "HOME", "HOMEDRIVE", "HOMEPATH"} {
		_ = os.Unsetenv(key)
	}
	for _, kv := range [][2]string{{"USERPROFILE", sandbox}, {"HOME", sandbox}} {
		if err := os.Setenv(kv[0], kv[1]); err != nil {
			fmt.Fprintf(os.Stderr, "testutil: %v\n", err)
			return 1
		}
	}
	for _, sub := range []string{
		filepath.Join(".claude", "projects"),
		filepath.Join(".claude", "state", "automemory"),
	} {
		_ = os.MkdirAll(filepath.Join(sandbox, sub), 0o755)
	}
	return m.Run()
}
