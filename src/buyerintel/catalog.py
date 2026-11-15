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
    skipped: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"products created={self.created} updated={self.updated} "
            f"aliases={self.aliases}"
        )


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
        for alias in entry.get("aliases", []):
            alias_name = str(alias).strip().lower()
            existing = session.scalar(
                select(ProductName).where(
                    ProductName.product_id == product.id,
                    ProductName.name == alias_name,
                    ProductName.kind == "synonym",
                )
            )
            if existing is None:
                session.add(
                    ProductName(
                        product_id=product.id, language="en", kind="synonym", name=alias_name
                    )
                )
                stats.aliases += 1
    session.flush()
    return stats
