package judge

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/dmmdea/agentic-memory-stack-for-claude-code/ams-store/internal/store"
)

// AddResult is what a migration write returned.
type AddResult struct {
	// ID is the record's id. Empty means the write is unverifiable and nothing may be
	// deleted - though a record MAY still have landed, which is why nothing is undone
	// either: the retry is hash-idempotent.
	ID string
	// Deduplicated is the server's flag saying the id belongs to a PRE-EXISTING record.
	//
	// It is the single most load-bearing field in this file. add() with infer=false
	// returns an EXISTING id on a hash hit; without the flag, a migration whose read-back
	// then failed would "undo" itself by DELETING a record it never created - an L1a fact
	// or an earlier migration. Never delete a dedup'd id.
	Deduplicated bool
}

// Record is a record read back by id.
type Record struct {
	// Text is the first present field of memory | data | text.
	Text string
	// Retrievable is the server's own reachability flag. A record that is stored but not
	// retrievable is not a place a fact may be moved to.
	Retrievable *bool
	// Found is false when the id could not be read at all.
	Found bool
}

// Mem0Client is the corpus side of a migration. It is an interface so the apply path can
// be driven against a fake in tests and so nothing in this package reaches the network
// implicitly: a nil client means migrations are impossible, never that they silently
// "succeed".
type Mem0Client interface {
	// Add writes a record and returns its id and the server's dedup flag.
	Add(ctx context.Context, text, source string, metadata map[string]string) (AddResult, error)
	// Get reads a record back BY ID.
	Get(ctx context.Context, id string) (Record, error)
	// Delete removes a record this run created and could not verify.
	Delete(ctx context.Context, id string) error
}

// Landed reports whether a record read back by id is byte-equal to what was sent.
//
// Byte equality against what we sent is the only falsifiable proof the migration landed.
// A top-ranked semantic search for the fact's own text is not proof: it can be satisfied
// by a pre-existing near-duplicate of the same fact, and the file would be deleted
// against a record that says something slightly different.
func Landed(rec Record, text string) bool {
	if !rec.Found {
		return false
	}
	if rec.Text != text {
		return false
	}
	if rec.Retrievable != nil && !*rec.Retrievable {
		return false
	}
	return true
}

// MigrationText is the verbatim text a fact file migrates as: its description, a blank
// line, its body, trimmed.
//
// VERBATIM, never a paraphrase: a re-run must produce identical bytes so a retry
// deduplicates instead of creating a second variant of the same fact in the corpus.
func MigrationText(description, body string) string {
	return strings.TrimSpace(description + "\n\n" + body)
}

// TooLargeToMigrate reports whether a migration text exceeds the server's storage cap.
//
// The cap is a CHARACTER count on the server, so it is counted in runes here, not bytes:
// a body of accented prose measured in bytes would be refused locally while the server
// would have taken it. A body genuinely over the cap is refused with a 413 on every
// attempt, nightly, forever - which is why the check runs both when the candidate set is
// built and again at apply time, against the text actually about to be sent.
func TooLargeToMigrate(text string) bool {
	return utf8.RuneCountInString(text) > store.Mem0MaxChars
}

// SourceTag is the A-to-B bridge and the dedup exemption key: it records which store and
// file a corpus record came from, so a re-created slug can be updated by id instead of
// gaining a nightly variant.
func SourceTag(workspace, slug string) string { return "automemory:" + workspace + "/" + slug }

// HTTPMem0 is the live mem0 client.
//
// It is this package's OWN writer rather than a shared helper for two reasons the
// PowerShell original spells out: a shared helper discards the server's deduplicated
// flag (see AddResult.Deduplicated), and a shared helper dead-letters failures for a
// later drain to re-post UNVERIFIED. A failed migration must simply be "not migrated".
type HTTPMem0 struct {
	// BaseURL is the authority, e.g. http://host:18791.
	BaseURL string
	// APIKey is sent as X-API-Key on every call.
	APIKey string
	// UserID is the corpus partition to write into.
	UserID string
	// Client defaults to a client with the timeouts below.
	Client *http.Client
}

// The timeouts are the PowerShell original's: 20 s for a write, 15 s for a read-back and
// for a delete. They are per-call, not per-run: a slow corpus must not hold the nightly.
const (
	mem0AddTimeout    = 20 * time.Second
	mem0GetTimeout    = 15 * time.Second
	mem0DeleteTimeout = 15 * time.Second
)

func (m *HTTPMem0) client() *http.Client {
	if m.Client != nil {
		return m.Client
	}
	return http.DefaultClient
}

func (m *HTTPMem0) endpoint(parts ...string) string {
	base := strings.TrimRight(m.BaseURL, "/")
	out := base + "/v1/memories"
	for _, p := range parts {
		out += "/" + url.PathEscape(p)
	}
	return out
}

func (m *HTTPMem0) do(ctx context.Context, method, endpoint string, body []byte, timeout time.Duration) ([]byte, int, error) {
	cctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	var rdr io.Reader
	if body != nil {
		rdr = bytes.NewReader(body)
	}
	req, err := http.NewRequestWithContext(cctx, method, endpoint, rdr)
	if err != nil {
		return nil, 0, err
	}
	if m.APIKey != "" {
		req.Header.Set("X-API-Key", m.APIKey)
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := m.client().Do(req)
	if err != nil {
		return nil, 0, err
	}
	defer resp.Body.Close()
	b, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, resp.StatusCode, err
	}
	return b, resp.StatusCode, nil
}

type addResponse struct {
	Results []struct {
		ID string `json:"id"`
	} `json:"results"`
	Deduplicated bool `json:"deduplicated"`
}

// Add posts a record with infer=false: the text is stored verbatim, not summarised by a
// model, because the whole contract of a migration is that the fact survives the move
// unchanged.
func (m *HTTPMem0) Add(ctx context.Context, text, source string, metadata map[string]string) (AddResult, error) {
	meta := map[string]string{}
	for k, v := range metadata {
		meta[k] = v
	}
	meta["source"] = source
	if _, ok := meta["tier"]; !ok {
		meta["tier"] = "evidence"
	}
	payload := map[string]any{
		"messages": text,
		"user_id":  m.UserID,
		"infer":    false,
		"metadata": meta,
	}
	body, err := json.Marshal(payload)
	if err != nil {
		return AddResult{}, err
	}
	raw, code, err := m.do(ctx, http.MethodPost, m.endpoint(), body, mem0AddTimeout)
	if err != nil {
		return AddResult{}, fmt.Errorf("mem0 add: %w", err)
	}
	if code < 200 || code > 299 {
		return AddResult{}, fmt.Errorf("mem0 add: HTTP %d: %s", code, snippet(raw))
	}
	var r addResponse
	if err := json.Unmarshal(raw, &r); err != nil {
		return AddResult{}, fmt.Errorf("mem0 add: unreadable response: %w", err)
	}
	if len(r.Results) == 0 || r.Results[0].ID == "" {
		return AddResult{}, fmt.Errorf("mem0 add: server answered without an id: %s", snippet(raw))
	}
	return AddResult{ID: r.Results[0].ID, Deduplicated: r.Deduplicated}, nil
}

// Get reads a record by id. A 404 is a record that is not there - found=false, no error -
// because "absent" is a legitimate answer to a read-back and must not be reported as a
// transport failure.
func (m *HTTPMem0) Get(ctx context.Context, id string) (Record, error) {
	raw, code, err := m.do(ctx, http.MethodGet, m.endpoint(id), nil, mem0GetTimeout)
	if err != nil {
		return Record{}, fmt.Errorf("mem0 get %s: %w", id, err)
	}
	if code == http.StatusNotFound {
		return Record{Found: false}, nil
	}
	if code < 200 || code > 299 {
		return Record{}, fmt.Errorf("mem0 get %s: HTTP %d: %s", id, code, snippet(raw))
	}
	var doc map[string]any
	if err := json.Unmarshal(raw, &doc); err != nil {
		return Record{}, fmt.Errorf("mem0 get %s: unreadable response: %w", id, err)
	}
	rec := Record{Found: true}
	for _, f := range []string{"memory", "data", "text"} {
		if v, ok := doc[f]; ok {
			if s, ok := v.(string); ok && s != "" {
				rec.Text = s
				break
			}
		}
	}
	if v, ok := doc["retrievable"]; ok {
		if b, ok := v.(bool); ok {
			rec.Retrievable = &b
		}
	}
	return rec, nil
}

// Delete removes a record. It is only ever called on an id THIS run created and could
// not verify.
func (m *HTTPMem0) Delete(ctx context.Context, id string) error {
	raw, code, err := m.do(ctx, http.MethodDelete, m.endpoint(id), nil, mem0DeleteTimeout)
	if err != nil {
		return fmt.Errorf("mem0 delete %s: %w", id, err)
	}
	if code < 200 || code > 299 {
		return fmt.Errorf("mem0 delete %s: HTTP %d: %s", id, code, snippet(raw))
	}
	return nil
}

func snippet(b []byte) string {
	s := strings.Join(strings.Fields(string(b)), " ")
	if len(s) > 200 {
		s = s[:200]
	}
	return s
}
