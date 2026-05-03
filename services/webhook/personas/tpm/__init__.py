"""TPM persona — Definition-of-Ready PR check.

Ports scripts/tpm.py logic. v1 ships the 5 static checks; Poolside LLM
review is wired in poolside_client.py and called from evaluate().

Conventions sourced from mattpocock/skills via the conventions adapter
(future). v1 hardcodes the rules (matches current tpm.py); SHA-bumpable
adapter lands in v1.5+ per PRD addendum.
"""
