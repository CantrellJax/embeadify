"""The bead-intake scribe (stage 1: shadow mode, one executor, CREATE as the fallback).

Producers submit candidates; a recommender proposes a typed action; a deterministic executor validates
that proposal and builds every `bd` argument array itself. See docs/scribe.md.
"""
