"""Market intelligence module (spec section 15, milestone M2).

Connectors: UN Comtrade, Eurostat Comext, ITC Trade Map CSV import, and the
signal collectors (Google Trends via pytrends, PubMed E-utilities, Frankfurter
FX). All free, all polite, all routed through the egress policy in
``config/network.yaml`` — direct by default, never a Tor exit node.
"""
