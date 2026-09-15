// Package porting holds the checks that make the PowerShell-to-Go port verifiable
// rather than merely claimed, plus the two tables they read.
//
// Nothing here tests ams-store's behaviour. Every check in this package tests the SUITE:
// each one covers a way a green test run can be telling you nothing.
//
// # The counterpart table (counterparts.go)
//
// The design's Phase 3 gate is "every Pester scenario has a 1:1 Go counterpart"
// (blueprint section 10.3, DESIGN:241). The table maps each of the 95 `It` blocks in the
// four Pester suites, by file and line, to the exact Go test that carries it, and
// TestPorting_EveryPesterScenarioHasANamedGoCounterpart reads the Pester files and
// enforces it against `go test -list`. Exactly one scenario is exempt and carries its
// reason in the table; a second exemption fails the gate.
//
// During the parallel build the not-yet-ported scenarios lived here as tests that skipped
// with the literal reason "ported in task N", so the table was enumerable from day one.
// They are all gone, and TestPorting_NoPlaceholderSkipsRemain is what keeps them gone: a
// skipped test is still a test to `go test -list`, so the counterpart gate on its own
// would pass over a suite that asserts nothing.
//
// # The mutation table (mutations.go)
//
// A passing suite proves the tests RUN. It does not prove they would notice if a rule were
// lost. For each rule of blueprint section 10.2 the table carries one minimal change that
// breaks it and the name of the test that must then go red;
// ams-store/scripts/mutation-gate.sh applies them one at a time, builds, and requires the
// failure. It is a local gate, never a CI job - it is N+1 times the suite.
//
// # The two repo-wide gates
//
// TestPorting_EveryPackageThatResolvesTheHomeSandboxesIt requires a package whose source
// calls store.DefaultRoots to run its tests with HOME and USERPROFILE moved. Two live
// incidents hours apart on 2026-09-15 came from a test that did not inject its roots: one
// harvested a hook: line into 248 live fact files, the other committed five live stores
// into the operator's real history repo. In both the code was correct and the test was what
// reached production.
//
// TestPorting_SourceStaysASCII keeps non-ASCII out of Go source (blueprint 1.3), inherited
// from the PowerShell library this module replaces: a BOM-less UTF-8 file read as ANSI by
// PowerShell 5.1 tokenises an em-dash as a smart quote, so the character is built from its
// code point and never typed.
package porting
