"""Product catalog seeding (company profile doc, section 5).

Idempotent upsert of the 42-product range by name; aliases go into
``product_name`` (kind=synonym). Re-running refreshes latin names, parts and
forms without duplicating rows.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from buyerintel.config import load_yaml
from buyerintel.db.models import Product, ProductName


@dataclass
class SeedStats:
    created: int = 0
    updated: int = 0
    aliases: int = 0
    misspellings: int = 0
    hs_candidates: int = 0
    skipped: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"products created={self.created} updated={self.updated} "
            f"aliases={self.aliases} misspellings={self.misspellings} "
            f"hs_candidates={self.hs_candidates}"
        )


def _name_kind_for(product_id: int, name: str, kind: str, language: str) -> ProductName:
    return ProductName(product_id=product_id, language=language, kind=kind, name=name)


def _ensure_name(
    session: Session, product_id: int, name: str, kind: str, language: str, stats: SeedStats
) -> None:
    name = name.strip().lower()
    if not name:
        return
    existing = session.scalar(
        select(ProductName).where(
            ProductName.product_id == product_id,
            ProductName.name == name,
            ProductName.kind == kind,
        )
    )
    if existing is None:
        session.add(_name_kind_for(product_id, name, kind, language))
        if kind == "misspelling":
            stats.misspellings += 1
        elif kind == "synonym":
            stats.aliases += 1


def seed_products(session: Session, *, source: str = "products.yaml") -> SeedStats:
    """Upsert every product from config/products.yaml (idempotent by name)."""
    session.info["audit_source"] = f"seed:{source}"
    data = load_yaml("products.yaml")
    stats = SeedStats()
    for entry in data.get("products", []):
        name = str(entry["name"]).strip().lower()
        if not name:
            stats.skipped.append(str(entry))
            continue
        product = session.scalar(select(Product).where(Product.name == name))
        created = product is None
        if created:
            product = Product(name=name)
            session.add(product)
        product.latin_name = entry.get("latin_name")
        product.part = entry.get("part")
        product.forms = entry.get("forms")
        product.conventional = True
        product.organic = True  # every catalog product has a 100% organic variant
        session.flush()
        if created:
            stats.created += 1
        else:
            stats.updated += 1

        # Ontology names: common (the catalog name), latin, synonyms, misspellings.
        _ensure_name(session, product.id, name, "common", "en", stats)
        if entry.get("latin_name"):
            _ensure_name(session, product.id, entry["latin_name"].strip().lower(), "latin", "la", stats)
        for alias in entry.get("aliases", []):
            _ensure_name(session, product.id, str(alias), "synonym", "en", stats)
        for misspelling in entry.get("misspellings", []):
            _ensure_name(session, product.id, str(misspelling), "misspelling", "en", stats)
    session.flush()
    return stats


def seed_hs(session: Session, *, source: str = "hs_codes.yaml") -> SeedStats:
    """Upsert HS candidates from config/hs_codes.yaml (idempotent per product+code)."""
    from buyerintel.db.models import HsCandidate

    session.info["audit_source"] = f"seed:{source}"
    data = load_yaml("hs_codes.yaml")
    stats = SeedStats()
    for product_name, entries in data.get("codes", {}).items():
        product = session.scalar(select(Product).where(Product.name == str(product_name).lower()))
        if product is None:
            stats.skipped.append(product_name)  # seeded after products, never before
            continue
        for entry in entries:
            code = str(entry["code"])
            existing = session.scalar(
                select(HsCandidate).where(
                    HsCandidate.product_id == product.id,
                    HsCandidate.hs_code == code,
                )
            )
            confidence = float(entry.get("confidence", 0.0))
            is_primary = bool(entry.get("primary", False))
            if existing is None:
                session.add(
                    HsCandidate(
                        product_id=product.id,
                        hs_code=code,
                        confidence=confidence,
                        is_primary=is_primary,
                    )
                )
                stats.hs_candidates += 1
            else:
                existing.confidence = confidence
                existing.is_primary = is_primary
    session.flush()
    return stats
