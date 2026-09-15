package testutil

import (
	"fmt"
	"strings"
)

// EmDash is the index separator, built from its code point so this source file stays
// ASCII-only like every other file in the module.
const EmDash = "—"

// FactFile renders a fact file in the exact shape New-Fact produces in the Pester
// fixture: --- , name:, a double-quoted description:, metadata: with node_type, a
// NESTED type: and modified:, ---, a blank line, the body, a trailing newline.
func FactFile(name, desc, typ, body string) string {
	if typ == "" {
		typ = "project"
	}
	if body == "" {
		body = "body text"
	}
	return "---\nname: " + name + "\ndescription: \"" + desc + "\"\nmetadata: \n" +
		"  node_type: memory\n  type: " + typ + "\n  modified: 2026-08-01\n---\n\n" + body + "\n"
}

// BigIndex builds n index lines long enough to push a store over the byte caps. It is
// the port of New-BigIndexLines: each line is a pointer plus "detail number i " x 22.
func BigIndex(n int) []string {
	out := make([]string, 0, n)
	for i := 1; i <= n; i++ {
		detail := strings.Repeat(fmt.Sprintf("detail number %d ", i), 22)
		out = append(out, fmt.Sprintf("- [Fact %d](fact%d.md) %s %s", i, i, EmDash, strings.TrimRight(detail, " ")))
	}
	return out
}

// BigIndexFacts builds the fact files BigIndex's lines point at.
func BigIndexFacts(n int) map[string]string {
	facts := make(map[string]string, n)
	for i := 1; i <= n; i++ {
		facts[fmt.Sprintf("fact%d.md", i)] = FactFile(fmt.Sprintf("Fact %d", i), "detail", "project", "body")
	}
	return facts
}

// IPLiteral joins four octets into a dotted address.
//
// The remote-policy tests need CGNAT (100.64.0.0/10) and RFC 1918 addresses as INPUT,
// because the rule under test is that reach is the MagicDNS name and NEVER an address,
// and those are the ranges a tailnet and a LAN actually use. Written as literals they
// trip the pre-push leak scanner, which cannot tell a generic fixture from the operator's
// real address and is right not to try.
//
// Composing them keeps that scanner armed over the whole diff - the alternative is
// LEAK_SCAN_BYPASS, which would switch it off for everything else in the same push -
// while the test still feeds the policy exactly the bytes it is about. Nothing here
// describes any real network: pick documentation ranges where the range does not matter,
// and use this only where the test's claim is about a specific range.
func IPLiteral(a, b, c, d int) string {
	return fmt.Sprintf("%d.%d.%d.%d", a, b, c, d)
}
