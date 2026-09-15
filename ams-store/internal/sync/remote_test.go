package sync

import (
	"context"
	"os"
	"strings"
	"testing"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/testutil"
)

// hubHost is the placeholder MagicDNS name the tests use. The real one is configuration,
// never source: a machine name compiled into this repository is a machine name published
// with it.
const hubHost = "hub-host"

// TestRemote_PolicyAcceptsOnlySSHToAMagicDNSHost is the Y3 amendment (DESIGN:170,
// blueprint section 7.2). Y3 turned "any remote is a finding" into "exactly one remote,
// named hub, SSH, MagicDNS host" - and everything else stays a finding.
func TestRemote_PolicyAcceptsOnlySSHToAMagicDNSHost(t *testing.T) {
	p := RemotePolicy{}
	accepted := []string{
		"ams-hub@" + hubHost + ":/srv/ams/ams-store.git",
		"ssh://ams-hub@" + hubHost + "/srv/ams/ams-store.git",
		"ssh://ams-hub@" + hubHost + ":2222/srv/ams/ams-store.git",
	}
	for _, url := range accepted {
		if reason := p.CheckURL(url); reason != "" {
			t.Fatalf("CheckURL(%q) refused an acceptable hub: %s", url, reason)
		}
	}

	rejected := map[string]string{
		"https://example.invalid/ams-store.git":                                    "must be SSH",
		"git://" + hubHost + "/ams-store.git":                                      "must be SSH",
		"ams-hub@192.0.2.10:/srv/ams/ams-store.git":                                "IP literal",
		"ams-hub@" + testutil.IPLiteral(100, 64, 0, 1) + ":/srv/ams/ams-store.git": "IP literal",
		"ssh://ams-hub@[2001:db8::1]/ams-store.git":                                "MagicDNS",
		"ams-hub@" + hubHost + ".example.invalid:/a":                               "dotted name",
		"/srv/ams/ams-store.git":                                                   "local path",
		"":                                                                         "no URL",
	}
	for url, want := range rejected {
		reason := p.CheckURL(url)
		if reason == "" {
			t.Fatalf("CheckURL(%q) accepted a URL the policy must refuse", url)
		}
		if !strings.Contains(reason, want) {
			t.Fatalf("CheckURL(%q) = %q, want a reason mentioning %q", url, reason, want)
		}
	}
}

// TestRemote_CGNATAndLANLiteralsAreRefusedEvenWhenTheyWork: reach is MagicDNS, never an
// address. An address that resolves today is a stale address after the next lease, and
// the failure mode is a silent push to whoever holds it.
func TestRemote_CGNATAndLANLiteralsAreRefusedEvenWhenTheyWork(t *testing.T) {
	p := RemotePolicy{}
	for _, url := range []string{
		"ams-hub@" + testutil.IPLiteral(10, 0, 0, 5) + ":/srv/ams/ams-store.git",
		"ams-hub@" + testutil.IPLiteral(192, 168, 1, 50) + ":/srv/ams/ams-store.git",
		"ssh://ams-hub@" + testutil.IPLiteral(100, 100, 100, 100) + "/srv/ams/ams-store.git",
	} {
		if reason := p.CheckURL(url); !strings.Contains(reason, "IP literal") {
			t.Fatalf("CheckURL(%q) = %q, want an IP-literal refusal", url, reason)
		}
	}
}

// TestRemote_ExpectedHostPinsTheHub: when the configured hub name is known, a
// well-shaped URL to a DIFFERENT tailnet machine is still refused.
func TestRemote_ExpectedHostPinsTheHub(t *testing.T) {
	p := RemotePolicy{ExpectedHost: hubHost}
	if reason := p.CheckURL("ams-hub@" + hubHost + ":/srv/ams/ams-store.git"); reason != "" {
		t.Fatalf("the configured hub was refused: %s", reason)
	}
	if reason := p.CheckURL("ams-hub@some-other-box:/srv/ams/ams-store.git"); reason == "" {
		t.Fatal("a different tailnet machine was accepted as the hub")
	}
}

// TestRemote_LocalPathIsOnlyForTheHubsOwnCheckout: the Lenovo judges its own checkout of
// the bare repository sitting beside it. A PC must never do that silently.
func TestRemote_LocalPathIsOnlyForTheHubsOwnCheckout(t *testing.T) {
	pc := RemotePolicy{}
	hub := RemotePolicy{AllowLocalPath: true}
	for _, url := range []string{"/srv/ams/ams-store.git", `C:\ams\hub.git`, "C:/ams/hub.git"} {
		if reason := pc.CheckURL(url); reason == "" {
			t.Fatalf("a PC accepted the local path %q", url)
		}
		if reason := hub.CheckURL(url); reason != "" {
			t.Fatalf("the hub's own checkout refused %q: %s", url, reason)
		}
	}
}

// TestRemote_ASecondRemoteIsAlwaysAFinding: exactly one remote, and it is named hub.
func TestRemote_ASecondRemoteIsAlwaysAFinding(t *testing.T) {
	testutil.RequireGit(t)
	_, repo, _ := historyFixture(t)
	ctx := context.Background()

	mustGit(t, "", "--git-dir="+repo.GitDir, "remote", "add", HubRemote, "ams-hub@"+hubHost+":/srv/ams/ams-store.git")
	mustGit(t, "", "--git-dir="+repo.GitDir, "remote", "add", "backup", "ams-hub@"+hubHost+":/srv/ams/backup.git")

	checks, err := (RemotePolicy{}).Check(ctx, repo)
	if err != nil {
		t.Fatalf("Check: %v", err)
	}
	if len(checks) != 2 {
		t.Fatalf("Check returned %d rows, want 2", len(checks))
	}
	for _, c := range checks {
		switch c.Name {
		case HubRemote:
			if !c.OK {
				t.Fatalf("the hub remote was refused: %s", c.Reason)
			}
		default:
			if c.OK {
				t.Fatalf("the second remote %q was accepted", c.Name)
			}
		}
	}
	if err := (RemotePolicy{}).Validate(ctx, repo); err == nil {
		t.Fatal("Validate accepted a repository with a second remote")
	}
}

// TestMachineID_IsStableAcrossCalls: the id is the merge's deterministic tiebreak, so a
// value that changed between calls would make the same merge resolve differently twice.
func TestMachineID_IsStableAcrossCalls(t *testing.T) {
	sb := testutil.NewSandbox(t)
	first, err := MachineID(sb.StateRoot)
	if err != nil {
		t.Fatalf("MachineID: %v", err)
	}
	if first == "" || !strings.Contains(first, "-") {
		t.Fatalf("machine id %q is not <host>-<hex>", first)
	}
	second, err := MachineID(sb.StateRoot)
	if err != nil {
		t.Fatalf("second MachineID: %v", err)
	}
	if first != second {
		t.Fatalf("the machine id changed between calls: %q then %q", first, second)
	}
	b, err := os.ReadFile(sb.StateRoot + string(os.PathSeparator) + MachineIDFile)
	if err != nil {
		t.Fatalf("the machine id was not persisted: %v", err)
	}
	if strings.TrimSpace(string(b)) != first {
		t.Fatalf("the file holds %q, the call returned %q", strings.TrimSpace(string(b)), first)
	}
}

// TestMachineID_TwoPCsDiffer: two state roots on one hostname must still get distinct
// ids. A hostname alone is not identity - two PCs restored from one image share it.
func TestMachineID_TwoPCsDiffer(t *testing.T) {
	a, err := MachineID(testutil.NewSandbox(t).StateRoot)
	if err != nil {
		t.Fatal(err)
	}
	b, err := MachineID(testutil.NewSandbox(t).StateRoot)
	if err != nil {
		t.Fatal(err)
	}
	if a == b {
		t.Fatalf("two machines got the same id %q", a)
	}
}
