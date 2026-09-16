"""Identifier and score rules, defined once (Spec.md §3, §5).

These constants are the single source of truth for what a valid identifier is.
They are consumed by three layers that must not be allowed to disagree:

* Pydantic request schemas, which reject bad input at the edge;
* Postgres CHECK constraints, which stop a bad row entering the table
  regardless of which code path wrote it;
* the Redis key builder, which must never emit a key it cannot parse back.

Duplicating a regex across those layers is how a value becomes acceptable to
the API and rejected by the database.
"""

from __future__ import annotations

# --------------------------------------------------------------------------- #
# Games
# --------------------------------------------------------------------------- #
# Lowercase slug, used verbatim in URLs and Redis keys. Must start
# alphanumeric so a slug can never be confused with a flag or an empty segment.
GAME_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{0,63}$"
GAME_ID_MAX_LENGTH = 64
GAME_NAME_MAX_LENGTH = 128

# --------------------------------------------------------------------------- #
# Users
# --------------------------------------------------------------------------- #
# Caller-supplied, because the requirement is "accept a user ID" — identity
# belongs to the calling game server.
#
# The charset is load-bearing, not cosmetic. These strings become Redis sorted
# set members and URL path segments, so:
#   * ':' is excluded — it is the Redis key separator, and allowing it would
#     let a user_id forge a different board's key;
#   * '/' and whitespace are excluded — they break path routing;
#   * control characters are excluded — they corrupt the log stream.
USER_ID_PATTERN = r"^[A-Za-z0-9_.\-]{1,64}$"
USER_ID_MAX_LENGTH = 64
DISPLAY_NAME_MAX_LENGTH = 64

# --------------------------------------------------------------------------- #
# Scores
# --------------------------------------------------------------------------- #
MIN_SCORE = 0

# 1e12. Chosen to sit far below 2**53 (~9.007e15), the largest integer a
# float64 represents exactly — and a Redis sorted set score *is* a float64.
# Beyond that boundary a score would be silently rounded on its way into the
# rank index, so two different scores could compare equal in Redis while
# differing in Postgres. The bound makes the guarantee explicit rather than
# incidental, with ~4 orders of magnitude of headroom.
MAX_SCORE = 1_000_000_000_000

# The float64 exact-integer limit, kept here so the test asserting our headroom
# reads as an intentional relationship rather than a magic number.
FLOAT64_EXACT_INTEGER_LIMIT = 2**53
