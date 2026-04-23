"""Extracted helper package. Invariants:
- Every module here is a pure leaf — no imports from the monolith.
- Adding a module here does not change runtime behavior; the monolith
  imports from here and wires it in as the existing call sites did.
- This package will eventually be renamed to stash_jellyfin_proxy and
  replace the single-file monolith (plan §4.6 Phase 0.6 completion).
"""
