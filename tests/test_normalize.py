from __future__ import annotations

import pytest

from buyerintel.resolve.normalize import normalize_domain, normalize_name


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Test Importer GmbH", "test importer"),
        ("Acme Trading Co., Ltd.", "acme trading"),
        ("ACME  Trading   Company", "acme trading"),  # "company" is a legal suffix
        ("Müller & Sons B.V.", "müller sons"),  # & stripped, unicode kept
        ("Zenith Foods S.A. de C.V.", "zenith foods"),
        ("", ""),
        ("Купец ООО", "купец"),  # cyrillic ok
        ("株式会社山田", "株式会社山田"),  # CJK kept (no suffix list for it yet)
    ],
)
def test_normalize_name(raw, expected):
    assert normalize_name(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://www.Example.com/about", "example.com"),
        ("www.example.com.", "example.com"),
        ("example.com/path?q=1", "example.com"),
        ("HTTP://Example.COM", "example.com"),
        ("", None),
        (None, None),
    ],
)
def test_normalize_domain(raw, expected):
    assert normalize_domain(raw) == expected
