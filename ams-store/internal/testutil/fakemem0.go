package testutil

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
)

// Mem0Mode selects how the fake corpus behaves. Each mode is a failure the live server
// really produces, and each has a guard that only fires against it.
type Mem0Mode string

const (
	// Mem0OK writes and reads back byte-identically.
	Mem0OK Mem0Mode = "ok"
	// Mem0NoID accepts the write and answers without an id: the write may or may not
	// have landed, so nothing may be deleted and nothing may be undone.
	Mem0NoID Mem0Mode = "noid"
	// Mem0Mismatch reads back different text: the write is unverifiable and must be
	// undone, because a record nothing indexes is a record nothing can ever clean up.
	Mem0Mismatch Mem0Mode = "mismatch"
	// Mem0DedupMismatch reads back different text AND reports the id as deduplicated:
	// the id belongs to a pre-existing record this run did not create, so it must be
	// left alone even though the read-back failed.
	Mem0DedupMismatch Mem0Mode = "dedup-mismatch"
	// Mem0DedupOK reads back byte-identically AND reports the id as deduplicated: the
	// migration verifies against a PRE-EXISTING record whose text happens to match -
	// the same fact migrated on an earlier night, or an L1a extraction of it. The
	// migration is legitimate, but the record is not this run's to remove, so an undo
	// must leave it in place and say so.
	Mem0DedupOK Mem0Mode = "dedup-ok"
	// Mem0NotRetrievable stores the right text but reports it as unreachable.
	Mem0NotRetrievable Mem0Mode = "not-retrievable"
)

// Mem0Post is one recorded write.
type Mem0Post struct {
	ID       string
	Text     string
	Source   string
	Metadata map[string]string
	APIKey   string
}

// Mem0Put is one recorded update (PUT /v1/memories/{id}).
type Mem0Put struct {
	ID   string
	Text string
}

// FakeMem0 is an in-process mem0 authority.
//
// It speaks the wire protocol rather than substituting the client, so the tests exercise
// the real HTTP client: the X-API-Key header, the infer=false body, the results[0].id
// shape and the deduplicated flag are all under test, and a client that stopped sending
// the key would be caught here rather than in production.
type FakeMem0 struct {
	Server *httptest.Server
	// OnAdd, when set, is called after a write is recorded and before the response is
	// written. It is how a test makes something else happen DURING a run - a concurrent
	// index write, say - without a seam into the code under test.
	OnAdd func(post Mem0Post)

	// DeleteStatus, when non-zero, makes every delete answer with that HTTP status and
	// remove nothing - a corpus that cannot take back an unverified write.
	DeleteStatus int

	// PutStatus, when non-zero, makes every update answer with that HTTP status and change
	// nothing - a corpus that refuses a write (a protected tier, an unavailable embedder).
	PutStatus int

	mu      sync.Mutex
	mode    Mem0Mode
	puts    []Mem0Put
	n       int
	posts   []Mem0Post
	deleted []string
	byID    map[string]string
	bySrc   map[string]string
}

// NewFakeMem0 starts a fake corpus and stops it when the test ends.
func NewFakeMem0(t *testing.T, mode Mem0Mode) *FakeMem0 {
	t.Helper()
	f := &FakeMem0{mode: mode, byID: map[string]string{}, bySrc: map[string]string{}}
	mux := http.NewServeMux()
	mux.HandleFunc("/v1/memories", f.handleCollection)
	mux.HandleFunc("/v1/memories/", f.handleItem)
	f.Server = httptest.NewServer(mux)
	t.Cleanup(f.Server.Close)
	return f
}

// URL is the authority base URL.
func (f *FakeMem0) URL() string { return f.Server.URL }

// Posts returns every recorded write.
func (f *FakeMem0) Posts() []Mem0Post {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]Mem0Post(nil), f.posts...)
}

// Puts returns every recorded update.
func (f *FakeMem0) Puts() []Mem0Put {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]Mem0Put(nil), f.puts...)
}

// Seed stores a record under a chosen id with NO source, the way a hand-written or
// foreign record looks to the judge.
func (f *FakeMem0) Seed(id, text string) { f.SeedWithSource(id, text, "") }

// SeedWithSource stores a record under a chosen id with a source tag, the way an earlier
// night's migration left it (the tag is "automemory:<workspace>/<slug>").
func (f *FakeMem0) SeedWithSource(id, text, source string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.byID[id] = text
	f.bySrc[id] = source
}

// Text returns a stored record's current text.
func (f *FakeMem0) Text(id string) (string, bool) {
	f.mu.Lock()
	defer f.mu.Unlock()
	t, ok := f.byID[id]
	return t, ok
}

// Deleted returns every id deleted through the API.
func (f *FakeMem0) Deleted() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]string(nil), f.deleted...)
}

// SourceTags returns the source metadata of every write, joined, for a contains-style
// assertion.
func (f *FakeMem0) SourceTags() string {
	var b strings.Builder
	for _, p := range f.Posts() {
		b.WriteString(p.Source)
		b.WriteString("\n")
	}
	return b.String()
}

func (f *FakeMem0) handleCollection(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		w.WriteHeader(http.StatusMethodNotAllowed)
		return
	}
	body, _ := io.ReadAll(r.Body)
	var payload struct {
		Messages string            `json:"messages"`
		UserID   string            `json:"user_id"`
		Infer    bool              `json:"infer"`
		Metadata map[string]string `json:"metadata"`
	}
	if err := json.Unmarshal(body, &payload); err != nil {
		http.Error(w, "bad body", http.StatusBadRequest)
		return
	}

	f.mu.Lock()
	f.n++
	id := fmt.Sprintf("stub-id-%04d", f.n)
	post := Mem0Post{ID: id, Text: payload.Messages, Source: payload.Metadata["source"], Metadata: payload.Metadata, APIKey: r.Header.Get("X-API-Key")}
	f.posts = append(f.posts, post)
	f.byID[id] = payload.Messages
	f.bySrc[id] = payload.Metadata["source"]
	mode := f.mode
	f.mu.Unlock()

	if f.OnAdd != nil {
		f.OnAdd(post)
	}

	w.Header().Set("Content-Type", "application/json")
	if mode == Mem0NoID {
		_, _ = w.Write([]byte(`{"results":[]}`))
		return
	}
	resp := map[string]any{
		"results":      []map[string]string{{"id": id}},
		"deduplicated": mode == Mem0DedupMismatch || mode == Mem0DedupOK,
	}
	_ = json.NewEncoder(w).Encode(resp)
}

func (f *FakeMem0) handleItem(w http.ResponseWriter, r *http.Request) {
	id := strings.TrimPrefix(r.URL.Path, "/v1/memories/")
	f.mu.Lock()
	text, ok := f.byID[id]
	src := f.bySrc[id]
	mode := f.mode
	if r.Method == http.MethodDelete && ok && f.DeleteStatus == 0 {
		f.deleted = append(f.deleted, id)
		delete(f.byID, id)
	}
	f.mu.Unlock()

	switch r.Method {
	case http.MethodDelete:
		if f.DeleteStatus != 0 {
			w.WriteHeader(f.DeleteStatus)
			return
		}
		if !ok {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"deleted":true}`))
	case http.MethodPut:
		if f.PutStatus != 0 {
			w.WriteHeader(f.PutStatus)
			_, _ = w.Write([]byte(`{"detail":"refused"}`))
			return
		}
		if !ok {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		var payload struct {
			Text string `json:"text"`
		}
		body, _ := io.ReadAll(r.Body)
		if err := json.Unmarshal(body, &payload); err != nil {
			http.Error(w, "bad body", http.StatusBadRequest)
			return
		}
		f.mu.Lock()
		f.byID[id] = payload.Text
		f.puts = append(f.puts, Mem0Put{ID: id, Text: payload.Text})
		f.mu.Unlock()
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"message":"updated"}`))
	case http.MethodGet:
		if !ok {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		doc := map[string]any{"id": id, "memory": text, "retrievable": true}
		if src != "" {
			doc["source"] = src
		}
		if mode == Mem0Mismatch || mode == Mem0DedupMismatch {
			doc["memory"] = "a near-duplicate of the same fact, reworded"
		}
		if mode == Mem0NotRetrievable {
			doc["retrievable"] = false
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(doc)
	default:
		w.WriteHeader(http.StatusMethodNotAllowed)
	}
}
