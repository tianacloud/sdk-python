"""Consume the fixed Gateway fixture using only the installed/public SDK API."""

import asyncio
import json
import os
from pathlib import Path
import sys

from tiana_sdk import Client, GatewayError


async def run(root: Path):
    ready = json.loads((root / "ready.json").read_text())
    token = (root / "fixtures/synthetic-token.txt").read_text().strip()
    results = []

    def make_client(address, credential=None):
        host, port = ready[address].rsplit(":", 1)
        return Client(ready["endpoint"], token=credential, gateway_address=(host, int(port)),
                      ca_file=root / "fixtures/gateway.pem", use_system_roots=False)

    payload = bytes(range(256)) * 8192
    for address, credential in [("anonymous_address", None), ("token_address", token)]:
        async with make_client(address, credential) as client:
            async with await client.connect(os.environ["TIANA_PROTOCOL"]) as tunnel:
                assert await tunnel.readexactly(len(ready["greeting"])) == ready["greeting"].encode()

                async def send():
                    await tunnel.write(payload)
                    await tunnel.write_eof()

                _, received = await asyncio.gather(send(), tunnel.read())
                assert received == payload + ready["eof_tail"].encode()
                results.append({"case": address, "bytes_each_direction": len(payload),
                                "auth_mode": tunnel.auth_mode, "server_first": True, "half_close_tail": True})

    for address, credential, code, status, retryable in [
        ("token_address", None, "AUTH_REQUIRED", 407, False),
        ("token_address", "tia_" + "B" * 42 + "A", "ACCESS_DENIED", 407, False),
        ("unavailable_address", None, "POLICY_UNAVAILABLE", 503, True),
    ]:
        async with make_client(address, credential) as client:
            try:
                await client.connect(os.environ["TIANA_PROTOCOL"])
            except GatewayError as error:
                assert (error.code, error.status, error.retryable) == (code, status, retryable)
                assert not error.outcome_unknown
                results.append({"case": code, "status": error.status, "retryable": error.retryable})
            else:
                raise AssertionError("Expected Gateway refusal")

    async with make_client("anonymous_address") as client:
        first, second = await asyncio.gather(client.connect(os.environ["TIANA_PROTOCOL"]), client.connect(os.environ["TIANA_PROTOCOL"]))
        await first.readexactly(len(ready["greeting"]))
        pending = asyncio.create_task(first.read(1))
        await asyncio.sleep(0)
        pending.cancel()
        try:
            await pending
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("Cancellation did not propagate")
        await second.readexactly(len(ready["greeting"]))
        await second.write(b"sibling")
        await second.write_eof()
        assert await second.read() == b"sibling" + ready["eof_tail"].encode()
        await second.aclose()
        results.append({"case": "cancel_read_and_preserve_other_tunnel", "pass": True})

    return {"gateway_commit": ready["gateway_commit"], "results": results,
            "real": "Gateway TLS/H2 ingress, CONNECT, authorization/commit, route handshake client, relay",
            "synthetic": "Control, Runtime, clock, loopback Agent", "database": False}


async def main():
    async with asyncio.timeout(30):
        print(json.dumps(await run(Path(sys.argv[1])), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
