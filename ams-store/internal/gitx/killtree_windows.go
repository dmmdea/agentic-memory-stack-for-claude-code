//go:build windows

package gitx

import (
	"os/exec"
	"strconv"
)

// killTree ends a cancelled git and everything it spawned. Windows has no process
// groups to signal, so taskkill /T walks the tree; killing only the git process would
// leave a credential helper or a transport child holding the inherited stdout pipe open,
// which is precisely what makes Wait hang.
func killTree(cmd *exec.Cmd) error {
	if cmd.Process == nil {
		return nil
	}
	pid := strconv.Itoa(cmd.Process.Pid)
	if err := exec.Command("taskkill", "/T", "/F", "/PID", pid).Run(); err != nil {
		return cmd.Process.Kill()
	}
	return nil
}

// setProcessGroup is a no-op on Windows; the kill path uses taskkill /T instead.
func setProcessGroup(cmd *exec.Cmd) {}
