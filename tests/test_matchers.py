from __future__ import annotations

from buyerintel.resolve.matchers import (
    MatchKeys,
    domain_similarity,
    normalize_phone,
    normalize_registry,
    normalize_social,
    phonetic_key,
)


def test_registry_normalization_strips_spaces_and_case():
    assert normalize_registry("HRB 12345") == "HRB12345"
    assert normalize_registry("hrb-12345") == "HRB12345"


def test_phone_normalization_to_e164():
    assert normalize_phone("+20 100 123 4567", "EG") == "+201001234567"
    assert normalize_phone("+61 2 8123 4567", "AU") == "+61281234567"
    assert normalize_phone("not-a-phone") is None


def test_social_normalization():
    assert normalize_social("https://www.linkedin.com/company/acme/") == "linkedin.com/company/acme"
    assert normalize_social(None) is None


def test_blocking_keys_include_country_token_and_phonetic():
    mk = MatchKeys(
        company_id=1,
        domain="acme.com",
        registries=["HRB1"],
        phones=["+15551234567"],
        socials=["linkedin.com/company/acme"],
        name_key="acme foods",
        country="US",
    )
    keys = mk.blocking_keys()
    assert "domain:acme.com" in keys
    assert "registry:HRB1" in keys
    assert "nc:US|acme" in keys
    assert any(k.startswith("phone_key:") for k in keys)


def test_phonetic_key_matches_similar_first_names():
    assert phonetic_key("müller") == phonetic_key("mueller")
    assert phonetic_key("acme") != phonetic_key("zentrum")


def test_domain_similarity_ignores_tld():
    assert domain_similarity("acmefoods.com", "acmefoods.de") == 1.0
    assert domain_similarity("acmefoods.com", "totallyother.net") < 0.85
    assert domain_similarity(None, "acme.com") == 0.0
