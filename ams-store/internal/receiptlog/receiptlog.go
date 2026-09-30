// Package receiptlog is the one writer and tail-reader of the append-only receipt files
// (sync-receipts.jsonl, compact-receipts.jsonl).
//
// Both files were append-only with no bound: one grew to 1 MB in 34 days, about a fifth of
// it one wedged store repeating the same abort. This package bounds them two ways:
//
//   - size-triggered rotation: at MaxBytes the live file becomes <path>.1, the older
//     generations shift up, and only Keep rotated generations are retained;
//   - collapsing: when the newest row is the same event as the one being appended (the
//     caller says what "same" is), the row is rewritten with a repeat count instead of
//     appended again.
//
// Readers that tail by row count go through ReadTailLines, which reads across the newest
// rotated generation so a rotation never blinds them.
package receiptlog

import (
	"bufio"
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

// Keep is how many rotated generations are retained beside the live file.
const Keep = 3

// MaxBytes is the size at which the live file is rotated. It is a variable only so a test
// in another package can shrink it instead of writing 2 MB.
var MaxBytes int64 = 2 * 1024 * 1024

// maxCollapseLine bounds how much of the file tail is read to find the newest row.
const maxCollapseLine = 64 * 1024

// Generation is the path of the n-th rotated generation (1 is the newest).
func Generation(path string, n int) string { return path + "." + strconv.Itoa(n) }

// Append writes one row, rotating the file first when it is at or over MaxBytes. The row
// is written even when rotation fails: a lost audit line is worse than a file that stays
// a little large, and the rotation error is returned for the caller to log.
func Append(path string, v any) error {
	return AppendLimit(path, v, MaxBytes)
}

// AppendLimit is Append with an explicit rotation size, so a test does not need 2 MB.
func AppendLimit(path string, v any, maxBytes int64) error {
	b, err := json.Marshal(v)
	if err != nil {
		return fmt.Errorf("encode row for %s: %w", path, err)
	}
	return appendLine(path, b, maxBytes)
}

func appendLine(path string, line []byte, maxBytes int64) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return fmt.Errorf("create %s: %w", filepath.Dir(path), err)
	}
	rotErr := rotateIfNeeded(path, maxBytes)
	f, err := os.OpenFile(path, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		return fmt.Errorf("open %s: %w", path, err)
	}
	defer f.Close()
	if _, err := f.Write(append(line, '\n')); err != nil {
		return fmt.Errorf("append to %s: %w", path, err)
	}
	return rotErr
}

func rotateIfNeeded(path string, maxBytes int64) error {
	fi, err := os.Stat(path)
	if err != nil || fi.Size() < maxBytes {
		return nil
	}
	// Shift the generations up, oldest first; the oldest falls off the end.
	if err := os.Remove(Generation(path, Keep)); err != nil && !errors.Is(err, os.ErrNotExist) {
		return fmt.Errorf("rotate %s: %w", path, err)
	}
	for n := Keep - 1; n >= 1; n-- {
		if err := os.Rename(Generation(path, n), Generation(path, n+1)); err != nil && !errors.Is(err, os.ErrNotExist) {
			return fmt.Errorf("rotate %s: %w", path, err)
		}
	}
	if err := os.Rename(path, Generation(path, 1)); err != nil {
		return fmt.Errorf("rotate %s: %w", path, err)
	}
	return nil
}

// AppendCollapsing writes a row, unless the file's newest row is the same event: then that
// row is rewritten in place with "repeat" raised by one, "first_ts" set to the original
// timestamp and "ts" moved to this one. same receives the newest row and the new one as
// decoded maps and decides. Only the newest row is ever considered, so two different
// events between two identical ones are never merged across.
func AppendCollapsing(path string, v any, same func(prev, cur map[string]any) bool) error {
	return AppendCollapsingLimit(path, v, same, MaxBytes)
}

// AppendCollapsingLimit is AppendCollapsing with an explicit rotation size.
func AppendCollapsingLimit(path string, v any, same func(prev, cur map[string]any) bool, maxBytes int64) error {
	b, err := json.Marshal(v)
	if err != nil {
		return fmt.Errorf("encode row for %s: %w", path, err)
	}
	var cur map[string]any
	if err := json.Unmarshal(b, &cur); err != nil || same == nil {
		return appendLine(path, b, maxBytes)
	}
	prev, offset, ok := lastRow(path)
	if !ok || !same(prev, cur) {
		return appendLine(path, b, maxBytes)
	}
	repeat := 2
	if n, isNum := prev["repeat"].(float64); isNum && n >= 1 {
		repeat = int(n) + 1
	}
	cur["repeat"] = repeat
	if first, has := prev["first_ts"]; has {
		cur["first_ts"] = first
	} else if ts, has := prev["ts"]; has {
		cur["first_ts"] = ts
	}
	out, err := json.Marshal(cur)
	if err != nil {
		return appendLine(path, b, maxBytes)
	}
	f, err := os.OpenFile(path, os.O_RDWR, 0o644)
	if err != nil {
		return appendLine(path, b, maxBytes)
	}
	defer f.Close()
	if err := f.Truncate(offset); err != nil {
		return appendLine(path, b, maxBytes)
	}
	if _, err := f.Seek(offset, io.SeekStart); err != nil {
		return fmt.Errorf("seek %s: %w", path, err)
	}
	if _, err := f.Write(append(out, '\n')); err != nil {
		return fmt.Errorf("rewrite the newest row of %s: %w", path, err)
	}
	return nil
}

// lastRow returns the newest non-empty row of a file, decoded, and the byte offset where
// that row starts. ok is false when there is no such row, it does not decode, or it is
// longer than the tail window.
func lastRow(path string) (row map[string]any, offset int64, ok bool) {
	f, err := os.Open(path)
	if err != nil {
		return nil, 0, false
	}
	defer f.Close()
	fi, err := f.Stat()
	if err != nil || fi.Size() == 0 {
		return nil, 0, false
	}
	start := fi.Size() - maxCollapseLine
	if start < 0 {
		start = 0
	}
	buf := make([]byte, fi.Size()-start)
	if _, err := f.ReadAt(buf, start); err != nil && !errors.Is(err, io.EOF) {
		return nil, 0, false
	}
	trimmed := bytes.TrimRight(buf, "\r\n")
	if len(trimmed) == 0 {
		return nil, 0, false
	}
	nl := bytes.LastIndexByte(trimmed, '\n')
	if nl < 0 && start > 0 {
		return nil, 0, false // the row starts before the window
	}
	line := trimmed[nl+1:]
	if err := json.Unmarshal(line, &row); err != nil {
		return nil, 0, false
	}
	return row, start + int64(nl+1), true
}

// SameAbortedRow is the collapsing rule both receipt writers use: two consecutive rows are
// the same event when they are for one workspace, carry the same aborted-* status and the
// same note and measurements, and neither is a dry run. A store wedged in one abort writes
// this row on every sync, and 231 of them in a week were 15 % of a file.
func SameAbortedRow(prev, cur map[string]any) bool {
	st, _ := cur["status"].(string)
	if !strings.HasPrefix(st, "aborted-") {
		return false
	}
	for _, k := range []string{"workspace", "status", "note", "before_bytes", "before_lines", "dry_run"} {
		if fmt.Sprint(prev[k]) != fmt.Sprint(cur[k]) {
			return false
		}
	}
	return true
}

// Expand returns how many rows a decoded row stands for: 1, or its "repeat" count. A
// reader that windows by row count uses it so a collapsed run still counts as the run it
// was.
func Expand(repeat int) int {
	if repeat < 1 {
		return 1
	}
	return repeat
}

// ReadTailLines returns the last n non-empty lines of a receipts file, reading across the
// newest rotated generation when the live file holds fewer than n. A missing file is no
// lines and no error; an unreadable one is an error, because "I could not read the ledger"
// must never be spelled the same way as "nothing has run".
func ReadTailLines(path string, n int) ([]string, error) {
	live, err := readLines(path)
	if err != nil {
		return nil, err
	}
	if n <= 0 || len(live) < n {
		older, err := readLines(Generation(path, 1))
		if err != nil {
			return nil, err
		}
		live = append(older, live...)
	}
	if n > 0 && len(live) > n {
		live = live[len(live)-n:]
	}
	return live, nil
}

func readLines(path string) ([]string, error) {
	f, err := os.Open(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil, nil
		}
		return nil, fmt.Errorf("read %s: %w", path, err)
	}
	defer f.Close()
	var lines []string
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 0, 64*1024), 4*1024*1024)
	for sc.Scan() {
		if l := strings.TrimSpace(sc.Text()); l != "" {
			lines = append(lines, l)
		}
	}
	if err := sc.Err(); err != nil {
		return nil, fmt.Errorf("scan %s: %w", path, err)
	}
	return lines, nil
}
