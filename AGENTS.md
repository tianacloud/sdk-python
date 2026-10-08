# Generic Python transport boundary (2026-09-28)

The user requires this repository to provide only generic channel capabilities,
with application adapters in separate dependent packages, matching the Rust split.
Old API compatibility/migration is explicitly unnecessary.
Baseline: b6307ce3c80cad1028a81322793dd6edff35ee9e.
Do not commit, push or publish without a fresh explicit user request.

Client.connect requires an explicit application protocol string. Validate 1–64
ASCII letters, digits, dot, underscore or hyphen before I/O; forward it unchanged
in tiana-database-protocol. Gateway decides supported profiles. Do not embed
application protocol enums, default profiles, classifiers, SQL or helper logic.

Preserve existing Endpoint/SNI/authority binding, opaque credential handling,
TLS 1.3 and h2, sensitive header protection, backpressure, half-close, cancellation,
resource limits, diagnostic errors and no-replay rules. One physical connection
per tunnel remains the design; no multiplexing or credential semantic change.
Profile validation is one bounded operation before a connection, with no payload
hot-path change. No storage, durability or server wire-format change.

Examples and external Gateway smoke scripts require TIANA_PROTOCOL from their
caller; local unit peers use opaque application identifiers. Keep frozen authority
and synthetic certificate provenance intact. Existing historical Gateway validation
records are not evidence of a rerun against a currently deployed environment.
Run all native TLS/H2 tests, generic identifier injection/length/forwarding tests,
example subprocess checks, build both wheel/sdist and verify installed wheels.
No compatibility aliases or migration paths should be added.


## 2026-09-28: opaque Gateway credentials

A Gateway-accepted 48-character credential was rejected by the former fixed
47-character/base64 InstanceToken parsers. Treat credentials as opaque, as sdk-go
and current Public CONNECT/Token specifications require. Accept 1..4096 visible
ASCII bytes (0x21..0x7e); reject empty, whitespace, control, non-ASCII and oversized
input before I/O/allocation. Do not infer prefix/version/type/scope, decode base64,
trim, normalize or append padding. The maximum is a resource bound with headroom
under the existing 16 KiB header-list budget, not a token-format discriminator.
Gateway remains the authentication authority; no authentication bypass/fallback.

Preserve exact Bearer bytes, never-indexed/sensitive headers, redacted errors,
owned-buffer clearing where supported and existing no-replay/TLS guarantees.
Removing base64 parsing removes temporary decoded secrets; validation is one
bounded linear scan at configuration time, with no payload hot-path work. No
storage, transaction, persisted schema or server wire change. This deliberately
accepts additional opaque representations; rollback reinstates the old client
rejection and must not transform saved credentials. Go's broader HTTP header
value check is the opacity reference, not a claim of identical whitespace policy.

Use only synthetic credentials in committed tests. Cover old/new-length and
non-prefix tokens, exact protected header forwarding, invalid/control/oversize
input, redaction and owned-buffer cleanup. Validate installed built artifacts
and real read-only notes demos without persisting user credentials. Isolated
verification may exercise unpublished changed artifacts; production adapter
manifests remain pinned remote HTTPS and must be refreshed after authorized
publication. No commit/push is authorized by this fix alone.
