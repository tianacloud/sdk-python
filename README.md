# Tiana Python SDK

Native asynchronous byte streams over Tiana v1 TLS/HTTP2 CONNECT. Requires
Python 3.11 or later; uses Python TLS, asyncio, and hyper-h2. Version
`0.1.0.dev0` is an installable local development candidate.

```sh
python -m pip install .
```

```python
import asyncio
import os
from tiana_sdk import Client

async def main():
    async with Client(
        os.environ["TIANA_ENDPOINT"],
        token=os.environ.get("TIANA_TOKEN"),
        ca_file=os.environ.get("TIANA_CA_FILE"),
    ) as client:
        async with await client.connect(os.environ["TIANA_PROTOCOL"]) as tunnel:
            await tunnel.write(b"application payload")
            print(await tunnel.read(4096))

asyncio.run(main())
```

`endpoint` accepts the canonical Endpoint ID or hostname. The hostname suffix
is `.db.service.internal.tiana.com`; SNI and CONNECT authority always use that
identity and logical port 443. TLS requires version 1.3, hostname verification,
and ALPN `h2`. `ca_file` accepts PEM certificates, `ca_pem` accepts PEM text.
System roots are enabled by default; set `use_system_roots=False` to trust
only the supplied roots. `gateway_address=(host, port)` overrides the physical
dial destination for local integration without changing SNI or authority.

The optional token is opaque: 1–4096 visible ASCII bytes, without whitespace or
control characters. Prefix, version, exact length and Base64 encoding are not
interpreted; values are never trimmed or rewritten. The Gateway decides validity
and scope. It is sent only in a never-indexed outer Proxy-Authorization header. An omitted token is decided by Gateway policy.
The SDK retains a redacted credential until Client close and clears its owned
mutable copy then. Dependency header logs are suppressed for SDK operations;
the SDK emits no request or response payload logs.

`connect(protocol)` waits for complete, validated 200 headers before exposing the
tunnel. Each tunnel owns a separate physical connection; a Client allows at
most 16 pending/active tunnels by default (`max_tunnels`). The default TCP
and TLS deadlines are 10 seconds each (`connect_timeout`), and the CONNECT
response deadline is 60 seconds (`response_timeout`).

The stream API supports one reader and one writer concurrently:

- `await tunnel.read(n)` returns up to `n` bytes; `read()` reads through EOF.
- `await tunnel.readexactly(n)` follows asyncio's IncompleteReadError semantics.
- `await tunnel.write(data)` waits for all bytes to pass HTTP/2 and TLS backpressure.
- `await tunnel.write_eof()` sends END_STREAM and leaves reads available.
- Remote END_STREAM ends reads and leaves writes available.
- `await tunnel.aclose()` releases both directions. `close()` starts closure;
  `await wait_closed()` waits for cleanup. Client close closes all owned tunnels.
- Cancelling a connect, read, or write task closes its tunnel and propagates
  CancelledError. Use `asyncio.timeout()` for an application I/O deadline.

Inbound transport buffering is bounded by the HTTP/2 receive window (65535
bytes), and writes use at most 16 KiB per DATA frame. `read()` and
`readexactly(n)` collect the amount requested by the application; use bounded
`read(n)` calls to process arbitrarily long streams.

`ConnectError` reports `code`, `phase`, and `outcome_unknown`. `GatewayError`
adds `status`, `retry_after_ms`, and `retryable`; retryability requires one of
the four frozen status/code pairs plus a valid 1..60000 ms hint.
`TunnelError` marks established-session failure with an unknown application
outcome. The SDK makes one CONNECT attempt per call and never replays bytes.
These rules and exact frozen revisions are in [the shared contract](docs/common-contract.md).

The stdin/stdout example sends opaque inner bytes and half-closes after input:

```sh
export TIANA_PROTOCOL=your-registered-profile
export TIANA_ENDPOINT=ep-01j5c9m7q2v8x4k6n3r0t1w2yz.db.service.internal.tiana.com
export TIANA_CA_FILE=/path/to/gateway-ca.pem
python examples/stream.py < request.bin > response.bin
```

`TIANA_TOKEN` supplies an existing token. For a local fixture, set
`TIANA_GATEWAY_ADDRESS=127.0.0.1:<listener-port>`. The example works with the
anonymous fixed Gateway fixture using `hello` as input, returning the fixture
greeting, echoed input, and EOF tail.

Build and validation:

```sh
python -m pip install build
python -m build
PYTHONPATH=src python -m unittest discover -v
python tests/gateway_smoke.py /path/to/fixed-gateway-fixture
```

The direct tests use TLS/H2 peers and fixed synthetic certificates from Rust
`3b8d64fd692e3b2edaebf4233ec25bf15bcc53d6`; authority assets are byte copies
from Contracts `59b3b4654511eb01b6d0c350fff8b7e22bbb0268`. Integration smoke
uses real Gateway source `4e026bcd34864e9c6aca4932ccad5e8c17f0496f` with
synthetic Control, Runtime, clock, and Agent. It exercises two 2 MiB sessions,
both auth modes, refusal metadata, half-close tails, and cancellation isolation.

## Limitations

This SDK exposes opaque application profile streams. The caller must supply a
profile supported by its Gateway; no application-specific default or allowlist
is built in. It implements no application adapter, framing, pool or CLI helper. GOAWAY terminates the owned connection with a typed failure;
accepted sessions are never replayed. Python cannot guarantee erasure of
immutable interpreter/TLS/HPACK copies or caller-owned strings. Validation
uses local fixtures, without a production database or cluster deployment.
The existing Agent/gnet path has a separate half-close limitation recorded in
the frozen contract; the synthetic Agent's passing EOF tail does not establish
that deployed Agent behavior.

## Generic protocol boundary

`connect(protocol)` requires 1–64 ASCII letters, digits, dots, underscores or
hyphens. It forwards the identifier in `tiana-database-protocol`; only the Gateway
decides supported profiles. The transport never interprets payloads. Application
adapters live in separate packages depending on `tiana-sdk`. The former implicit
profile and fixed application allowlist are removed without compatibility aliases.
Gateway smoke scripts also require `TIANA_PROTOCOL` for their fixture's registered
profile. Prior smoke results in docs/validation.md describe the original baseline,
not a rerun of that external Gateway fixture for this change.
