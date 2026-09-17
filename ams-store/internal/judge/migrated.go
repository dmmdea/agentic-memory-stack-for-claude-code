package judge

import (
	"context"
	"fmt"
	"path/filepath"
	"regexp"
	"strings"
	"time"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/gitx"
)

// TrailerKey is the commit trailer that carries a migration's slug-to-id mapping.
//
// Decision Q8. `migrated: <mem0 id>` in a fact file has no producer, because the file
// the id belongs to is the file the migration DELETES: there is nowhere to write it at
// the moment it is known. The mapping has to survive the deletion, so it goes into the
// deletion COMMIT, which is the one artifact that outlives the file and is already
// synced to every PC. No new transport, no ledger to keep in step.
//
// When a slug re-appears later - the same fact written again by a session that did not
// know it had been migrated - derive's harvest step looks the slug up here and writes
// `migrated: <id>` into the new file, so the judge updates the existing record by id
// instead of adding a near-duplicate variant every night.
const TrailerKey = "Migrated"

// reTrailer matches one trailer line: "Migrated: <slug> <id>".
var reTrailer = regexp.MustCompile(`(?m)^` + TrailerKey + `:[ \t]+(\S+\.md)[ \t]+(\S+)[ \t]*$`)

// MigratedTrailer renders one trailer line.
func MigratedTrailer(slug, mem0ID string) string {
	return TrailerKey + ": " + slug + " " + mem0ID
}

// Migration is one slug-to-id pair a deletion commit records.
type Migration struct {
	Slug   string
	Mem0ID string
}

// CommitMessage builds the deletion commit's message: a subject line, a blank line, and
// one trailer per migrated fact.
//
// Trailers sit in their own block at the end, which is what makes `git log --grep` and
// `git interpret-trailers` both able to read them, and what keeps a subject line
// readable in `git log --oneline`.
func CommitMessage(subject string, migrations []Migration) string {
	var b strings.Builder
	b.WriteString(subject)
	if len(migrations) == 0 {
		return b.String() + "\n"
	}
	b.WriteString("\n\n")
	for _, m := range migrations {
		b.WriteString(MigratedTrailer(m.Slug, m.Mem0ID))
		b.WriteString("\n")
	}
	return b.String()
}

// HistoryRepo is the out-of-tree local history repository: the git dir under STATE_ROOT,
// the work tree at PROJECTS_ROOT. No .git ever lands inside a store.
type HistoryRepo struct {
	GitDir   string
	WorkTree string
}

// Valid reports whether the repo is addressable at all.
func (h HistoryRepo) Valid() bool { return h.GitDir != "" }

// CommitDeletions stages the deleted fact files and commits them with one Migrated:
// trailer each, returning the new commit's short id.
//
// The pathspec is forced (-f) because a store's files reach this repo only through a
// forced add: info/exclude excludes everything and un-excludes fact files, and MEMORY.md
// stays excluded because it is derived, never merged.
func CommitDeletions(ctx context.Context, repo HistoryRepo, relPaths []string, subject string, migrations []Migration) (string, error) {
	if !repo.Valid() {
		return "", fmt.Errorf("no history repo configured")
	}
	if len(relPaths) == 0 {
		return "", nil
	}
	opt := gitx.Options{GitDir: repo.GitDir, WorkTree: repo.WorkTree, Timeout: 60 * time.Second}
	args := append([]string{"add", "-A", "-f", "--"}, relPaths...)
	if _, err := gitx.Run(ctx, opt, args...); err != nil {
		return "", fmt.Errorf("stage deletions: %w", err)
	}
	msg := CommitMessage(subject, migrations)
	// A commit with nothing staged exits 1 and says "nothing to commit"; that is a
	// legitimate no-op here (the files were already recorded as gone), not a failure.
	commitOpt := opt
	commitOpt.OkExit = gitx.OkExitCodes(0, 1)
	res, err := gitx.Run(ctx, commitOpt, "commit", "-m", msg)
	if err != nil {
		return "", fmt.Errorf("commit deletions: %w", err)
	}
	if res.Code == 1 {
		return "", nil
	}
	head, err := gitx.Run(ctx, opt, "rev-parse", "--short", "HEAD")
	if err != nil {
		return "", fmt.Errorf("read commit id: %w", err)
	}
	return strings.TrimSpace(head.Stdout), nil
}

// MigratedFor returns the mem0 id a slug was migrated under, from the history repo's
// commit trailers, newest commit first.
//
// The search is `git log --grep` over the trailer, which reads the commits that are in
// the shared history - so every PC answers the same question the same way after a sync,
// with no state file to keep in step and nothing to rebuild when a PC is re-seeded.
//
// It fails CLOSED: any error, a missing repo, an unparsable log - all answer
// (", false"), meaning "no known migration". A wrong id written into a fact file would
// make the judge update someone else's record.
func MigratedFor(repo HistoryRepo, slug string) (string, bool) {
	return migratedFor(context.Background(), repo, slug)
}

func migratedFor(ctx context.Context, repo HistoryRepo, slug string) (string, bool) {
	if !repo.Valid() || !reSlug.MatchString(slug) {
		return "", false
	}
	opt := gitx.Options{GitDir: repo.GitDir, WorkTree: repo.WorkTree, Timeout: 30 * time.Second}
	// --fixed-strings keeps a slug's dots literal; the full parse below is what decides,
	// so the grep is only a cheap pre-filter.
	res, err := gitx.Run(ctx, opt,
		"log", "--no-color", "--format=%H%x00%B%x00", "--fixed-strings",
		"--grep="+TrailerKey+": "+slug+" ", "-n", "50")
	if err != nil {
		return "", false
	}
	for _, chunk := range strings.Split(res.Stdout, "\x00") {
		for _, m := range reTrailer.FindAllStringSubmatch(chunk, -1) {
			if m[1] == slug {
				return m[2], true
			}
		}
	}
	return "", false
}

// HistoryMigrated is the concrete lookup derive's harvest step takes: it satisfies a
// one-method MigratedLookup interface (MigratedFor(slug) (string, bool)) so derive never
// imports git, and so a test can substitute a map.
type HistoryMigrated struct {
	Repo HistoryRepo
}

// MigratedFor implements the lookup.
func (h HistoryMigrated) MigratedFor(slug string) (string, bool) {
	return MigratedFor(h.Repo, slug)
}

// DeletedInHistory reports whether a store-relative path was removed by a commit in the
// shared history and is absent from HEAD - a deletion somebody DECIDED, as opposed to a
// file that merely cannot be seen right now.
//
// It is the second half of the blast cap's evidence rule (the first is the Migrated:
// trailer): a dangling index pointer whose file the history says was deleted is the
// consequence of a decision already made and synced, so removing the pointer is not a
// wipe in progress. A hand re-home of a hundred doctrine facts into topic files is
// exactly this shape - no trailer, one deletion commit, every PC then holding a hundred
// dangling pointers over its 20 % cap - and without this rule every PC refuses that
// clean-up forever (2026-09-17).
//
// It fails CLOSED: a missing repo, a path still present at HEAD, an unparsable log, any
// error at all - all answer false, which means "counts against the cap".
func DeletedInHistory(repo HistoryRepo, relPath string) (bool, error) {
	if !repo.Valid() || strings.TrimSpace(relPath) == "" {
		return false, nil
	}
	ctx := context.Background()
	opt := gitx.Options{GitDir: repo.GitDir, WorkTree: repo.WorkTree, Timeout: 30 * time.Second}
	// Present at HEAD means not deleted, whatever the log says about an earlier life.
	present := opt
	present.OkExit = gitx.OkExitCodes(0, 1, 128)
	res, err := gitx.Run(ctx, present, "cat-file", "-e", "HEAD:"+relPath)
	if err != nil {
		return false, err
	}
	if res.Code == 0 {
		return false, nil
	}
	res, err = gitx.Run(ctx, opt, "log", "-1", "--diff-filter=D", "--format=%H", "--", relPath)
	if err != nil {
		return false, err
	}
	return strings.TrimSpace(res.Stdout) != "", nil
}

// HistoryDeleted is the concrete lookup derive's blast cap takes for the deletion half of
// its evidence rule; it satisfies derive.DeletedLookup so derive never imports git.
type HistoryDeleted struct {
	Repo HistoryRepo
}

// DeletedInHistory resolves the store-relative path of a slug and asks the history.
func (h HistoryDeleted) DeletedInHistory(storeDir, slug string) (bool, error) {
	if !h.Repo.Valid() || h.Repo.WorkTree == "" {
		return false, nil
	}
	rel, err := filepath.Rel(h.Repo.WorkTree, storeDir)
	if err != nil || strings.HasPrefix(rel, "..") {
		return false, nil
	}
	return DeletedInHistory(h.Repo, filepath.ToSlash(filepath.Join(rel, slug)))
}
