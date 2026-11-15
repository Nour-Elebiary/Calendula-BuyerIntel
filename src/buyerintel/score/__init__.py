"""BuyerIntel scoring package (M5).

Modules:
- matching: product matching over the ontology (spec section 10)
- personas: persona classification + similarity (config/personas.yaml v2)
- scoring:  weighted component score, tier assignment, ScoreRun persistence
- rules:    ordered rule-chain state machine (spec section 14)
- autotune: logistic-regression weight tuning, dormant until 200 labelled leads
"""
