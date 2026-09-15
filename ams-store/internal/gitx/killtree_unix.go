//go:build !windows

package gitx

import (
	"os/exec"
	"syscall"
)

// killTree ends a cancelled git and everything it spawned by signalling the whole
// process group. Killing only the git process would leave a credential helper or a
// transport child holding the inherited stdout pipe open, which is precisely what makes
// Wait hang.
func killTree(cmd *exec.Cmd) error {
	if cmd.Process == nil {
		return nil
	}
	if err := syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL); err != nil {
		return cmd.Process.Kill()
	}
	return nil
}

// setProcessGroup puts the child in its own process group so killTree can signal the
// whole group rather than just the git process.
func setProcessGroup(cmd *exec.Cmd) {
	if cmd.SysProcAttr == nil {
		cmd.SysProcAttr = &syscall.SysProcAttr{}
	}
	cmd.SysProcAttr.Setpgid = true
}
