package gitx

import (
	"errors"
	"path/filepath"
	"strings"
)

// KnownHostsFile is the pre-seeded known_hosts under STATE_ROOT.
const KnownHostsFile = "known_hosts"

// ErrUnhardenedNetwork is returned INSTEAD of running git when a network subcommand is
// asked for without the hardened environment. It is a refusal, not a failure: nothing was
// dialled, no key was offered and no host key was accepted on trust.
var ErrUnhardenedNetwork = errors.New("a network git call must carry GIT_SSH_COMMAND (see gitx.NetworkEnv)")

// SSHCommand builds the GIT_SSH_COMMAND every network call runs under (DESIGN:184).
//
// BatchMode=yes so a missing key fails instead of prompting a box nobody is sitting at;
// ConnectTimeout=2 so an unreachable hub costs two seconds, not a TCP timeout;
// StrictHostKeyChecking=yes with a pre-seeded known_hosts so a changed host key is
// refused rather than accepted on trust. Never prompt, never hang.
//
// It lives HERE, in the package that owns every git invocation, and not beside the sync
// verb that happened to need it first: a second call site that builds its own options -
// merge.Engine's Fetch and Push did exactly this - is how the rule was lost the first
// time, and a helper one import away from every caller is what makes the refusal below
// something a caller can satisfy rather than work around.
func SSHCommand(stateRoot string) string {
	kh := filepath.Join(stateRoot, KnownHostsFile)
	return "ssh -o BatchMode=yes -o ConnectTimeout=2 -o StrictHostKeyChecking=yes " +
		"-o UserKnownHostsFile=" + quoteForSSH(kh)
}

// NetworkEnv is the ExtraEnv every network git call carries. Callers append nothing to it
// and build nothing of their own: this one value is what the guard in Run looks for.
func NetworkEnv(stateRoot string) []string {
	return []string{"GIT_SSH_COMMAND=" + SSHCommand(stateRoot)}
}

func quoteForSSH(p string) string {
	if !strings.ContainsAny(p, " \t") {
		return p
	}
	return `"` + p + `"`
}

// networkVerbs are the git subcommands that open a socket. `remote` is here for its
// `update` subcommand only, which is why it is matched with its argument.
var networkVerbs = map[string]bool{
	"fetch":     true,
	"push":      true,
	"ls-remote": true,
	"clone":     true,
	"pull":      true,
}

// NetworkSubcommand names the network subcommand in an argv, or "" when the call is
// local.
//
// It reads PAST the global options, because every call in this module carries some:
// --git-dir=, --work-tree= and, on occasion, `-c key=value`. `-c` and `-C` take a separate
// value, so their argument is skipped rather than mistaken for the subcommand - `git -C
// fetch rev-parse HEAD` is a local call in a directory named "fetch".
func NetworkSubcommand(args []string) string {
	for i := 0; i < len(args); i++ {
		a := args[i]
		if strings.HasPrefix(a, "-") {
			if a == "-c" || a == "-C" {
				i++
			}
			continue
		}
		if networkVerbs[a] {
			return a
		}
		if a == "remote" {
			for _, rest := range args[i+1:] {
				if strings.HasPrefix(rest, "-") {
					continue
				}
				if rest == "update" {
					return "remote update"
				}
				return ""
			}
		}
		return ""
	}
	return ""
}

// hasSSHCommand reports whether an assembled environment carries a non-empty
// GIT_SSH_COMMAND. The LAST assignment is the one the child process sees, so the scan
// runs backwards: an ExtraEnv that blanks the variable is not hardening.
func hasSSHCommand(env []string) bool {
	const key = "GIT_SSH_COMMAND="
	for i := len(env) - 1; i >= 0; i-- {
		if strings.HasPrefix(env[i], key) {
			return strings.TrimSpace(strings.TrimPrefix(env[i], key)) != ""
		}
	}
	return false
}
