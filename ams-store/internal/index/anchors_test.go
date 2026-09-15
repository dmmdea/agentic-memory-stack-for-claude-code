package index

import "testing"

// The token classes are the port of Get-AmAnchorTokens (LIB:391-402). They are what told
// the reader WHEN to open a file, so a rewrite that keeps none of them has kept the topic
// and lost the trigger.
//
// The reserved counterpart names for the lib-level Pester scenarios belong to the derive
// port; this test covers the shared implementation those scenarios will read.
func TestIndex_AnchorTokenClasses(t *testing.T) {
	got := AnchorTokens("port 18791, C:\\Users\\x\\.mem0, `Test-Throttle`, DPAPI phase 3, /etc/hosts, https://example.test/x")
	for _, want := range []string{"18791", `C:\Users\x\.mem0`, "Test-Throttle", "DPAPI", "3", "/etc/hosts", "https://example.test/x"} {
		if !got[want] {
			t.Errorf("anchor %q was not recognised; got %v", want, got)
		}
	}
	if len(AnchorTokens("")) != 0 {
		t.Error("empty text has no anchors")
	}
	if len(AnchorTokens("a plain sentence of ordinary words")) != 0 {
		t.Error("ordinary prose must carry no anchors, or every rewrite is unconstrained")
	}
}

// An anchor such as `cfg[0].name` is a character CLASS to a wildcard matcher, which both
// rejects hooks that DO keep it and accepts hooks that dropped it. The false accept is
// what defeats the guard entirely, so containment is plain substring matching.
func TestIndex_AnchorKeepRuleHasNoWildcardFalseAccept(t *testing.T) {
	old := "the `cfg[0].name` knob matters a great deal here"

	if !KeepsAnAnchor(old, "the `cfg[0].name` knob matters") {
		t.Error("a hook that KEEPS the anchor was rejected")
	}
	if KeepsAnAnchor(old, "the cfg0.name knob matters") {
		t.Error("a hook that DROPPED the anchor was accepted: the wildcard false accept is back")
	}
	if !KeepsAnAnchor("a hook with no anchors at all", "any rewrite") {
		t.Error("a hook with nothing to lose must be unconstrained")
	}
	if KeepsAnAnchor("listen on port 18791", "listen on the usual port") {
		t.Error("a rewrite that dropped the port number was accepted")
	}
}
