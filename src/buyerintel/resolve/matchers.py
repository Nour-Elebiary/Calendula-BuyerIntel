"""Match keys and similarity signals for entity resolution (spec section 11).

Match keys in priority order: normalized domain, registry / VAT / LEI, phone,
social profile URLs, normalized name, then address. Blocking uses (country,
first name token) and a phonetic key so matching scales.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

import phonenumbers
from rapidfuzz import fuzz

from buyerintel.db.models import Company, ContactPoint
from buyerintel.resolve.normalize import normalize_domain, normalize_name

SOCIAL_KINDS = {
    "linkedin",
    "facebook",
    "instagram",
    "tiktok",
    "wechat",
    "telegram",
    "vk",
    "line",
    "kakao",
    "zalo",
    "twitter",
    "youtube",
}
PHONE_KINDS = {"phone", "whatsapp"}


@dataclass
class MatchKeys:
    company_id: int | None = None
    domain: str | None = None
    registries: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    socials: list[str] = field(default_factory=list)
    name_key: str = ""
    country: str | None = None
    address: str | None = None

    def blocking_keys(self) -> set[str]:
        """Hashable blocking keys; two companies sharing any key become candidates."""
        keys: set[str] = set()
        if self.domain:
            keys.add(f"domain:{self.domain}")
        for r in self.registries:
            keys.add(f"registry:{r}")
        for p in self.phones:
            keys.add(f"phone:{p}")
        for s in self.socials:
            keys.add(f"social:{s}")
        if self.name_key:
            first = self.name_key.split()[0]
            if self.country:
                keys.add(f"nc:{self.country}|{first}")
            keys.add(f"phone_key:{phonetic_key(self.name_key)}")
        return keys


def normalize_registry(value: str | None) -> str | None:
    if not value:
        return None
    return re.sub(r"[\s\-]", "", str(value)).upper() or None


def normalize_phone(value: str | None, country: str | None = None) -> str | None:
    if not value:
        return None
    try:
        parsed = phonenumbers.parse(str(value), country or None)
        if phonenumbers.is_valid_number(parsed):
            return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    except phonenumbers.NumberParseException:
        pass
    return None


def normalize_social(url: str | None) -> str | None:
    if not url:
        return None
    text = str(url).strip().lower()
    for prefix in ("https://", "http://"):
        text = text.removeprefix(prefix)
    text = text.removeprefix("www.").rstrip("/").split("?", 1)[0]
    return text or None


def keys_from_company(company: Company, contacts: list[ContactPoint] | None = None) -> MatchKeys:
    contacts = contacts or []
    phones = [
        p
        for p in (
            normalize_phone(c.value, company.country)
            for c in contacts
            if c.kind in PHONE_KINDS
        )
        if p
    ]
    socials = [
        s
        for s in (normalize_social(c.value) for c in contacts if c.kind in SOCIAL_KINDS)
        if s
    ]
    return MatchKeys(
        company_id=company.id,
        domain=normalize_domain(company.domain),
        registries=[
            r
            for r in (
                normalize_registry(company.registry_id),
                normalize_registry(company.vat_id),
                normalize_registry(company.lei),
            )
            if r
        ],
        phones=phones,
        socials=socials,
        name_key=normalize_name(company.canonical_name or ""),
        country=(company.country or "").upper() or None,
        address=(company.address or "").strip() or None,
    )


def phonetic_key(name: str) -> str:
    """Rough soundex; Latin text is accent-stripped first, other scripts fall
    back to their raw first token."""
    first = name.split()[0] if name.split() else ""
    if not first:
        return ""
    text = unicodedata.normalize("NFKD", first).encode("ascii", "ignore").decode().strip()
    if not text:
        return first  # CJK / Arabic etc: no Latin skeleton, use raw token
    return _soundex(text.upper())


_SOUNDEX_MAP = str.maketrans("", "", "AEHIOUWY")
_SOUNDEX_DIGITS = str.maketrans(
    "BFPVCGJKQSXZDTLMNR",  # BFPV->1 CGJKQSXZ->2 DT->3 L->4 MN->5 R->6
    "111122222222334556",
)


def _soundex(word: str) -> str:
    if not word:
        return ""
    first = word[0]
    stripped = word[1:].translate(_SOUNDEX_MAP)
    digits = stripped.translate(_SOUNDEX_DIGITS)
    collapsed = "".join(c for i, c in enumerate(digits) if i == 0 or c != digits[i - 1])
    return (first + (collapsed + "000")[:3])[:4]


def name_similarity(a: str, b: str) -> float:
    """0..1 token-order-insensitive name similarity."""
    if not a or not b:
        return 0.0
    return fuzz.token_sort_ratio(a, b) / 100.0


def domain_similarity(a: str | None, b: str | None) -> float:
    """Compare registrable domains ignoring TLD (acmefoods.com vs acmefoods.de)."""
    if not a or not b:
        return 0.0
    strip_tld = lambda d: re.sub(r"\.[a-z]{2,}(\.[a-z]{2})?$", "", d)  # noqa: E731
    return fuzz.ratio(strip_tld(a), strip_tld(b)) / 100.0


def address_similarity(a: str | None, b: str | None) -> float:
    if not a or not b:
        return 0.0
    norm = lambda s: normalize_name(s)  # noqa: E731
    return fuzz.token_sort_ratio(norm(a), norm(b)) / 100.0
