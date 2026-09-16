# Error reference

Every error response from this service uses one envelope:

```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "Request validation failed",
    "details": [{ "field": "score", "issue": "greater_than_equal", "value": -50 }],
    "request_id": "01JD8FQK3M2P9VX7ABCDEF1234",
    "documentation_url": "https://.../docs/errors.md#validation_error"
  }
}
```

**Branch on `code`, never on `message`.** `code` is a stable contract;
`message` is human-facing and may be reworded at any time.

`details` lists *every* problem found, not just the first, so a client can fix
them all in one round trip. `request_id` matches the `X-Request-ID` response
header and appears on every log line for that request — quote it when
reporting a problem.

| Code | HTTP | Meaning |
|---|---|---|
| [`MALFORMED_JSON`](#malformed_json) | 400 | Body is not parseable JSON |
| [`VALIDATION_ERROR`](#validation_error) | 422 | Valid JSON, invalid contents |
| [`UNAUTHORIZED`](#unauthorized) | 401 | Missing or wrong API key |
| [`NOT_FOUND`](#not_found) | 404 | No such endpoint |
| [`METHOD_NOT_ALLOWED`](#method_not_allowed) | 405 | Wrong HTTP method for the path |
| [`GAME_NOT_FOUND`](#game_not_found) | 404 | The game slug is not registered |
| [`USER_NOT_RANKED`](#user_not_ranked) | 404 | The user has no score on that board |
| [`GAME_INACTIVE`](#game_inactive) | 409 | The game exists but is retired |
| [`PAYLOAD_TOO_LARGE`](#payload_too_large) | 413 | Body over 8 KiB |
| [`DEPENDENCY_UNAVAILABLE`](#dependency_unavailable) | 503 | A required backing store is unreachable |
| [`INTERNAL_ERROR`](#internal_error) | 500 | Unhandled fault — should be rare |

---

## `MALFORMED_JSON`

**400.** The request body could not be parsed as JSON.

Deliberately distinct from `VALIDATION_ERROR`: this means the serialiser
producing the request is broken, which is a different fix from a field being
wrong.

## `VALIDATION_ERROR`

**422.** The body parsed but failed the schema.

Unknown fields are **rejected**, not ignored. Silently dropping a misspelled
field is how a client ships `user_ID` to production and loses scores for a
week. `achieved_at` is rejected specifically: timestamps are server-assigned,
because a client-controlled clock would let a caller backdate a score and win
every tie-break.

```json
{ "error": { "code": "VALIDATION_ERROR", "message": "Request validation failed",
  "details": [
    { "field": "user_id", "issue": "too_short", "value": "" },
    { "field": "score", "issue": "greater_than_equal", "value": -50 },
    { "field": "achieved_at", "issue": "extra_forbidden", "value": "2020-01-01T00:00:00Z" }
  ], "request_id": "01JD8..." } }
```

Out-of-range query parameters are rejected rather than clamped: silently
returning 100 rows for `limit=10000` teaches a client that its request worked.

## `UNAUTHORIZED`

**401.** `X-API-Key` is missing or does not match, on `POST /v1/scores`; or
`X-Admin-Key` on the admin route. Checked before any validation or database
work. Read endpoints are public — a leaderboard is public information, and
requiring auth to read one would be theatre.

## `NOT_FOUND`

**404.** No endpoint matches the path.

Distinct from `GAME_NOT_FOUND` on purpose: telling a client with a typo'd URL
that its *game* does not exist sends it debugging the wrong thing.

## `METHOD_NOT_ALLOWED`

**405.** The path exists but not for that method.

## `GAME_NOT_FOUND`

**404.** The `game_id` is not in the game registry. Games are registered
explicitly so an unknown slug is an error rather than silently creating an
empty leaderboard. `GET /v1/games` lists valid slugs.

## `USER_NOT_RANKED`

**404.** The game is fine; this user has no score on the requested board.

Shares its status with `GAME_NOT_FOUND` but not its code, because the
remedies differ entirely: one means "check your slug", the other "this player
has not scored in this period yet". Note that a user can be ranked on the
all-time board and unranked on today's daily board.

## `GAME_INACTIVE`

**409.** The game is registered but `is_active = false`, so it accepts no new
scores. Existing boards remain readable.

This is the *only* 409 the service emits. There is no duplicate-submission
conflict by construction: submitting a score is an idempotent upsert, so a
replayed submission is a no-op that returns `improved: false` rather than an
error.

## `PAYLOAD_TOO_LARGE`

**413.** The body exceeds 8 KiB. Checked from `Content-Length` before routing,
so an oversized body is never buffered.

## `DEPENDENCY_UNAVAILABLE`

**503.** Includes a `Retry-After` header. Retry with backoff.

Raised when Postgres is unreachable, or when Redis is unreachable *and* the
Postgres fallback cannot serve the request either. Redis being down on its own
is **not** an error: reads transparently fall back to Postgres (the response
reports `"source": "postgres"`) and writes still commit durably, with the rank
index catching up afterwards.

## `INTERNAL_ERROR`

**500.** An unhandled fault. The body carries only the `request_id` — no stack
trace, no SQL, no dependency names, since anything more is free reconnaissance.
The full trace is in our logs against that id, so please quote it.
