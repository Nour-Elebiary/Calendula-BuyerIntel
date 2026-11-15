"""ITC Trade Map CSV import tests (preamble, delimiter sniffing, aliases)."""
from __future__ import annotations

import textwrap

import pytest
from sqlalchemy import select

from buyerintel.db.models import TradeFlow
from buyerintel.market.trademap import (
    import_trademap_csv,
    skip_preamble,
    sniff_delimiter,
)

SAMPLE = textwrap.dedent(
    """\
    ITC Trade Map - List of importing markets for a product exported by Egypt
    Product: 0909 Spices
    Export value in 2024: USD 12,345 thousand
    Reporter;Partner;Code;Year;Trade value (USD) thousand;Netweight (kg)
    Egypt;Germany;090961;2024;1200;500
    Egypt;Netherlands;090961;2024;800;300
    """
)


def test_sniff_delimiter_semicolon():
    assert sniff_delimiter("Reporter;Partner;Code") == ";"
    assert sniff_delimiter("a,b,c") == ","
    assert sniff_delimiter("a\tb\tc") == "\t"


def test_skip_preamble_finds_header():
    lines = SAMPLE.splitlines()
    assert lines[skip_preamble(lines)].startswith("Reporter")


def test_import_trademap_csv_upserts(session, tmp_path):
    path = tmp_path / "trademap.csv"
    path.write_text(SAMPLE, encoding="utf-8-sig")
    stats = import_trademap_csv(session, path)
    session.commit()

    assert stats.rows_read == 2
    assert stats.flows_created == 2
    assert stats.skipped == []
    flows = session.scalars(select(TradeFlow).order_by(TradeFlow.partner)).all()
    assert len(flows) == 2
    deu = next(f for f in flows if f.partner == "Germany")
    assert deu.hs == "090961"
    assert deu.period == "2024"
    assert deu.value_usd == 1200.0
    assert deu.qty == 500.0
    assert deu.unit_value == pytest.approx(2.4)


def test_import_trademap_csv_is_idempotent(session, tmp_path):
    path = tmp_path / "trademap.csv"
    path.write_text(SAMPLE, encoding="utf-8-sig")
    first = import_trademap_csv(session, path)
    session.commit()
    second = import_trademap_csv(session, path)
    session.commit()
    assert first.flows_created == 2
    assert second.flows_created == 0


def test_import_trademap_skips_rows_without_hs(session, tmp_path):
    content = "Reporter,Partner,Code,Year,Trade value (USD) thousand\nEgypt,Germany,,2024,100\n"
    path = tmp_path / "bad.csv"
    path.write_text(content, encoding="utf-8")
    stats = import_trademap_csv(session, path)
    assert stats.flows_created == 0
    assert len(stats.skipped) == 1
