package sync

import (
	"context"
	"fmt"
	"net"
	"regexp"
	"strings"
)

// The remote policy, DESIGN:170 (Y3) and blueprint section 5.4.
//
// Exactly ONE remote is permitted, named `hub`, over SSH, whose host is a tailnet
// MagicDNS name. Anything else refuses the sync with exit 3 and is a lint finding.
//
// Why so narrow: these stores hold credentials and private brand facts. A second remote,
// or an https URL, or a raw address, is a path off the tailnet - and the hub is the
// operator's own box, nothing else is. A LAN or CGNAT literal is refused even when it
// points at the right machine, because an address that is right today is a stale address
// after the next DHCP lease and the failure mode is a silent push to whoever holds it.
//
// The expected host is NOT compiled in. It is read from configuration, so this source
// carries a SHAPE rule and never a machine name.

// reSCPLike matches git's scp-like syntax: user@host:path.
var reSCPLike = regexp.MustCompile(`^(?:([^@/]+)@)?([A-Za-z0-9._-]+):(?:[^/].*|/.*)$`)

// reSSHURL matches ssh://user@host[:port]/path. The host alternative accepts a bracketed
// IPv6 literal so such a URL is refused for being an ADDRESS rather than for being
// malformed - a reason the operator can act on.
var reSSHURL = regexp.MustCompile(`^ssh://(?:([^@/]+)@)?(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9._-]+)(?::\d+)?(/.*)$`)

// reMagicDNS is the shape a tailnet MagicDNS name has: a single DNS label. A dotted name
// is a FQDN or a search-domain guess, and neither is the tailnet.
var reMagicDNS = regexp.MustCompile(`^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$`)

// RemotePolicy is the configured expectation. ExpectedHost is optional: when set, the
// hub's host must equal it exactly; when empty, the shape rules alone govern.
type RemotePolicy struct {
	ExpectedHost string
	// AllowLocalPath permits a plain filesystem path as the hub URL. It is FALSE on
	// every PC and true only on the hub itself, whose own ams-store checkout has the
	// bare repository sitting beside it on the same disk (blueprint section 8: "the
	// hub holds its own checkout of the bare repository and judges that"). Allowing it by
	// default would let a PC quietly point at a path that syncs with nothing.
	AllowLocalPath bool
}

// RemoteCheck is one remote judged against the policy.
type RemoteCheck struct {
	Name   string
	URL    string
	OK     bool
	Reason string
}

// CheckURL judges one hub URL. It returns "" when the URL is acceptable and a
// human-readable reason when it is not.
func (p RemotePolicy) CheckURL(url string) string {
	url = strings.TrimSpace(url)
	if url == "" {
		return "the hub remote has no URL"
	}
	// A filesystem path is decided BEFORE the URL forms are tried. On Windows a drive
	// path parses as git's scp-like user@host:path with the drive letter as the host
	// ("C:/hub.git" -> host "C"), so without this branch a local path would be judged as
	// if it named a machine - and a one-letter host looks exactly like a MagicDNS label.
	if isLocalPath(url) {
		if p.AllowLocalPath {
			return ""
		}
		return "the hub remote is a local path; only the hub's own checkout may use one"
	}
	// A "://" settles the form before the scp-like pattern is tried. That pattern
	// otherwise matches "https://host/path" with "https" as the HOST - a one-word host
	// that then passes every MagicDNS check - and an https remote would be accepted.
	var host string
	if i := strings.Index(url, "://"); i >= 0 {
		if url[:i] != "ssh" {
			return "the hub remote must be SSH; " + url[:i] + " is not permitted (these stores hold credentials)"
		}
		m := reSSHURL.FindStringSubmatch(url)
		if m == nil {
			return "the hub URL is not a well-formed ssh:// URL"
		}
		host = m[2]
	} else if reSCPLike.MatchString(url) {
		host = reSCPLike.FindStringSubmatch(url)[2]
	} else {
		return "the hub remote must be SSH; " + scheme(url) + " is not permitted (these stores hold credentials)"
	}
	host = strings.TrimSuffix(strings.TrimPrefix(host, "["), "]")
	if ip := net.ParseIP(host); ip != nil {
		return "the hub host is an IP literal; reach is MagicDNS only, never a LAN or CGNAT address"
	}
	if strings.Contains(host, ".") {
		return "the hub host is a dotted name; reach is the tailnet MagicDNS name only"
	}
	if !reMagicDNS.MatchString(host) {
		return "the hub host is not a MagicDNS name"
	}
	if p.ExpectedHost != "" && !strings.EqualFold(host, p.ExpectedHost) {
		return "the hub host is not the configured hub"
	}
	return ""
}

// Check judges every configured remote of a repository.
func (p RemotePolicy) Check(ctx context.Context, r Repo) ([]RemoteCheck, error) {
	names, err := r.Remotes(ctx)
	if err != nil {
		return nil, err
	}
	out := make([]RemoteCheck, 0, len(names))
	for _, n := range names {
		url, err := r.RemoteURL(ctx, n)
		if err != nil {
			return nil, err
		}
		c := RemoteCheck{Name: n, URL: url}
		if n != HubRemote {
			c.Reason = "only one remote is permitted and it must be named " + HubRemote
		} else {
			c.Reason = p.CheckURL(url)
		}
		c.OK = c.Reason == ""
		out = append(out, c)
	}
	return out, nil
}

// Validate is Check reduced to one answer: nil when the repository's remotes are
// acceptable, an error naming the first offender otherwise. sync refuses with exit 3 on
// a non-nil result.
func (p RemotePolicy) Validate(ctx context.Context, r Repo) error {
	checks, err := p.Check(ctx, r)
	if err != nil {
		return err
	}
	for _, c := range checks {
		if !c.OK {
			return fmt.Errorf("remote %q: %s", c.Name, c.Reason)
		}
	}
	return nil
}

// HasHub reports whether the hub remote is configured. No remote at all is not a fault:
// a PC with no hub is offline-only, derives and commits locally, and says so.
func HasHub(ctx context.Context, r Repo) (bool, error) {
	names, err := r.Remotes(ctx)
	if err != nil {
		return false, err
	}
	for _, n := range names {
		if n == HubRemote {
			return true, nil
		}
	}
	return false, nil
}

// isLocalPath reports whether a URL is a plain filesystem path rather than a network
// URL. git accepts both; only the hub's own checkout may use the former.
func isLocalPath(url string) bool {
	if strings.Contains(url, "://") {
		return strings.HasPrefix(url, "file://")
	}
	if strings.HasPrefix(url, "/") || strings.HasPrefix(url, `\`) {
		return true
	}
	// A Windows drive letter: C:\path or C:/path. The scp-like form user@host:path is
	// excluded because its prefix is longer than one character.
	if len(url) >= 3 && url[1] == ':' && (url[2] == '\\' || url[2] == '/') {
		return true
	}
	return false
}

func scheme(url string) string {
	if i := strings.Index(url, "://"); i > 0 {
		return url[:i]
	}
	if strings.HasPrefix(url, "/") || strings.HasPrefix(url, `\`) {
		return "a local path"
	}
	return "that URL form"
}
