# Privacy gate

`scripts/ci/privacy-gate.py` keeps personal and operator-private identifiers (machine names,
people, handles, business names, private network and path shapes) out of this repository
without publishing the list of things it protects.

## What it checks

| Surface | How |
|---|---|
| Every tracked file's text | line by line |
| Every tracked path | per path component; a flagged component is printed as `***` |
| Commit messages | non-merge commits in `--range BASE..HEAD` (CI passes the PR or push range) |

Commit author name and e-mail are not checked: they cannot change without rewriting history.

Two layers:

1. **Digest layer.** `scripts/ci/privacy-digests.txt` holds one HMAC-SHA256 digest per term.
   The input is the normalized term (lowercase, letters and digits only) and the key is never
   committed. Text is tokenized (alphanumeric runs, camelCase pieces, letter/digit pieces, and
   two or three adjacent runs joined) so `two words`, `two-words` and `twoWords` all match a
   two-word term. Without the key the digests are opaque, so publishing the file reveals no
   term and cannot be reversed by a dictionary attack. Limit: matching is whole-token, so a term
   fused inside a longer lowercase word is not seen.
2. **Shape layer** (no key needed). Home directories with a real user name, tailnet FQDNs,
   private and CGNAT IP literals, MAC addresses, full GPU UUIDs, `Dev`/`repos` drive paths and
   non-reserved e-mail domains. Placeholders pass: `<user>`, `example.test`, `192.0.2.x`,
   `00:00:5E:00:53:xx`, `100.64.0.x`.

The gate never prints the matched text, only `path:line:column: category`, because CI logs of a
public repository are public. The repo's own canonical URL is listed in
`scripts/ci/privacy-gate.json` (`sanctioned`) and is stripped from each line before scanning.

## Where the key lives

The key is read from, in order: `$PRIVACY_GATE_KEY`, the file named by
`$PRIVACY_GATE_KEY_FILE`, then `~/.config/privacy-gate/key`. In CI it is the repository secret
`PRIVACY_GATE_KEY`. With no key the gate runs the shape layer only and says so; set
`PRIVACY_GATE_REQUIRE_KEY=1` to make a missing key an error (CI does this for pushes and
same-repo pull requests, where the secret is available).

Create a key once: `python -c "import secrets;print(secrets.token_hex(32))" > ~/.config/privacy-gate/key`,
then store the same value as the `PRIVACY_GATE_KEY` repository secret.

## Add a term without committing it

Read the term from stdin so it never lands in a file, a commit or shell history, then commit
only the changed digest file:

```
python scripts/ci/privacy-gate.py add-term -      # type the term(s), one per line, then Ctrl-D
git add scripts/ci/privacy-digests.txt
```

`python scripts/ci/privacy-gate.py digest -` prints digests without touching the file. Every
clone that runs the gate locally needs the same key file, or it will only run the shape layer.

## Run it

```
python scripts/ci/privacy-gate.py                      # whole tracked tree
python scripts/ci/privacy-gate.py --base origin/main   # plus messages of your commits
python scripts/ci/test_privacy_gate.py                 # proof the gate fails on a planted term
```

When it fails, replace the value with a role name (`the reference workstation`, `an edge node`,
`the operator`) or a placeholder; do not add an exemption.
