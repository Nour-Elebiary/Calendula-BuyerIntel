"""Shipment data package (M7, spec section 15).

- base:       the ShipmentSource interface + idempotent shipment upserts
- us_bol:     US public bill-of-lading connector (ImportYeti public pages,
              no login, polite pacing, degrades gracefully; CSV fallback)
- csv_import: generic CSV importer with the column-mapping YAML

The user may later add sources for other countries; they plug in through
ShipmentSource without redesign (spec section 15). The addendum's Tor-based
ImportGenius scraping stays an OPEN SLOT - Tor egress is banned (DECISIONS
2026-11-14) and that slot is only filled if the user supplies a compliant
source.
"""
