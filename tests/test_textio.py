"""Tolerant file reading: 'take ANY csv' (owner request 2026-11-16).

The live failure was `ingest failed: 'utf-8' codec can't decode byte 0x92`
- 0x92 is a cp1252 right single quote, so the ladder must fall back.
"""
from __future__ import annotations

from pathlib import Path

from buyerintel.textio import read_csv_dicts, read_text_any, sniff_delimiter


def _tmp(tmp_path: Path, name: str, data: bytes) -> Path:
    p = tmp_path / name
    p.write_bytes(data)
    return p


def test_read_text_any_cp1252_smart_quote(tmp_path):
    # 0x92 = right single quote in cp1252 - the exact live failure
    p = _tmp(tmp_path, "win.csv",
             b"Name,Note\r\nKr\xe4uter Klang,'owner\x92s choice'\r\n")
    text = read_text_any(p)
    assert "Kräuter Klang" in text
    assert "owner’s choice" in text


def test_read_text_any_utf8_bom(tmp_path):
    p = _tmp(tmp_path, "bom.csv",
             "Name,City\nACME,Kairo\n".encode("utf-8-sig"))
    assert read_text_any(p).startswith("Name,City")


def test_read_text_any_latin1_always_decodes(tmp_path):
    p = _tmp(tmp_path, "odd.csv", b"\x93mixed\x94 garbage \xe9 bytes\r\n")
    assert read_text_any(p) is not None   # latin-1 accepts every byte


def test_sniff_delimiter_variants():
    assert sniff_delimiter("Name;City;Country") == ";"
    assert sniff_delimiter("Name,City,Country") == ","
    assert sniff_delimiter("Name\tCity\tCountry") == "\t"
    assert sniff_delimiter("Name|City") == "|"
    assert sniff_delimiter("single column") == ","   # default


def test_read_csv_dicts_semicolon_european(tmp_path):
    p = _tmp(tmp_path, "eu.csv",
             "Company Name;Contact;Country\n"
             "Kräuter Klang GmbH;info@klang.de;DEU\n"
             "Tea Traders Ltd;;GBR\n".encode("cp1252"))
    rows = read_csv_dicts(p)
    assert len(rows) == 2
    assert rows[0]["Company Name"] == "Kräuter Klang GmbH"
    assert rows[0]["Contact"] == "info@klang.de"
    assert rows[1].get("Contact", "") == ""   # empty cell dropped


def test_read_csv_dicts_skips_blank_rows(tmp_path):
    p = _tmp(tmp_path, "blanks.csv", b"A,B\r\n1,2\r\n\r\n3,4\r\n")
    assert len(read_csv_dicts(p)) == 2


def test_read_csv_dicts_empty_file(tmp_path):
    p = _tmp(tmp_path, "empty.csv", b"")
    assert read_csv_dicts(p) == []


# ------------------------------------------------------- importer integration
def test_importer_read_rows_csv_any_encoding(tmp_path):
    """The actual failure path: importer.read_rows on a cp1252 CSV."""
    from buyerintel.connectors.importer import read_rows

    p = _tmp(tmp_path, "leads.csv",
             "Name;Domain;Country\r\n"
             "Kräuter Klang GmbH;kraeuter-klang.de;DEU\r\n"
             "Café 'Müller' Ltd;muller.co.uk;GBR\r\n".encode("cp1252"))
    rows = read_rows(p)
    assert rows[0]["Name"] == "Kräuter Klang GmbH"
    assert rows[1]["Name"] == "Café 'Müller' Ltd"
