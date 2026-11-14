"""Name normalization utilities (used by the importer now, entity resolution later).

Design checklist per ADD-006 (awesome-falsehood): company names are not two words,
not ASCII, change over time, and have many legal forms. Dots are removed FIRST so
dotted legal suffixes collapse into their dotless form (B.V. -> bv, S.A. -> sa,
o.o. -> oo) before suffix stripping. Transliteration comes with ICU later.
"""
from __future__ import annotations

import re
import unicodedata

# Common legal / trade suffixes across registries (dotless forms; expand as needed).
LEGAL_SUFFIXES = [
    "gmbh",
    "co",
    "kg",
    "ag",
    "ltd",
    "limited",
    "llc",
    "inc",
    "incorporated",
    "corp",
    "corporation",
    "srl",
    "spa",
    "bv",
    "nv",
    "sa",
    "sas",
    "sarl",
    "cv",
    "oy",
    "ab",
    "as",
    "plc",
    "pte",
    "pty",
    "ug",
    "doo",
    "sro",
    "kk",
    "sl",
    "eirl",
    "sapi",
    "wll",
    "jsc",
    "oao",
    "ooo",
    "zrt",
    "kft",
    "sp",
    "z",
    "oo",
    "yugengaisha",
    # Cyrillic registry forms (ООО, ОАО, ЗАО, АО, ООО) in Cyrillic script
    "ооо",
    "оао",
    "зао",
    "ао",
    "пао",
]

# Connector words that may sit between suffixes (e.g. "S.A. de C.V.", "Sp. z o.o.").
CONNECTORS = {"de", "del", "da", "do", "und", "and", "of", "y", "e", "di", "d", "in", "the"}

PUNCTUATION = set(".,;:!?()[]{}'\"“”‘’-–—/&")

# A trailing "(connector) suffix" chunk, matched repeatedly from the end.
_suffix_alt = "|".join(re.escape(s) for s in sorted(LEGAL_SUFFIXES, key=len, reverse=True))
_connector_alt = "|".join(re.escape(c) for c in sorted(CONNECTORS, key=len, reverse=True))
_SUFFIX_RE = re.compile(
    rf"(?:\s+(?:{_connector_alt}))?\s+(?:{_suffix_alt})$", re.IGNORECASE
)


def normalize_name(name: str) -> str:
    """Normalize a company name for dedupe matching.

    Steps: NFKC unicode normalization, casefold, drop dots (so dotted suffixes
    collapse), strip punctuation (keeping word boundaries), collapse whitespace,
    then repeatedly strip trailing legal-suffix chunks.
    """
    if not name:
        return ""
    text = unicodedata.normalize("NFKC", name)
    text = text.casefold()
    text = text.replace(".", "")  # dotted suffixes collapse into dotless tokens
    text = "".join(ch if ch not in PUNCTUATION else " " for ch in text)
    tokens = [t for t in text.split() if t]
    while tokens:
        candidate = " ".join(tokens)
        match = _SUFFIX_RE.search(candidate)
        if not match:
            break
        tokens = candidate[: match.start()].split()
    return " ".join(tokens)


def normalize_domain(domain: str | None) -> str | None:
    """Lowercase, strip scheme, www prefix, paths and trailing dot."""
    if not domain:
        return None
    d = domain.strip().lower()
    for prefix in ("https://", "http://"):
        if d.startswith(prefix):
            d = d[len(prefix) :]
    d = d.split("/", 1)[0].split("?", 1)[0].rstrip(".")
    if d.startswith("www."):
        d = d[4:]
    return d or None
