package store

import (
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

// Store is one populated auto-memory store, or an alias directory pointing at one.
type Store struct {
	Workspace    string
	Dir          string // the memory directory
	IndexPath    string
	CanonicalDir string // canonical key; empty when the link could not be resolved
	IsAlias      bool
	AliasOf      string
	WorkspaceDir string
	// ProbeDirs is every workspace directory that reaches this store. A session running
	// under an ALIAS path writes its transcripts into the alias directory, so a liveness
	// probe on the canonical name alone would miss it entirely - and the liveness gate
	// exists because a live session was observed writing mid-run.
	ProbeDirs []string
}

// FactFile is one *.md in a store that is not the index.
type FactFile struct {
	Name string
	Path string
	Size int64
}

// CanonicalKey folds a resolved workspace target into the key stores are deduped on.
//
// On Windows the key is lower-cased, because the filesystem folds too and two spellings
// of one path are one store. Off Windows it is case-PRESERVING: folding there would
// collapse two genuinely distinct workspaces that differ only in case into one and mark
// one of them IsAlias, silently. Enumerate warns about that shape instead (decision Q5).
func CanonicalKey(target string) string {
	k := filepath.Clean(target)
	if foldCase() {
		k = strings.ToLower(k)
	}
	return k
}

// ResolveReparseTarget returns the canonical directory a workspace directory denotes: the
// directory itself when it is real, the link's target when it is a junction or symlink,
// and "" when it IS a link whose target cannot be read.
//
// Resolution fails CLOSED. Returning the input directory on failure would crown an
// unresolvable junction its own canonical store and compact the same physical store
// TWICE in one run, the second pass reading the first pass's output - defeating both the
// seal and the blast cap.
//
// The target is read with os.Readlink, NOT filepath.EvalSymlinks. Measured on Go 1.26.7
// against a real `mklink /J` junction: EvalSymlinks returns the link path UNCHANGED and
// reports no error, because its walk skips any component whose Lstat mode lacks
// fs.ModeSymlink (path/filepath/symlink.go) and a Windows junction Lstats as
// ModeIrregular with FILE_ATTRIBUTE_REPARSE_POINT set. Using EvalSymlinks here would
// make every junction alias resolve to itself - exactly the fail-open the dedup exists
// to prevent. os.Readlink does follow a mount point, and returns the same literal target
// the PowerShell original reads from $item.Target (LIB:120-122), so the two
// implementations agree.
//
// One hop, like the original: a link to a link is not chased.
func ResolveReparseTarget(dir string) string {
	fi, err := os.Lstat(dir)
	if err != nil {
		return ""
	}
	if !isReparsePoint(fi) {
		return strings.TrimRight(filepath.Clean(dir), `\/`)
	}
	target, err := os.Readlink(dir)
	if err != nil || target == "" {
		return "" // it IS a link and we could not read where it points
	}
	if !filepath.IsAbs(target) {
		target = filepath.Join(filepath.Dir(dir), target)
	}
	return strings.TrimRight(filepath.Clean(target), `\/`)
}

// Enumerate returns every populated store under projectsRoot, deduplicated by canonical
// path so an alias is never processed twice (mutating through an alias = mutating the
// real store), plus any warnings the caller should surface at startup.
//
// A workspace without <ws>/memory/MEMORY.md is an empty scaffold and is skipped outright.
func Enumerate(projectsRoot string) ([]Store, []string, error) {
	var warnings []string
	entries, err := os.ReadDir(projectsRoot)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil, nil
		}
		return nil, nil, fmt.Errorf("read projects root %s: %w", projectsRoot, err)
	}

	type dirRow struct {
		name    string
		path    string
		reparse bool
	}
	var dirs []dirRow
	for _, e := range entries {
		fi, err := os.Lstat(filepath.Join(projectsRoot, e.Name()))
		if err != nil {
			continue
		}
		if !fi.IsDir() && !isReparsePoint(fi) {
			continue
		}
		dirs = append(dirs, dirRow{name: e.Name(), path: filepath.Join(projectsRoot, e.Name()), reparse: isReparsePoint(fi)})
	}
	// Real directories first, then reparse points, then name: the PHYSICAL store is
	// always crowned canonical and an alias can never win by sort order. Sorting by name
	// alone once crowned an alias, because spaces sort before dashes, and the job would
	// have mutated the store through the link.
	sort.SliceStable(dirs, func(i, j int) bool {
		if dirs[i].reparse != dirs[j].reparse {
			return !dirs[i].reparse
		}
		li, lj := strings.ToLower(dirs[i].name), strings.ToLower(dirs[j].name)
		if li != lj {
			return li < lj
		}
		return dirs[i].name < dirs[j].name
	})

	seen := make(map[string]string)    // canonical key -> winning workspace name
	exact := make(map[string][]string) // case-folded key -> the distinct exact keys seen
	rows := make([]Store, 0, len(dirs))
	for _, d := range dirs {
		memDir := filepath.Join(d.path, "memory")
		idx := filepath.Join(memDir, IndexName)
		if _, err := os.Stat(idx); err != nil {
			continue // an empty scaffold is not a store
		}
		target := ResolveReparseTarget(d.path)
		if target == "" {
			// Unresolvable link: an alias of nothing, i.e. never mutate through it.
			rows = append(rows, Store{
				Workspace: d.name, Dir: memDir, IndexPath: idx,
				CanonicalDir: "", IsAlias: true, AliasOf: "(unresolved link)",
				WorkspaceDir: d.path,
			})
			continue
		}
		canon := CanonicalKey(filepath.Join(target, "memory"))
		isAlias := false
		aliasOf := ""
		if w, ok := seen[canon]; ok {
			isAlias = true
			aliasOf = w
		} else {
			seen[canon] = d.name
		}
		if !foldCase() {
			lower := strings.ToLower(canon)
			if !contains(exact[lower], canon) {
				exact[lower] = append(exact[lower], canon)
			}
		}
		rows = append(rows, Store{
			Workspace: d.name, Dir: memDir, IndexPath: idx,
			CanonicalDir: canon, IsAlias: isAlias, AliasOf: aliasOf,
			WorkspaceDir: d.path,
		})
	}

	// Give every canonical store the list of workspace directories that reach it.
	for i := range rows {
		probe := []string{rows[i].WorkspaceDir}
		if !rows[i].IsAlias && rows[i].CanonicalDir != "" {
			for j := range rows {
				if rows[j].IsAlias && rows[j].CanonicalDir == rows[i].CanonicalDir {
					probe = append(probe, rows[j].WorkspaceDir)
				}
			}
		}
		rows[i].ProbeDirs = probe
	}

	for lower, keys := range exact {
		if len(keys) > 1 {
			sort.Strings(keys)
			warnings = append(warnings, fmt.Sprintf(
				"two stores differ only in case and are treated as distinct on this filesystem: %s (folded: %s)",
				strings.Join(keys, ", "), lower))
		}
	}
	sort.Strings(warnings)
	return rows, warnings, nil
}

// FactFiles lists the fact files of a store: every *.md except the index, sorted by name.
// It never recurses - subdirectories are not part of the harness contract and a
// maintainer must never create one.
//
// It THROWS on an enumeration failure, deliberately. When an unreadable directory and an
// empty one both answered "nothing here", a caller comparing the index against that empty
// set concluded every line was dangling - then every downstream guard agreed, because
// they all compared against the same empty set, and the whole index was wiped with a
// receipt reporting success. "Could not read" must never be spellable as "nothing there".
func FactFiles(dir string) ([]FactFile, error) {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil, fmt.Errorf("enumerate fact files in %s: %w", dir, err)
	}
	out := make([]FactFile, 0, len(entries))
	for _, e := range entries {
		if e.IsDir() {
			continue
		}
		name := e.Name()
		if name == IndexName || !strings.HasSuffix(name, ".md") {
			continue
		}
		info, err := e.Info()
		if err != nil {
			return nil, fmt.Errorf("stat %s: %w", filepath.Join(dir, name), err)
		}
		out = append(out, FactFile{Name: name, Path: filepath.Join(dir, name), Size: info.Size()})
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	return out, nil
}

func contains(list []string, s string) bool {
	for _, v := range list {
		if v == s {
			return true
		}
	}
	return false
}
