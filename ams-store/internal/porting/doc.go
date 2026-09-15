// Package porting holds the 1:1 Pester counterpart table while the engines it names
// are still being built.
//
// The design's Phase 3 gate is "every Pester scenario has a 1:1 Go counterpart"
// (blueprint section 10.3). That gate is only checkable if the whole table exists from
// day one, so every scenario in the four Pester suites has a Go test of the mapped name
// from the first commit: the ones this scaffold can already assert live in the package
// they belong to (index, store, frontmatter, atomic), and the rest live here as a
// skipped placeholder naming the task that will port them.
//
// # Rule for the tasks that follow
//
// When a task implements a scenario, it MOVES the test into the package that owns the
// behaviour and DELETES the placeholder here. Go allows the same test name in two
// packages, so a forgotten placeholder does not break the build - it silently keeps a
// scenario looking half-ported. The counterpart-table check (register row P3-6) reads
// the Pester files and asserts that every It has a Go test of the mapped name; a
// placeholder satisfies it, which is exactly why deleting it is part of the port and
// not an afterthought.
//
// The skip reason is the literal string "ported in task N" so the pending set can be
// listed with `go test ./internal/porting -run . -v` and counted per task.
package porting
