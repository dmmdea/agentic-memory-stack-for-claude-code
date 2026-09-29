"""Server-side secret redaction for STORED text.

Strip credential-shaped substrings before they land in a persistent, queryable store. Applied at
the checkpoint chokepoint (_checkpoint_core) so prompt_text from BOTH /v1/episodes/checkpoint and
/v1/context/bundle (the daemon hot-path) is scrubbed regardless of which client POSTed it.

One canonical pattern set, FOUR copies — three in this repo (this module, the L1a reader
`scripts/windows/memory-common.ps1 Redact-Secrets`, and the PreCompact sidecar
`claude-config/precompact_capture.py`), plus the SkillOpt reader (`skillopt harvest.redact_secrets`)
which lives on the offline replica host and is therefore NOT covered by this repo's tests. The
docstring here said "three runtimes" until 2026-08-07; the count was wrong and the fourth copy is
the one that can silently drift. The three in-repo copies are pinned to each other by a single
shared fixture, `tests/fixtures/redaction-cases.jsonl`, which all three test suites iterate; the
SkillOpt copy adopts the same fixture on the replica's next return.

Design rules the pattern set obeys (each is a live defect this set fixed, 2026-08-07 / AMS-12):

* **Left boundary, not `\\b`.** `sk-[A-Za-z0-9_-]{10,}` with no left anchor corrupted 12 live
  stored points: `task-notification` -> `ta[REDACTED_OPENAI_KEY]`, and likewise `disk-temperature`,
  `02-disk-partition-runbook.md`, `Task-and-Session-Assistant`, `disk-constrained`.
  `(?<![A-Za-z0-9])` is used instead of `\\b` because `_` is a word character: `\\b` would also
  refuse to fire on a genuine `MY_sk-...`, and (worse, see below) cannot match `API_KEY` after `_`.
* **Prefix tolerance on the assignment rule.** `\\b(api[_-]?key|token|...)` cannot match `API_KEY`
  inside `MEM0_API_KEY=` at all -- `_` is a word char, so there is no boundary. Every
  `FOO_API_KEY=`, `CRON_SECRET=`, `GITHUB_TOKEN=`, `X-Api-Key:` in the corpus was invisible.
  Two independent changes fix that, and the fixture pins them separately: swapping `\\b` for
  `(?<![A-Za-z0-9])` already admits a keyword that follows `_` or `-`, which covers those three
  live shapes; the `[A-Za-z0-9]*[_-]?` prefix additionally absorbs a vendor prefix glued on with
  NO separator (`apiToken=`, `vercelSecret:`), which the lookbehind alone still rejects.
* **`[ \\t]`, never `\\s`, around the separator.** `\\s*` spans the `\\n\\n` turn break in a joined
  transcript, so `API token:\\n\\n[assistant] Sure` redacted the next role tag.
* **The value class never spans a newline and is bounded.** An unbalanced quote in a transcript
  window would otherwise swallow the rest of the conversation; a multi-line quoted value loses its
  first line only. Escapes are consumed (`\\\\.`) so a value like `"abc\\"def12345"` loses its tail
  too instead of leaking it. KNOWN LIMIT of the 200-char cap: a single-line quoted value longer
  than 200 characters has its first 200 redacted and its tail left in place. The cap is a
  deliberate blast-radius bound, and `tests/fixtures/redaction-cases.jsonl` pins the consequence
  explicitly (`quoted-value-over-200-chars-keeps-its-tail`) so it cannot change unnoticed. In
  practice the >200-char credentials that actually occur -- JWTs and PEM blocks -- have their own
  rules and are matched whole.
* **Credential-shaped values only.** An unquoted value must be >= 16 chars or contain a digit, so
  `password: never store passwords in mem0` survives while an `export MEM0_API_KEY=<16+ chars>`
  assignment does not. (Prose here deliberately avoids a literal key-shaped example: secret
  scanners match `KEY=<16 alnum>` in any context, docstrings included — see the fixture, whose
  cases are split at load time for the same reason.)
* **Provider families carry quantifiers and the same left boundary**, or they become the next `sk-`.
* **No bare-hex and no bare-`Bearer` rule** -- both eat live content (git SHAs, Cloudflare zone
  ids, and a stored fact whose text is "THREE Vercel Bearer API tokens embedded plaintext").
* **Session-12 additions (appended, so the generic rules keep their `pattern_<index>` telemetry
  keys).** Shapes observed unredacted in live stored points: the `vcp_` / `sbp_` / `cfut_` /
  `sk_live_` / `re_` / `tskey-` / `whsec_` / `AIza` provider families and Telegram bot tokens
  (`<8-10 digits>:AA<30+>`); the keyword-with-a-SPACE forms the assignment rule could not see
  (`API key: <tok>`, `API key <tok>`); `<label> is <tok>` / `<label> value is <tok>`; and
  `login <user> / <password>`. The label-gated rules take only a 20+ alphanumeric token that
  contains a digit and no `-`/`_`/`/`, so kebab slugs and paths after a label stay
  (`API key rotation-2026-09-28-review`); `re_` additionally needs an uppercase letter and refuses
  a `_` on its left (`re_search_results_2026_09_28`); a bare `login <user> / <word>` needs a digit
  or symbol in the word (`login page / logout flow` stays), an `@` in the user is enough on its own.
  The fixture pins a positive and a negative row for each.

Every pattern carries an EXPLICIT case flag. PowerShell's `-replace` is case-insensitive by
default and Python's `re` is not; that single difference was the whole observed drift between the
copies (`SK-UPPER` redacted in PowerShell, kept in Python). Spelling `(?i)` / `(?-i:...)` into the
pattern string makes the strings byte-identical across runtimes and the behaviour identical too.
"""
from __future__ import annotations

import re
from typing import Optional

# (pattern, replacement). The pattern strings are byte-identical to the PowerShell and PreCompact
# copies; only the backreference syntax differs (\1 here, $1 there) -- which is why the shared
# fixture asserts on SUBSTRINGS and never on exact output equality.
_SECRET_PATTERNS = tuple(
    (re.compile(p), r)
    for p, r in (
        # OpenAI-style key.
        (r"(?i)(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{10,}", "[REDACTED_OPENAI_KEY]"),
        # Authorization headers (the only Bearer/Basic rule -- a bare `Bearer <word>` rule eats prose).
        (r"(?i)(Authorization[ \t]*:[ \t]*Bearer[ \t]+)[^\s\"']+", r"\1[REDACTED]"),
        (r"(?i)(Authorization[ \t]*:[ \t]*Basic[ \t]+)[^\s\"']+", r"\1[REDACTED]"),
        # Prefix-tolerant `<vendor>_<keyword> = <value>` assignment, quoted or bare.
        (r"(?i)((?<![A-Za-z0-9])[A-Za-z0-9]*[_-]?(?:api[_-]?key|token|password|passwd|secret)"
         r"[ \t]*[:=][ \t]*)"
         r"(?:[\"'](?:\\.|[^\\\r\n]){4,200}[\"']|[\"'](?:\\.|[^\"'\\\r\n]){4,200}"
         r"|(?=[^\s\"'\r\n]{4})(?:[^\s\"'\r\n]{16,200}|[^\s\"'\r\n]{0,200}[0-9][^\s\"'\r\n]{0,200}))",
         r"\1[REDACTED]"),
        # W5 T6.2: JSON-quoted-key assignment (`{"api_key": "..."}`) — the
        # closing quote between keyword and colon made every JSON credential
        # invisible to the rule above. A SEPARATE rule, deliberately narrower:
        # the value must carry a DIGIT, because quoted-key/quoted-value is
        # also the shape of prose JSON ('"secret": "sauce of the design"' is
        # a pinned must-keep) — the >=16-chars-alone arm of the bare rule
        # would eat it. A digit-less JSON password is missed BY DESIGN and
        # the fixture pins both sides.
        (r"(?i)([\"'][A-Za-z0-9]*[_-]?(?:api[_-]?key|token|password|passwd|secret)[\"']"
         r"[ \t]*:[ \t]*[\"'])([^\"'\r\n]{0,200}[0-9][^\"'\r\n]{0,200})([\"'])",
         r"\1[REDACTED]\3"),
        # A labelled value line: `Value: <32+ alnum>` / `- Key: `<hex>``. This is the corpus shape
        # where the key NAME is on one line and the credential on the next; a >= 32-char unbroken
        # alphanumeric run is the entropy floor that keeps paths and dated slugs out.
        # W5 T6.2: the second alternative admits DASH-SEPARATED HEX (UUID-shaped
        # secrets) — hex chars only, so kebab-case slugs under a `value:` label
        # stay untouched, and benign labels (`zone id:`, `Correlation-Id:`) are
        # outside the keyword set entirely (the no-bare-hex rationale bounds
        # this: label-gated, hex-only, >=32 chars incl. dashes).
        (r"(?im)^([ \t]*(?:[-*+][ \t]+)?(?:value|key|api[_-]?key|token|password|passwd|secret)"
         r"[ \t]*[:=][ \t]*)[\"'\x60]?(?:[A-Za-z0-9]{32,}[A-Za-z0-9+/=_-]*|[0-9a-f][0-9a-f-]{31,})",
         r"\1[REDACTED]"),
        # Provider families.
        # W5 T6.2: `{36,}` — the exactly-{36} quantifier left the tail of
        # longer-than-36 tokens in cleartext (verified: a 40-char token leaked
        # its last 4 chars).
        (r"(?-i:(?<![A-Za-z0-9])gh[pousr]_[A-Za-z0-9]{36,})", "[REDACTED_GITHUB_TOKEN]"),
        (r"(?-i:(?<![A-Za-z0-9])github_pat_[A-Za-z0-9_]{60,})", "[REDACTED_GITHUB_TOKEN]"),
        (r"(?-i:(?<![A-Za-z0-9])xox[baprs]-[A-Za-z0-9-]{10,})", "[REDACTED_SLACK_TOKEN]"),
        (r"(?-i:(?<![A-Za-z0-9])nvapi-[A-Za-z0-9_-]{60,})", "[REDACTED_NVIDIA_KEY]"),
        (r"(?-i:(?<![A-Za-z0-9])(?:AKIA|ASIA)[0-9A-Z]{16})", "[REDACTED_AWS_KEY]"),
        (r"(?-i:(?<![A-Za-z0-9])eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}(?:\.[A-Za-z0-9_-]+)?)",
         "[REDACTED_JWT]"),
        # scheme://user:pass@host
        (r"(?i)(?<![A-Za-z0-9+.-])([a-z][a-z0-9+.-]{0,31}://[^\s:@/]{1,64}):[^\s:@/]{1,256}@", r"\1:[REDACTED]@"),
        # PEM private key block.
        (r"(?is)-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
         "[REDACTED_PRIVATE_KEY]"),
        # Session-12 additions, APPENDED so the generic rules keep their 'pattern_<index>' telemetry
        # keys. Provider families first: Vercel / Supabase / Cloudflare / Stripe / Resend / Tailscale /
        # Google, each shape seen live and unredacted.
        (r"(?-i:(?<![A-Za-z0-9])vcp_[A-Za-z0-9]{20,})", "[REDACTED_VERCEL_TOKEN]"),
        (r"(?-i:(?<![A-Za-z0-9])sbp_[A-Za-z0-9]{20,})", "[REDACTED_SUPABASE_TOKEN]"),
        (r"(?-i:(?<![A-Za-z0-9])cfut_[A-Za-z0-9]{20,})", "[REDACTED_CLOUDFLARE_TOKEN]"),
        (r"(?-i:(?<![A-Za-z0-9])sk_live_[A-Za-z0-9]{16,})", "[REDACTED_STRIPE_KEY]"),
        # re_ is a two-letter prefix, so it also needs a digit AND an uppercase letter and a left
        # boundary that refuses `_` (re_search_results_2026_09_28 stays)
        (r"(?-i:(?<![A-Za-z0-9_])re_(?=[A-Za-z0-9_]*[0-9])(?=[A-Za-z0-9_]*[A-Z])[A-Za-z0-9_]{24,})", "[REDACTED_RESEND_KEY]"),
        (r"(?-i:(?<![A-Za-z0-9])tskey-[A-Za-z0-9-]{20,})", "[REDACTED_TAILSCALE_KEY]"),
        (r"(?-i:(?<![A-Za-z0-9])whsec_[A-Za-z0-9]{16,})", "[REDACTED_STRIPE_WEBHOOK_SECRET]"),
        (r"(?-i:(?<![A-Za-z0-9])AIza[A-Za-z0-9_-]{30,})", "[REDACTED_GOOGLE_API_KEY]"),
        # Telegram bot token: 8-10 digit bot id, colon, `AA`, 30+ url-safe chars
        (r"(?-i:(?<![A-Za-z0-9])[0-9]{8,10}:AA[A-Za-z0-9_-]{30,})", "[REDACTED_TELEGRAM_TOKEN]"),
        # `API key <tok>` / `API key: <tok>` with a SPACE inside the keyword (the assignment rule's
        # `api[_-]?key` never matched it). The token is 20+ alphanumerics with a digit, no `-`/`_`/`/`,
        # so kebab slugs and paths after the label stay
        (r"(?i)((?<![A-Za-z0-9])(?:api|access|auth|secret|private)[ \t]+(?:key|token)[ \t]*[:=]?[ \t]*[\x22\x27\x60]?)(?=[A-Za-z0-9]{0,200}[0-9])[A-Za-z0-9]{20,200}", r"\1[REDACTED]"),
        # `<label> is <tok>` / `<label> value is <tok>` / `<label> (note) is <tok>`, same token class as
        # above
        (r"(?i)((?<![A-Za-z0-9])[A-Za-z0-9]*[_-]?(?:api[_ \t-]?key|token|password|passwd|secret)(?:[ \t]*\([^)\r\n]{0,40}\))?[ \t]+(?:value[ \t]+)?is[ \t]+[\x22\x27\x60]?)(?=[A-Za-z0-9]{0,200}[0-9])[A-Za-z0-9]{20,200}", r"\1[REDACTED]"),
        # `login <user> / <password>`: a `@` in the user is enough; a bare user needs a digit or symbol
        # in the password so `login page / logout flow` stays
        (r"(?i)((?<![A-Za-z0-9])login[ \t]+(?:[^\s/@]{1,64}@[^\s/]{1,64}[ \t]*/[ \t]*|[^\s/@]{1,64}[ \t]*/[ \t]*(?=\S*[0-9!@#$%^&*])))[^\s\x22\x27\x60]{4,128}", r"\1[REDACTED]"),
    )
)


def redact_secrets(text: Optional[str]) -> Optional[str]:
    """Replace credential-shaped substrings with markers. None/empty pass through unchanged; safe
    prose is untouched (only credential-shaped assignments, labelled value lines, and known
    provider/PEM shapes are scrubbed)."""
    if not text:
        return text
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def count_redactions(text: Optional[str]) -> dict:
    """W5 T6.1 (count-only entrance telemetry): how many redactions WOULD
    apply to `text`, per runtime-derived rule key — the marker name for
    family-named rules ('REDACTED_GITHUB_TOKEN'), else 'pattern_<index>' for
    the generic '[REDACTED]' rules (review R8: no structural change to the
    byte-pinned tuples). NEVER mutates or stores anything; the store's
    entrance policy is the operator's fork, this only measures it."""
    counts: dict = {}
    if not text:
        return counts
    # Review fix 8: SEQUENTIAL subn on a working copy — running every pattern
    # against the ORIGINAL text double-counted one credential matched by two
    # rules; redact_secrets applies rules sequentially, so the honest
    # would-apply count must too. The input is never mutated (subn copies).
    work = text
    for i, (pattern, replacement) in enumerate(_SECRET_PATTERNS):
        work, n = pattern.subn(replacement, work)
        if n:
            m = re.search(r"REDACTED_[A-Z_]+", str(replacement))
            key = m.group(0) if m else f"pattern_{i}"
            counts[key] = counts.get(key, 0) + n
    return counts


def find_credentials(text: Optional[str]) -> list:
    """S12 (L10 credential detector): `[(rule_key, value), ...]` for every credential-shaped span
    the rule set matches in `text` -- the same sequential application and the same rule keys as
    `count_redactions` (family marker name, else 'pattern_<index>'), but keeping WHAT matched so a
    caller can veto benign matches (an env-var reference, a path). For the `<label><sep><value>`
    rules the value is the part after the label. NEVER mutates or stores anything; the caller
    decides what to do with the values and must not persist them."""
    found: list = []
    if not text:
        return found
    work = text
    for i, (pattern, replacement) in enumerate(_SECRET_PATTERNS):
        m_key = re.search(r"REDACTED_[A-Z_]+", str(replacement))
        key = m_key.group(0) if m_key else f"pattern_{i}"
        keep_prefix = str(replacement).startswith("\\1")

        def _record(m, key=key, keep_prefix=keep_prefix, replacement=replacement):
            whole = m.group(0)
            found.append((key, whole[len(m.group(1)):] if keep_prefix else whole))
            return m.expand(replacement)

        work = pattern.sub(_record, work)
    return found
