// Command mutationgate is the mechanical half of the mutation gate (blueprint section
// 10.2). It exists so the shell script that drives the gate never has to reproduce a Go
// string literal in sed: the table lives in internal/porting, and the substitutions are
// applied by the same program that reads it.
//
// It does three things and none of them is clever:
//
//	list           one TAB-separated row per mutation, for the driver's loop
//	files <id>     the distinct files that mutation touches, for the driver's restore
//	apply <id>     apply that mutation's hunks to the work tree
//
// `apply` refuses a hunk whose Old text does not appear EXACTLY ONCE in its file. A
// substitution that silently hit the wrong occurrence - or no occurrence - would let the
// driver report a rule GREEN that was never actually mutated, which is the one failure
// mode a mutation gate cannot have.
//
// Restoring is the driver's job, and it restores from git rather than from a copy this
// program kept, so a crash mid-run leaves a tree `git status` can explain.
//
// ASCII-only source (see the store package doc).
package main

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/porting"
)

func main() {
	if err := run(os.Args[1:]); err != nil {
		fmt.Fprintln(os.Stderr, "mutationgate: "+err.Error())
		os.Exit(1)
	}
}

const usage = "usage: mutationgate list | files <id> | apply <id>"

func run(args []string) error {
	if len(args) == 0 {
		return fmt.Errorf("%s", usage)
	}
	switch args[0] {
	case "list":
		return list()
	case "files":
		if len(args) != 2 {
			return fmt.Errorf("%s", usage)
		}
		return files(args[1])
	case "apply":
		if len(args) != 2 {
			return fmt.Errorf("%s", usage)
		}
		return apply(args[1])
	default:
		return fmt.Errorf("unknown command %q; %s", args[0], usage)
	}
}

// list prints the table the driver loops over. TAB-separated because a rule sentence
// contains every other plausible separator.
func list() error {
	for _, m := range porting.Mutations {
		state := "ready"
		if m.Pending() {
			state = "pending"
		}
		fmt.Printf("%s\t%d\t%s\t%s\t%s\t%s\t%s\n",
			m.ID, m.Task, state, m.Package, m.Test, m.File, m.Rule)
	}
	return nil
}

func find(id string) (porting.Mutation, error) {
	for _, m := range porting.Mutations {
		if m.ID == id {
			return m, nil
		}
	}
	return porting.Mutation{}, fmt.Errorf("no mutation with id %q", id)
}

// hunkFiles lists the distinct files a mutation touches, in the order its hunks name
// them. A rule defended in two places mutates both, so the driver has to restore both.
func hunkFiles(m porting.Mutation) []string {
	var out []string
	seen := map[string]bool{}
	add := func(f string) {
		if f != "" && !seen[f] {
			seen[f] = true
			out = append(out, f)
		}
	}
	if len(m.Hunks) == 0 {
		add(m.File)
	}
	for _, h := range m.Hunks {
		if h.File != "" {
			add(h.File)
			continue
		}
		add(m.File)
	}
	return out
}

// files prints what the driver must restore afterwards. It asks rather than assumes,
// because a mutation that touches two files and is restored in one leaves the other in
// the tree and poisons every result after it.
func files(id string) error {
	m, err := find(id)
	if err != nil {
		return err
	}
	for _, f := range hunkFiles(m) {
		fmt.Println(f)
	}
	return nil
}

func apply(id string) error {
	m, err := find(id)
	if err != nil {
		return err
	}
	if m.Pending() {
		return fmt.Errorf("mutation %q is pending: its target is not built yet", id)
	}
	// Every hunk is checked and staged in memory before a single byte is written, so a
	// mutation with one stale hunk never leaves half of itself on disk.
	edited := map[string]string{}
	read := func(f string) (string, error) {
		if text, ok := edited[f]; ok {
			return text, nil
		}
		raw, err := os.ReadFile(filepath.FromSlash(f))
		if err != nil {
			return "", fmt.Errorf("read %s: %w", f, err)
		}
		edited[f] = string(raw)
		return edited[f], nil
	}
	for i, h := range m.Hunks {
		f := h.File
		if f == "" {
			f = m.File
		}
		text, err := read(f)
		if err != nil {
			return err
		}
		n := strings.Count(text, h.Old)
		if n != 1 {
			return fmt.Errorf("hunk %d of %s: its Old text occurs %d times in %s, want exactly 1 - the table is stale",
				i+1, id, n, f)
		}
		edited[f] = strings.Replace(text, h.Old, h.New, 1)
	}
	for f, text := range edited {
		if err := os.WriteFile(filepath.FromSlash(f), []byte(text), 0o644); err != nil {
			return fmt.Errorf("write %s: %w", f, err)
		}
	}
	return nil
}
