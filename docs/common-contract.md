# Native CONNECT SDK contract

Contracts `59b3b4654511eb01b6d0c350fff8b7e22bbb0268` is the v1 authority.
Rust `3b8d64fd692e3b2edaebf4233ec25bf15bcc53d6` supplies reference behavior
and synthetic TLS fixtures. Gateway integration uses source
`4e026bcd34864e9c6aca4932ccad5e8c17f0496f`.

Each SDK takes an Endpoint, optional existing InstanceToken, and TLS trust
roots. Endpoint IDs follow `ep-[0-7][0-9a-hjkmnp-tv-z]{25}`. Canonical hosts
append `.db.service.internal.tiana.com`. DNS case is normalized. The SNI host
equals the CONNECT authority host; logical port is 443. A physical dial
override changes only the network destination, preserving verification.

The transport is TLS 1.3, ALPN `h2`, regular HTTP/2 CONNECT, with no scheme,
path, or extended-CONNECT protocol pseudo-header. Request headers contain
`tiana-tunnel-version: 1`, a caller-supplied routing profile, a fresh `req-` ID,
optional User-Agent, and optional `proxy-authorization: Bearer <token>`.
Credentials are opaque and forwarded unchanged. The SDK accepts 1–4096 visible
ASCII bytes, rejecting whitespace/control characters without interpreting prefix,
version, exact length, encoding or scope. This supersedes the original fixed
InstanceToken validation; frozen fixture provenance is unchanged. Absent credentials
omit the header. Credential headers are never-indexed and request HPACK
dynamic table size is zero.

Successful CONNECT requires exactly the status, tunnel-version, auth-mode
(`TOKEN_REQUIRED` or `DISABLED`), and matching request-id headers. SDKs send
no DATA before complete success headers. Server-first bytes are preserved.
Profiles identify opaque application bytes. The caller supplies a bounded
identifier; application-level adapters own the meaning and payload encoding.

Reads and writes are ordered, independent, and subject to backpressure.
Local write EOF sends END_STREAM. Remote END_STREAM ends reads while writes
remain possible. Full close or cancellation releases the owned stream and
resources. Python task cancellation raises CancelledError, Node supports
AbortSignal, and Go uses context. Closing one tunnel preserves other tunnels.

Failures distinguish configuration, TCP, TLS, HTTP/2, response validation,
timeouts, cancellation, Gateway refusal, and established-stream I/O. Public
exceptions contain static diagnostics and bounded metadata, never remote
debug data, response bodies, inner payloads, or credentials. Once 200 has
been observed, an I/O failure has an unknown application outcome.

Gateway refusal metadata includes status, recognized code, and a retry hint
of 1 through 60000 milliseconds. Retryability requires both a valid hint and
one of: 429/CONNECTION_LIMIT, 503/POLICY_UNAVAILABLE,
503/INSTANCE_UNAVAILABLE, 504/ACTIVATION_TIMEOUT. SDKs never automatically
retry or replay, either before or after acceptance.

Each language owns installation and runnable examples, wire and lifecycle
tests, and consumer smoke evidence. Integration reports identify real
Gateway components and synthetic policy/upstream components separately.
