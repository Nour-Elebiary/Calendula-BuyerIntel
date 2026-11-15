"""Contact verification + extraction tests (spec 12.2-12.4, M4)."""
from __future__ import annotations

import pytest

from buyerintel.enrich import verify as verify_mod
from buyerintel.enrich.extract import extract_tags, regex_contacts
from buyerintel.enrich.verify import (
    check_syntax,
    infer_emails,
    is_role_address,
    parse_phone,
    verify_email,
)


@pytest.fixture(autouse=True)
def no_dns(monkeypatch):
    """MX lookups must not hit the network in tests."""
    monkeypatch.setattr(verify_mod.dns.resolver, "resolve",
                        lambda *a, **kw: ["1.2.3.4"])


def test_email_syntax():
    assert check_syntax("info@herbhandel.de")
    assert not check_syntax("not-an-email")
    assert not check_syntax("missing@tld")


def test_role_address_detection():
    assert is_role_address("info@x.com")
    assert is_role_address("purchasing@x.com")
    assert not is_role_address("john.smith@x.com")


def test_verify_email_published(monkeypatch):
    verdict = verify_email("sales@herbhandel.de", found_on_page=True)
    assert verdict["status"] == "published"
    assert verdict["confidence"] >= 0.8
    assert verdict["role"]  # sales@ is a role address, correctly flagged


def test_verify_email_mx_verified_vs_guessed(monkeypatch):
    verdict = verify_email("john.smith@herbhandel.de")  # not found on page, MX ok
    assert verdict["status"] == "verified"
    assert verdict["mx"]

    monkeypatch.setattr(verify_mod.dns.resolver, "resolve",
                        lambda *a, **kw: (_ for _ in ()).throw(Exception("NXDOMAIN")))
    verdict2 = verify_email("john.smith@no-mx-xyz123.com")
    assert verdict2["status"] == "pattern-guessed"


def test_phone_parsing():
    parsed = parse_phone("+49 40 123456", default_region="DE")
    assert parsed and parsed["e164"] == "+4940123456"
    assert parsed["valid"]
    assert parse_phone("123") is None          # too short / invalid
    assert parse_phone("gibberish") is None


def test_pattern_inference_variants(monkeypatch):
    guesses = infer_emails("John Smith", "herbhandel.de")
    values = [g["email"] for g in guesses]
    assert "john.smith@herbhandel.de" in values
    assert "johnsmith@herbhandel.de" in values
    assert "j.smith@herbhandel.de" in values
    assert all(g["status"] == "pattern-guessed" for g in guesses)
    # the one whose domain has MX gets the higher confidence
    best = max(guesses, key=lambda g: g["confidence"])
    assert best["confidence"] == 0.4


# ------------------------------------------------------------------ extract --
def test_extract_tags_from_text():
    text = "We are an ISO 9001 certified supplier of organic herbs and offer " \
           "private label services. Halal certified facility."
    tags = extract_tags(text)
    assert "organic" in tags
    assert "halal" in tags
    assert "private_label" in tags
    assert "iso" in tags
    assert "kosher" not in tags


def test_regex_contacts_finds_emails_and_socials():
    html = """
    <a href="mailto:info@herbhandel.de">mail</a> info@herbhandel.de
    <a href="https://www.linkedin.com/company/herbhandel-gmbh">LinkedIn</a>
    <a href="https://instagram.com/herbhandel">IG</a>
    <a href="https://www.facebook.com/herbhandel.de">FB</a>
    """
    contacts = regex_contacts(html)
    kinds = {(c["kind"], c["value"]) for c in contacts}
    assert ("email", "info@herbhandel.de") in kinds
    assert ("linkedin", "https://linkedin.com/company/herbhandel-gmbh") in kinds
    assert ("instagram", "https://instagram.com/herbhandel") in kinds
    # dedupe: info@herbhandel.de appears once
    emails = [c for c in contacts if c["kind"] == "email"]
    assert len(emails) == 1
