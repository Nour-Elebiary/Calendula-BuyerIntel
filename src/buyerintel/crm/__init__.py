"""Calendula CRM layer (spec section 16, M8).

The system LOGS and RECOMMENDS - it never sends messages:
- outreach:  contact log + the 'never contacted' / 'last reached out'
             filters + the per-company next-action engine
- channels:  per-country channel priors (config/channels.yaml), re-learned
             from observed reply rates
- quotes:    quote tracker (Calendula's own prices are the user's call; the
             trade-data unit value appears only as a reference benchmark)
- samples:   sample offers with the FIXED terms text (spec 16)
- content:   trending-herb drafts (B2B framing, image brief, nothing is
             ever posted automatically) + the send/skip suggestion engine
"""
