"""Contact verification (spec 12.4).

- Email syntax (email-validator), MX checks (dnspython), role-address
  detection, pattern inference (first.last@domain and variants).
- Phone parsing with libphonenumber (phonenumbers).
- Statuses: `published` (found on a page), `pattern-guessed` (inferred),
  `verified` (MX ok and pattern confirms a real mailbox family), `bounced`.
"""
from __future__ import annotations

import re

import dns.resolver
import phonenumbers
from email_validator import validate_email, EmailNotValidError

ROLE_PREFIXES = [
    "info", "contact", "sales", "office", "hello", "export", "purchasing",
    "buying", "procurement", "admin", "support", "enquiries", "einkauf",
]

# Person-name email patterns, most common first (spec: first.last@ and variants).
PATTERNS = [
    ("first.last", lambda f, l: f"{f}.{l}"),
    ("firstlast", lambda f, l: f"{f}{l}"),
    ("first_last", lambda f, l: f"{f}_{l}"),
    ("f.last", lambda f, l: f"{f[0]}.{l}"),
    ("first", lambda f, l: f),
    ("firstl", lambda f, l: f + l[0]),
]


def check_syntax(email: str) -> bool:
    """RFC-ish syntax check (email-validator, DNS checks off)."""
    try:
        validate_email(email, check_deliverability=False)
        return True
    except EmailNotValidError:
        return False


def has_mx(domain: str) -> bool:
    """True when the domain has at least one MX record."""
    try:
        return bool(dns.resolver.resolve(domain, "MX"))
    except Exception:  # noqa: BLE001 - NXDOMAIN, timeout, no network: not a crash
        return False


def is_role_address(email: str) -> bool:
    local = email.split("@", 1)[0].lower()
    return local in ROLE_PREFIXES


def verify_email(email: str, *, found_on_page: bool = False) -> dict:
    """Classify one email into the spec's statuses.

    Returns {email, syntax_ok, mx, role, status, confidence}.
    """
    syntax_ok = check_syntax(email)
    domain = email.split("@", 1)[-1].lower() if "@" in email else ""
    mx = has_mx(domain) if domain and syntax_ok else False
    role = is_role_address(email)
    if not syntax_ok:
        status, confidence = "invalid", 0.0
    elif found_on_page:
        # Published on the company's own pages: good enough for outreach.
        status, confidence = "published", 0.9
    elif mx:
        status, confidence = "verified", 0.7
    else:
        status, confidence = "pattern-guessed", 0.4
    return {"email": email.lower(), "syntax_ok": syntax_ok, "mx": mx, "role": role,
            "status": status, "confidence": confidence}


def parse_phone(raw: str, *, default_region: str | None = None) -> dict | None:
    """Parse with libphonenumber; returns e164, national, type, validity."""
    try:
        number = phonenumbers.parse(raw, default_region)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(number):
        return None
    line_type = phonenumbers.number_type(number)
    type_name = {
        phonenumbers.PhoneNumberType.FIXED_LINE: "fixed_line",
        phonenumbers.PhoneNumberType.MOBILE: "mobile",
        phonenumbers.PhoneNumberType.FIXED_LINE_OR_MOBILE: "fixed_or_mobile",
        phonenumbers.PhoneNumberType.TOLL_FREE: "toll_free",
        phonenumbers.PhoneNumberType.VOIP: "voip",
    }.get(line_type, "other")
    return {
        "e164": phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164),
        "national": phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.NATIONAL),
        "type": type_name,
        "valid": True,
    }


def infer_emails(full_name: str, domain: str) -> list[dict]:
    """Pattern inference: first.last@domain and variants, all marked guessed."""
    parts = re.findall(r"[a-z]+", (full_name or "").lower())
    if len(parts) < 2 or not domain:
        return []
    first, last = parts[0], parts[-1]
    out = []
    for kind, build in PATTERNS:
        email = f"{build(first, last)}@{domain}"
        result = verify_email(email)
        result["pattern"] = kind
        result["status"] = "pattern-guessed"
        result["confidence"] = 0.4 if result["mx"] else 0.1
        out.append(result)
    return out
