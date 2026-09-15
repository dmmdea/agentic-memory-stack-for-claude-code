package judge

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

// RoleFileName is the one-word file under STATE_ROOT that says what this machine is.
const RoleFileName = "role"

// RoleHub is the only role allowed to apply a judge plan.
const RoleHub = "hub"

// ReadRole returns the machine's role, lower-cased and trimmed, or "" when the file is
// absent or unreadable.
//
// Unreadable folds into absent on purpose: the ONLY thing this answer is used for is
// permitting a privileged action, so "I could not tell" must mean "not the hub".
func ReadRole(stateRoot string) string {
	b, err := os.ReadFile(filepath.Join(stateRoot, RoleFileName))
	if err != nil {
		return ""
	}
	return strings.ToLower(strings.TrimSpace(string(b)))
}

// RequireHub is the hub-only guard on judge-apply.
//
// The design gives exactly one judge, on the Lenovo, against its own checkout of the hub
// - never a live store (DESIGN:261) - because "no local fallback judge: a second judge on
// the same store was the concurrency bug in another form". Two PCs applying the same
// nightly plan to their own copies would each migrate the same fact, each delete its own
// copy of the file, and push two different histories at the hub.
//
// The explicit flag exists for the hub's own first run, before its role file is seeded,
// and for tests. It is the operator asserting the role, not a way around it.
func RequireHub(stateRoot string, hubFlag bool) error {
	if hubFlag {
		return nil
	}
	role := ReadRole(stateRoot)
	if role == RoleHub {
		return nil
	}
	if role == "" {
		return fmt.Errorf("judge-apply is hub-only and this machine has no role: write %q into %s, or pass --hub",
			RoleHub, filepath.Join(stateRoot, RoleFileName))
	}
	return fmt.Errorf("judge-apply is hub-only and this machine's role is %q: a PC applies a plan only by syncing the hub's result", role)
}
