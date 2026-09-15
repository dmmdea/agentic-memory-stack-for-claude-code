package sync

import (
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

// MachineIDFile is the file under STATE_ROOT that holds this PC's stable id.
const MachineIDFile = "machine-id"

// MachineID reads this PC's id, creating it on first call.
//
// The id is the lower-cased hostname plus six hex characters, and both halves earn their
// place. A hostname alone is not stable - a PC can be renamed, and two PCs restored from
// the same image share one - and a random id alone is unreadable in a commit trailer,
// where a human has to tell at a glance which machine wrote a fact. It is written once
// and never regenerated: it is the deterministic tiebreak when two sides of a merge
// committed in the same second, so a value that changed would make the same merge resolve
// differently on two PCs.
func MachineID(stateRoot string) (string, error) {
	path := filepath.Join(stateRoot, MachineIDFile)
	b, err := os.ReadFile(path)
	if err == nil {
		if id := strings.TrimSpace(string(b)); id != "" {
			return id, nil
		}
	} else if !os.IsNotExist(err) {
		return "", fmt.Errorf("sync: read machine id: %w", err)
	}

	host, _ := os.Hostname()
	host = sanitizeHost(host)
	var suffix [3]byte
	if _, err := rand.Read(suffix[:]); err != nil {
		return "", fmt.Errorf("sync: machine id entropy: %w", err)
	}
	id := host + "-" + hex.EncodeToString(suffix[:])

	if err := os.MkdirAll(stateRoot, 0o755); err != nil {
		return "", fmt.Errorf("sync: create state root: %w", err)
	}
	// O_EXCL, so two processes racing on first run cannot end up with two ids: the loser
	// re-reads the winner's file rather than overwriting it.
	f, err := os.OpenFile(path, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o644)
	if err != nil {
		if os.IsExist(err) {
			b, rerr := os.ReadFile(path)
			if rerr == nil {
				if existing := strings.TrimSpace(string(b)); existing != "" {
					return existing, nil
				}
			}
		}
		return "", fmt.Errorf("sync: create machine id: %w", err)
	}
	defer f.Close()
	if _, err := f.WriteString(id + "\n"); err != nil {
		return "", fmt.Errorf("sync: write machine id: %w", err)
	}
	return id, nil
}

func sanitizeHost(h string) string {
	h = strings.ToLower(strings.TrimSpace(h))
	var b strings.Builder
	for _, r := range h {
		switch {
		case r >= 'a' && r <= 'z', r >= '0' && r <= '9', r == '-':
			b.WriteRune(r)
		case r == '.' || r == '_' || r == ' ':
			b.WriteRune('-')
		}
	}
	out := strings.Trim(b.String(), "-")
	if out == "" {
		out = "pc"
	}
	if len(out) > 40 {
		out = out[:40]
	}
	return out
}
