package gitx

// Plumbing wrappers. Every one of these is a porcelain-free command whose output git
// promises to keep stable, because the merge engine reads them as data rather than as
// text for a human. The one exception is `merge-tree`, whose conflict framing is NOT
// stable across 2.38 -> 2.45; it is parsed defensively here and nowhere else.

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
)

// NullOID is git's all-zero object id, the "this ref does not exist yet" old value for
// update-ref.
const NullOID = "0000000000000000000000000000000000000000"

// reOID matches a SHA-1 (40) or SHA-256 (64) object id and nothing else. It is the
// shape check every parse below runs before believing a line is an object id.
var reOID = regexp.MustCompile(`^[0-9a-f]{40}(?:[0-9a-f]{24})?$`)

// IsOID reports whether s is a full object id.
func IsOID(s string) bool { return reOID.MatchString(s) }

// RevParse resolves a revision to an object id. found=false means the revision does not
// exist, which is an ordinary answer (an empty repo has no HEAD) and not an error.
func RevParse(ctx context.Context, opt Options, rev string) (oid string, found bool, err error) {
	opt.OkExit = OkExitCodes(0, 1, 128)
	res, err := Run(ctx, opt, "rev-parse", "--verify", "--quiet", rev+"^{commit}")
	if err != nil {
		return "", false, err
	}
	out := strings.TrimSpace(res.Stdout)
	if res.Code != 0 || out == "" {
		return "", false, nil
	}
	return out, true, nil
}

// MergeBase returns the best common ancestor of two commits. found=false means the two
// histories are unrelated - the orphan-histories path, where the caller treats the base
// as the EMPTY TREE rather than aborting. Two PCs that both `git init` before either
// pushes produce exactly that, and it is a merge, not a failure.
func MergeBase(ctx context.Context, opt Options, a, b string) (oid string, found bool, err error) {
	opt.OkExit = OkExitCodes(0, 1)
	res, err := Run(ctx, opt, "merge-base", a, b)
	if err != nil {
		return "", false, err
	}
	out := strings.TrimSpace(res.Stdout)
	if res.Code != 0 || !IsOID(out) {
		return "", false, nil
	}
	return out, true, nil
}

// EmptyTree writes (idempotently) and returns the id of the empty tree object, the stand-in
// base for an orphan-histories merge.
func EmptyTree(ctx context.Context, opt Options) (string, error) {
	res, err := Run(ctx, opt, "hash-object", "-w", "-t", "tree", "--stdin")
	if err != nil {
		return "", err
	}
	out := strings.TrimSpace(res.Stdout)
	if !IsOID(out) {
		return "", fmt.Errorf("hash-object -t tree returned %q, which is not an object id", out)
	}
	return out, nil
}

// AddFactFiles stages a store's fact files: additions, modifications and deletions.
//
// The pathspec is forced past info/exclude and narrowed to *.md, with the derived index
// excluded. It lives here rather than in two packages because the merge engine and sync
// both stage the same thing, and a narrowing applied in one place only is a narrowing
// that whichever code path ran last undoes.
//
// An unmatched pathspec is NOT an error here. git add exits 128 with "did not match any
// files" when a store holds no fact file at all - a workspace whose facts were all
// migrated, or one created empty - and that is a store with nothing to stage, not a
// failure. The message is checked rather than the code alone, so a genuine bad pathspec
// still surfaces.
func AddFactFiles(ctx context.Context, opt Options, storeRel, indexName string) error {
	o := opt
	o.OkExit = OkExitCodes(0, 128)
	res, err := Run(ctx, o, "add", "-A", "-f", "--",
		":(glob)"+storeRel+"/*.md", ":(exclude)"+storeRel+"/"+indexName)
	if err != nil {
		return err
	}
	if res.Code == 0 || strings.Contains(res.Stderr, "did not match any files") {
		return nil
	}
	return fmt.Errorf("git add %s: exit %d: %s", storeRel, res.Code, strings.TrimSpace(res.Stderr))
}

// HashObject writes data as a blob and returns its id. data may be empty.
func HashObject(ctx context.Context, opt Options, data []byte) (string, error) {
	if data == nil {
		data = []byte{}
	}
	opt.StdinRaw = data
	res, err := Run(ctx, opt, "hash-object", "-w", "--stdin")
	if err != nil {
		return "", err
	}
	out := strings.TrimSpace(res.Stdout)
	if !IsOID(out) {
		return "", fmt.Errorf("hash-object returned %q, which is not an object id", out)
	}
	return out, nil
}

// CatBlob reads a blob's bytes.
func CatBlob(ctx context.Context, opt Options, oid string) ([]byte, error) {
	res, err := Run(ctx, opt, "cat-file", "blob", oid)
	if err != nil {
		return nil, err
	}
	return []byte(res.Stdout), nil
}

// TreeEntry is one row of `ls-tree -r`.
type TreeEntry struct {
	Mode string
	Type string
	OID  string
	Path string
}

// reLsTree is `<mode> SP <type> SP <oid> TAB <path>`.
var reLsTree = regexp.MustCompile(`^(\d{6}) ([a-z]+) ([0-9a-f]{40,64})\t(.*)$`)

// LsTree lists a tree recursively, keyed by path. Paths are slash-separated as git
// stores them; core.quotepath=false and -z keep a non-ASCII or spacey path intact.
func LsTree(ctx context.Context, opt Options, tree string) (map[string]TreeEntry, error) {
	if tree == "" {
		return map[string]TreeEntry{}, nil
	}
	res, err := Run(ctx, opt, "ls-tree", "-r", "-z", "--full-tree", tree)
	if err != nil {
		return nil, err
	}
	out := make(map[string]TreeEntry)
	for _, rec := range strings.Split(res.Stdout, "\x00") {
		if rec == "" {
			continue
		}
		m := reLsTree.FindStringSubmatch(rec)
		if m == nil {
			return nil, fmt.Errorf("ls-tree produced an unrecognised row %q", rec)
		}
		out[m[4]] = TreeEntry{Mode: m[1], Type: m[2], OID: m[3], Path: m[4]}
	}
	return out, nil
}

// IndexChange is one staging instruction for StageInto. An empty OID removes the path.
type IndexChange struct {
	Path string
	Mode string // "" means 100644
	OID  string // "" means remove
}

// StageInto reads tree into a SCRATCH index, applies changes, and writes the result out
// as a new tree.
//
// The scratch index is the whole point: the repo's real index belongs to the work tree,
// and the design's rule is that the merge NEVER touches the work tree. Staging through
// GIT_INDEX_FILE keeps a half-finished merge invisible to anything else looking at the
// repo, and leaves nothing to clean up if the process dies mid-merge.
func StageInto(ctx context.Context, opt Options, tree string, changes []IndexChange) (string, error) {
	tmp, err := os.CreateTemp("", "ams-index-*")
	if err != nil {
		return "", fmt.Errorf("scratch index: %w", err)
	}
	idx := tmp.Name()
	_ = tmp.Close()
	// git refuses to read-tree into a non-empty file it did not write, so hand it a path
	// that does not exist yet.
	_ = os.Remove(idx)
	defer func() { _ = os.Remove(idx) }()

	sub := opt
	sub.ExtraEnv = append(append([]string(nil), opt.ExtraEnv...), "GIT_INDEX_FILE="+idx)

	if tree != "" {
		if _, err := Run(ctx, sub, "read-tree", tree); err != nil {
			return "", err
		}
	} else {
		if _, err := Run(ctx, sub, "read-tree", "--empty"); err != nil {
			return "", err
		}
	}
	for _, c := range changes {
		if c.OID == "" {
			if _, err := Run(ctx, sub, "update-index", "--force-remove", "--", c.Path); err != nil {
				return "", err
			}
			continue
		}
		mode := c.Mode
		if mode == "" {
			mode = "100644"
		}
		if _, err := Run(ctx, sub, "update-index", "--add", "--cacheinfo", mode+","+c.OID+","+c.Path); err != nil {
			return "", err
		}
	}
	res, err := Run(ctx, sub, "write-tree")
	if err != nil {
		return "", err
	}
	out := strings.TrimSpace(res.Stdout)
	if !IsOID(out) {
		return "", fmt.Errorf("write-tree returned %q, which is not an object id", out)
	}
	return out, nil
}

// CommitTree commits a tree with the given parents and message.
//
// The parents are the whole reason the loser of a body conflict is still recoverable:
// a merge commit that names only the winning side drops the other side's blob out of
// every reachable history, and `conflict-in-history` becomes a finding pointing at
// nothing.
func CommitTree(ctx context.Context, opt Options, tree string, parents []string, message string) (string, error) {
	args := []string{"commit-tree", tree}
	for _, p := range parents {
		if p == "" {
			continue
		}
		args = append(args, "-p", p)
	}
	args = append(args, "-m", message)
	res, err := Run(ctx, opt, args...)
	if err != nil {
		return "", err
	}
	out := strings.TrimSpace(res.Stdout)
	if !IsOID(out) {
		return "", fmt.Errorf("commit-tree returned %q, which is not an object id", out)
	}
	return out, nil
}

// UpdateRef moves a ref, guarded by its old value.
//
// oldOID is not optional in practice: between the merge-tree and the update-ref the
// gate may have derived and committed, and a ref move that does not name what it
// expected to replace silently discards that commit. Pass NullOID to require that the
// ref does not exist.
func UpdateRef(ctx context.Context, opt Options, ref, newOID, oldOID string) error {
	_, err := Run(ctx, opt, "update-ref", ref, newOID, oldOID)
	return err
}

// MergeFileResult is a three-way content merge.
type MergeFileResult struct {
	Merged   []byte
	Conflict bool
}

// MergeFile is a three-way line merge of three in-memory versions.
//
// git merge-file exits 0 on a clean merge and with the NUMBER of conflict hunks
// otherwise, so a non-zero exit is signal and not failure - anything above 127 is the
// real error band.
func MergeFile(ctx context.Context, opt Options, ours, base, theirs []byte, oursLabel, baseLabel, theirsLabel string) (MergeFileResult, error) {
	dir, err := os.MkdirTemp("", "ams-merge-*")
	if err != nil {
		return MergeFileResult{}, fmt.Errorf("merge scratch dir: %w", err)
	}
	defer func() { _ = os.RemoveAll(dir) }()

	write := func(name string, b []byte) (string, error) {
		p := filepath.Join(dir, name)
		if err := os.WriteFile(p, b, 0o600); err != nil {
			return "", fmt.Errorf("merge scratch %s: %w", name, err)
		}
		return p, nil
	}
	op, err := write("ours", ours)
	if err != nil {
		return MergeFileResult{}, err
	}
	bp, err := write("base", base)
	if err != nil {
		return MergeFileResult{}, err
	}
	tp, err := write("theirs", theirs)
	if err != nil {
		return MergeFileResult{}, err
	}

	sub := opt
	sub.OkExit = func(c int) bool { return c >= 0 && c < 128 }
	res, err := Run(ctx, sub, "merge-file", "--stdout",
		"-L", oursLabel, "-L", baseLabel, "-L", theirsLabel, op, bp, tp)
	if err != nil {
		return MergeFileResult{}, err
	}
	return MergeFileResult{Merged: []byte(res.Stdout), Conflict: res.Code != 0}, nil
}

// PathCommit is the last commit that touched one path on one side.
type PathCommit struct {
	OID     string
	Unix    int64
	Machine string
}

const (
	logRecordSep = "\x01"
	logFieldSep  = "\x1f"
	logHeadEnd   = "\x02"
)

// CommitTimesByPath walks rev's history ONCE and returns, per path, the commit that last
// touched it.
//
// One pass, not one `git log -1` per file: the render order needs a commit time for
// every entry in a store, and a per-file exec turns a 200-entry index into 200 process
// spawns on the hook path. --no-renames keeps a delete and an add from being folded into
// one rename row, which is the same reason merge.renames is off (a judge migration must
// never be paired with an unrelated new file).
func CommitTimesByPath(ctx context.Context, opt Options, rev string, pathspec ...string) (map[string]PathCommit, error) {
	args := []string{
		"log", "--no-renames", "--name-only",
		"--format=" + logRecordSep + "%H" + logFieldSep + "%ct" + logFieldSep +
			"%(trailers:key=Ams-Machine,valueonly,separator=%x1e)" + logHeadEnd,
		rev,
	}
	if len(pathspec) > 0 {
		args = append(args, "--")
		args = append(args, pathspec...)
	}
	res, err := Run(ctx, opt, args...)
	if err != nil {
		return nil, err
	}
	out := make(map[string]PathCommit)
	for _, rec := range strings.Split(res.Stdout, logRecordSep) {
		if strings.TrimSpace(rec) == "" {
			continue
		}
		head, names, ok := strings.Cut(rec, logHeadEnd)
		if !ok {
			return nil, fmt.Errorf("git log produced a record without a header terminator: %q", rec)
		}
		f := strings.Split(head, logFieldSep)
		if len(f) < 3 || !IsOID(f[0]) {
			return nil, fmt.Errorf("git log produced an unrecognised header %q", head)
		}
		ct, convErr := strconv.ParseInt(strings.TrimSpace(f[1]), 10, 64)
		if convErr != nil {
			return nil, fmt.Errorf("git log produced an unreadable commit time %q", f[1])
		}
		pc := PathCommit{OID: f[0], Unix: ct, Machine: strings.TrimSpace(strings.ReplaceAll(f[2], "\x1e", " "))}
		for _, name := range strings.Split(names, "\n") {
			name = strings.TrimSpace(name)
			if name == "" {
				continue
			}
			// The walk is newest-first, so the FIRST row for a path is its last change.
			if _, seen := out[name]; !seen {
				out[name] = pc
			}
		}
	}
	return out, nil
}

// MergeTreeOutput is a parsed `git merge-tree --write-tree --name-only`.
type MergeTreeOutput struct {
	// Tree is the merged tree's object id. It is present whether or not there were
	// conflicts: merge-tree resolves what it can and leaves the rest staged as stages.
	Tree string
	// Conflicted holds the paths merge-tree could not resolve on its own.
	Conflicted []string
	// Advisory is everything after the first blank line. It is HUMAN TEXT whose wording
	// changed between 2.38 and 2.45; nothing in this tool ever branches on it.
	Advisory string
	// Clean is true when merge-tree exited 0.
	Clean bool
	Raw   string
}

// ParseMergeTreeOutput reads merge-tree's stdout defensively (blueprint Q11).
//
// The contract this tool relies on, and the ONLY part of the output treated as an API:
// line 1 is the merged tree's object id, every line after it up to the first BLANK line
// is a conflicted path, and everything after that blank line is advisory prose. The
// framing of that prose, and the `info` section's fields, changed across 2.38 -> 2.45;
// a parser that reads them fails on one PC's git and not another's, which is the exact
// failure this shape check exists to catch loudly instead of silently mis-merging.
func ParseMergeTreeOutput(stdout string, exitCode int) (MergeTreeOutput, error) {
	out := MergeTreeOutput{Clean: exitCode == 0, Raw: stdout}
	text := strings.ReplaceAll(stdout, "\r\n", "\n")
	lines := strings.Split(text, "\n")
	if len(lines) == 0 || !IsOID(strings.TrimSpace(lines[0])) {
		first := ""
		if len(lines) > 0 {
			first = lines[0]
		}
		return out, fmt.Errorf("merge-tree --write-tree did not start with a tree object id (got %q): this git's output shape is not the one ams-store parses", first)
	}
	out.Tree = strings.TrimSpace(lines[0])
	rest := lines[1:]
	for i, ln := range rest {
		if strings.TrimSpace(ln) == "" {
			out.Advisory = strings.TrimRight(strings.Join(rest[i+1:], "\n"), "\n")
			return out, nil
		}
		out.Conflicted = append(out.Conflicted, ln)
	}
	// No blank line at all: a clean merge prints the oid and stops.
	if exitCode != 0 && len(out.Conflicted) > 0 {
		return out, fmt.Errorf("merge-tree reported conflicts but produced no advisory section: this git's output shape is not the one ams-store parses")
	}
	return out, nil
}

// MergeTree runs `merge-tree --write-tree --name-only --merge-base=<base>` and parses it.
//
// Exit 0 is a clean merge and exit 1 is a conflicted one; anything above that is a hard
// error (a bad object, a git too old) and is returned as such rather than parsed.
func MergeTree(ctx context.Context, opt Options, base, ours, theirs string) (MergeTreeOutput, error) {
	opt.OkExit = OkExitCodes(0, 1)
	args := []string{"merge-tree", "--write-tree", "--name-only"}
	if base != "" {
		args = append(args, "--merge-base="+base)
	}
	args = append(args, ours, theirs)
	res, err := Run(ctx, opt, args...)
	if err != nil {
		return MergeTreeOutput{}, err
	}
	return ParseMergeTreeOutput(res.Stdout, res.Code)
}
