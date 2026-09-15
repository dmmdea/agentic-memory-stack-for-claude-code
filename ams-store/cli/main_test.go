package cli_test

import (
	"os"
	"testing"
	"time"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/testutil"
)

// TestMain pins every home-directory source at a throwaway directory before a single
// test runs. The reasoning, and the two live incidents behind it, are in
// testutil.RunWithSandboxHome - one implementation, so a package that adds a TestMain
// later cannot get a weaker version of it.
func TestMain(m *testing.M) { os.Exit(testutil.RunWithSandboxHome(m)) }

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
