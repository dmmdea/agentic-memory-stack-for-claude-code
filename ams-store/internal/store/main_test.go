package store_test

import (
	"os"
	"testing"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/testutil"
)

// internal/store is where DefaultRoots lives, so a test here that calls it reaches the
// operator's real profile by construction. See testutil.RunWithSandboxHome.
func TestMain(m *testing.M) { os.Exit(testutil.RunWithSandboxHome(m)) }
