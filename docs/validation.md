# Validation

Validated source commit: `df56610e0d885286df756d8fccadcfb0bf4f1f49`.
Candidate: `tiana-sdk 0.1.0.dev0`, Python 3.11+, Apache-2.0.

Shared QA fixture repository:
`qa-native-connect`.
Shared QA fixture tag: `qa-2026-09-05`.

## Direct tests

Python 3.11.15: 18 tests passed with ResourceWarning promoted to an error.
The tests cover canonical authority and credential inputs; TLS 1.3, SNI and
ALPN; rejection of untrusted and wrong-name certificates and TLS 1.2;
HPACK never-indexed credentials and request table zero; the 200 barrier;
server-first bytes; both half-close orders; pending-connect, TLS, read and
blocked-write cancellation; independent tunnel cleanup; all 11 fixed Gateway
codes and retry hints; malformed 200 headers with unknown-outcome metadata;
RST/GOAWAY without replay; capacity/deadline failures; and dependency-log
redaction. Exact authority and synthetic TLS fixtures come from the frozen
Contracts and Rust revisions recorded in `common-contract.md`.

Command: `PYTHONPATH=src .venv/bin/python -W error::ResourceWarning -m unittest tests.test_connect -v`.
## Packaging and consumer

`python -m build --no-isolation` produced both the sdist and a wheel built
from that sdist. The build produced both an sdist and a wheel.

A separate Python 3.14.5 environment installed the wheel with h2 4.4.1,
hpack 4.2.0, and hyperframe 6.1.0. That installed package completed all six
fixed-Gateway integration cases from a working directory outside the source
tree. The source-distribution example ran with the installed interpreter:
pipe EOF and regular-file EOF both produced exact greeting/echo/tail output;
one SIGINT with stdin still open exited with status 130.

Reproduction: `tests/gateway_smoke.py <fixture-directory>` and
`tests/example_smoke.py <installed-python> <sdist-example> <fixture-directory>`.

## Fixed Gateway integration

Gateway source: `4e026bcd34864e9c6aca4932ccad5e8c17f0496f`.
Shared QA fixture repository:
`qa-native-connect`.
Shared QA fixture tag: `qa-2026-09-05`.

| Case | Result |
| --- | --- |
| Anonymous DISABLED | 2097152 bytes each direction; server greeting and EOF tail exact |
| Token TOKEN_REQUIRED | 2097152 bytes each direction; server greeting and EOF tail exact |
| Missing token | 407 AUTH_REQUIRED, retryable false |
| Wrong synthetic token | 407 ACCESS_DENIED, retryable false |
| Policy unavailable | 503 POLICY_UNAVAILABLE, bounded hint, retryable true |
| Cancel established read | CancelledError; independent tunnel completes echo and EOF tail |

The TLS/H2 ingress, CONNECT validation, authorization/commit, Route Handshake
v1 client, and byte relay are real Gateway components. Control, Runtime,
clock, and loopback Agent are synthetic. This evidence contains no database,
business credential, or cluster operation. Production Agent/gnet half-close
behavior remains the limitation recorded by the frozen v1 contract.

Python validation created no persistent server or boo session. The shared
Gateway process belongs to QA and is cleaned up by its owner.
