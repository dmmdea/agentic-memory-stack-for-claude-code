package cli_test

import (
	"os"
	"strconv"
	"testing"
	"time"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/cli"
	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/testutil"
)

// TestMain pins every home-directory source at a throwaway directory before a single
// test runs. The reasoning, and the two live incidents behind it, are in
// testutil.RunWithSandboxHome - one implementation, so a package that adds a TestMain
// later cannot get a weaker version of it.
func TestMain(m *testing.M) { os.Exit(testutil.RunWithSandboxHome(m)) }

// isolateLocks points every per-PC lock the verbs take at names nothing outside this
// test can hold, and restores the production names when the test ends.
//
// Without it the verbs take `Local\ams-store` and the PowerShell compactor's
// `Local\ams-memory-compact` - real objects on the operator's desktop. Measured at the
// branch this was written on: with a sibling process holding the legacy mutex, sixteen
// tests in this package fail on exit 4, and several others pass while asserting nothing,
// because the verb skipped as a contender before it produced the output under test. The
// suite's result must not depend on whether a nightly compaction happens to be running.
//
// The pid is in the name because two checkouts run this package at once during a repair
// round; the test name is in it because two tests in one binary must not contend either.
// The re-executed child process (TestHelperSleeper) needs no override: it only sleeps,
// and takes no lock at all.
func isolateLocks(t *testing.T) {
	t.Helper()
	suffix := "-" + strconv.Itoa(os.Getpid()) + "-" + t.Name()
	t.Cleanup(cli.UseTestLockNames(`Local\ams-store-test`+suffix, `Local\ams-store-test-legacy`+suffix))
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
