// Package gitx is the one place ams-store invokes git. Every argv, every environment
// tweak, every timeout and the >= 2.38 floor live here so a second call site cannot
// drift from the first.
package gitx

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"strings"
	"time"
)

// DefaultTimeout bounds a git call that does not name its own.
const DefaultTimeout = 60 * time.Second

// KillDelay is how long Wait is allowed to linger after a cancelled command's process has
// been killed before its I/O is abandoned.
//
// exec.CommandContext sets Cancel to kill the process but leaves WaitDelay UNSET, and an
// unset WaitDelay means a killed process whose inherited pipes are still held open by a
// grandchild hangs Wait indefinitely. A hung `git fetch` on a dead tailnet link is
// exactly the hazard the design's "the network is never on a hook's critical path" rule
// exists for, so both fields are set here for every call.
const KillDelay = 10 * time.Second

// Result is one git invocation's outcome.
type Result struct {
	Code   int
	Stdout string
	Stderr string
	Args   []string
}

// Options configures one git invocation.
type Options struct {
	// GitDir and WorkTree, when set, are prepended as --git-dir / --work-tree so the
	// history repo's metadata never lands inside a synced store.
	GitDir   string
	WorkTree string
	// Dir is the process working directory.
	Dir string
	// Env replaces the inherited environment when non-nil.
	Env []string
	// ExtraEnv is appended to the effective environment.
	ExtraEnv []string
	// Timeout bounds the call. Zero means DefaultTimeout.
	Timeout time.Duration
	// OkExit classifies an exit code as success. Nil means "0 only".
	OkExit func(int) bool
	// Stdin, when non-empty, is fed to the command.
	Stdin string
	// StdinRaw, when non-nil, is fed to the command verbatim and takes precedence over
	// Stdin. It exists because an EMPTY stdin is a real input that Stdin's "" cannot
	// express: `hash-object -w --stdin` on a zero-byte fact file must write the empty
	// blob, and with no stdin attached at all it would inherit the parent's and hang.
	StdinRaw []byte
}

// ExitError is returned when git ran and exited with a code OkExit rejects.
type ExitError struct {
	Result Result
}

func (e *ExitError) Error() string {
	msg := strings.TrimSpace(e.Result.Stderr)
	if msg == "" {
		msg = strings.TrimSpace(e.Result.Stdout)
	}
	return fmt.Sprintf("git %s: exit %d: %s", strings.Join(e.Result.Args, " "), e.Result.Code, msg)
}

// OkExitCodes builds an OkExit classifier from a list of acceptable codes.
//
// Git's plumbing uses a non-zero exit as signal routinely - `merge-tree --write-tree`
// exits non-zero on conflict, `rev-parse --verify --quiet` exits 1 on a missing ref - so
// callers classify by exit code rather than treating any error as a failure.
func OkExitCodes(codes ...int) func(int) bool {
	set := make(map[int]bool, len(codes))
	for _, c := range codes {
		set[c] = true
	}
	return func(c int) bool { return set[c] }
}

// Run invokes git with args.
//
// The argv is a slice, never a joined command line: a store whose workspace name
// contains a space is ordinary, and the PowerShell original had to hand-quote around
// CommandLineToArgv to survive one.
func Run(ctx context.Context, opt Options, args ...string) (Result, error) {
	full := make([]string, 0, len(args)+2)
	if opt.GitDir != "" {
		full = append(full, "--git-dir="+opt.GitDir)
	}
	if opt.WorkTree != "" {
		full = append(full, "--work-tree="+opt.WorkTree)
	}
	full = append(full, args...)

	timeout := opt.Timeout
	if timeout <= 0 {
		timeout = DefaultTimeout
	}
	cctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	cmd := exec.CommandContext(cctx, "git", full...)
	cmd.Cancel = func() error { return killTree(cmd) }
	cmd.WaitDelay = KillDelay
	setProcessGroup(cmd)
	cmd.Dir = opt.Dir

	env := opt.Env
	if env == nil {
		env = os.Environ()
	}
	env = append(append([]string(nil), env...), "GIT_TERMINAL_PROMPT=0")
	env = append(env, opt.ExtraEnv...)
	cmd.Env = env

	// The network is hardened HERE or not at all. Every caller that opens a socket has to
	// come through this function, so the check that the hardened environment is present
	// belongs on this side of the call rather than in each caller's own options: the one
	// place that forgot it (merge.Engine's Fetch and Push) looked exactly like the places
	// that remembered, and nothing could tell them apart until now. Refused BEFORE the
	// process starts, so a call with no GIT_SSH_COMMAND never reaches a host key.
	if sub := NetworkSubcommand(full); sub != "" && !hasSSHCommand(env) {
		return Result{Args: full}, fmt.Errorf("git %s: refused: %w", sub, ErrUnhardenedNetwork)
	}

	switch {
	case opt.StdinRaw != nil:
		cmd.Stdin = bytes.NewReader(opt.StdinRaw)
	case opt.Stdin != "":
		cmd.Stdin = strings.NewReader(opt.Stdin)
	}
	var out, errb bytes.Buffer
	cmd.Stdout = &out
	cmd.Stderr = &errb

	runErr := cmd.Run()
	res := Result{Stdout: out.String(), Stderr: errb.String(), Args: full}
	if cmd.ProcessState != nil {
		res.Code = cmd.ProcessState.ExitCode()
	}

	ok := opt.OkExit
	if ok == nil {
		ok = func(c int) bool { return c == 0 }
	}

	if runErr != nil {
		var ee *exec.ExitError
		if errors.As(runErr, &ee) {
			if ok(res.Code) {
				return res, nil
			}
			return res, &ExitError{Result: res}
		}
		// Not an exit status: git is missing (LookPath fails at construction and
		// surfaces here), the context was cancelled, or the pipe broke.
		return res, fmt.Errorf("git %s: %w", strings.Join(full, " "), runErr)
	}
	if !ok(res.Code) {
		return res, &ExitError{Result: res}
	}
	return res, nil
}
