"""imperative_canary.py — v0.29 Phase 2a promote-canary.

Pure helper: is_imperative_canonical(text) -> bool

Canonical tier is declarative facts only.  This module flags standing-order
phrasing that is inappropriate for canonical records.

Canary lexicon (v0.29):
  MUST | NEVER | ALWAYS | DO NOT | DON'T | SHALL | YOU MUST | RULE:

FIRES on  : leading/emphatic standing orders — MUST/NEVER/ALWAYS/DO NOT/DON'T/SHALL
            at the START of a sentence; "you must" anywhere; a leading "RULE:" prefix.
MUST NOT  : declarative facts that happen to contain these words in a descriptive
            sentence, e.g. "The reserved ports are …", "Ollama :11434 is decommissioned",
            "Postiz is a retired, forbidden social scheduler", "over-long inputs defer."

Strategy: ANCHORED, narrow regex — checked PER-SENTENCE so that an imperative
in the second (or later) sentence of a multi-sentence text is still caught.
The text is split on sentence-ending punctuation or newlines; the regex is
applied to each non-empty sentence individually.  True is returned if ANY
sentence matches.

The over-firing trap (R3-LEVER retraction) is avoided by requiring the keyword
to appear at the *beginning* of a sentence (not mid-sentence) or in a possessive
"you must" construction that is inherently directive regardless of position.
"""
import re

_SENTENCE_SPLIT_RE = re.compile(r"[.!?\n]+")

_IMPERATIVE_RE = re.compile(
    r"""
    (?:
        # Bare leading keywords at the very START of the sentence (after optional whitespace).
        # These signal "I am commanding you …" when they open the sentence.
        ^\s*(?:MUST|NEVER|ALWAYS|SHALL)\b
        |
        # "DO NOT" / "DON'T" at the start — "Do not install …", "Don't bind …"
        ^\s*(?:DO\s+NOT|DON'T)\b
        |
        # "RULE:" — an explicit rule label at the very start.
        ^\s*RULE\s*:
        |
        # "you must" / "You MUST" etc. — possessive imperative, position-independent.
        # "The operator must …" does NOT match because it lacks the "you" subject.
        \byou\s+must\b
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)


def is_imperative_canonical(text: str) -> bool:
    """Return True iff *text* contains imperative standing-order phrasing.

    Canonical tier stores declarative facts.  An imperative standing order is
    inappropriate and will be rejected with HTTP 422 at the write path.

    The check is performed PER-SENTENCE: the text is split on [.!?\\n]+ and
    the anchored regex is applied to each sentence individually.  This ensures
    that an imperative in a later sentence (e.g. "Fact one.\\nNEVER do X.") is
    caught even though the overall text does not start with an imperative.

    Examples that return True (fire):
        "ALWAYS offload everything"
        "You MUST use X for all calls"
        "NEVER bind port 80"
        "DO NOT redeploy without approval"
        "RULE: do Y"
        "Ollama is decommissioned.\\nNEVER re-register it."
        "It is retired. Always re-register it."

    Examples that return False (pass — declarative facts):
        "The reserved ports are 80, 443, 3000, 5000, 8000, 6443."
        "Ollama on :11434 is decommissioned (v0.22, 2026-06); …"
        "Postiz is a retired, forbidden social scheduler …"
        "The local-offload harness handles … over-long inputs defer."
        "The operator must provide a reason when promoting …"  # 'must' mid-sentence
        "Port 80 is reserved for HTTP; do not bind it without approval."
    """
    for sentence in _SENTENCE_SPLIT_RE.split(text):
        sentence = sentence.strip()
        if sentence and _IMPERATIVE_RE.search(sentence):
            return True
    return False
