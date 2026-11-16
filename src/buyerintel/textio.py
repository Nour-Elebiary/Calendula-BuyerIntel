"""Tolerant file reading: 'take ANY csv' (owner request 2026-11-16).

Encoding ladder utf-8-sig -> cp1252 -> latin-1 picks the first decode that
succeeds (cp1252 is the Windows default and the usual source of crashes like
"'utf-8' codec can't decode byte 0x92" - that byte is a right single quote in
cp1252); latin-1 accepts every byte sequence, so the ladder never dead-ends.
Delimiters are sniffed from the first non-empty line (European exports often
use ';' or tab, not ',').
"""
from __future__ import annotations

import csv
from pathlib import Path

ENCODING_LADDER = ("utf-8-sig", "cp1252", "latin-1")


def read_text_any(path: Path) -> str:
    """Decode a text file through the encoding ladder; never gives up early."""
    last_error: Exception | None = None
    for encoding in ENCODING_LADDER:
        try:
            return path.read_text(encoding=encoding)
        except (UnicodeDecodeError, UnicodeError) as exc:
            last_error = exc
    raise ValueError(f"cannot decode {path.name}: {last_error}")


def sniff_delimiter(sample: str) -> str:
    """Most frequent candidate delimiter in the sample line(s); default ','."""
    counts = {d: sample.count(d) for d in (";", ",", "\t", "|")}
    best = max(counts, key=lambda d: counts[d])
    return best if counts[best] else ","


def read_csv_dicts(path: Path) -> list[dict]:
    """Any CSV -> list of {header: value} rows.

    Tolerant: encoding ladder, delimiter sniffing, blank-row skip. Rows with
    extra/missing cells are kept as-is (callers map by header alias).
    """
    text = read_text_any(path)
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return []
    delimiter = sniff_delimiter(lines[0])
    reader = csv.DictReader(lines, delimiter=delimiter)
    return [
        {k: v for k, v in row.items() if k is not None and str(v or "").strip()}
        for row in reader
        if any(str(v or "").strip() for v in row.values())
    ]
