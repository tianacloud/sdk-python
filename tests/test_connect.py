import asyncio
import io
import json
import logging
import ssl
import unittest

from h2.settings import SettingCodes
from hpack import NeverIndexedHeaderTuple

from tiana_sdk import Client, ConnectError, GatewayError, TunnelError
from tiana_sdk._protocol import ENDPOINT_SUFFIX, SecretToken, response_metadata
from .peer import ENDPOINT, FIXTURES, TOKEN, Peer


def client(peer, **options):
    return Client(ENDPOINT, gateway_address=peer.address, ca_file=FIXTURES / "gateway.pem",
                  use_system_roots=False, **options)


class ConfigurationTests(unittest.TestCase):
    def test_frozen_authority(self):
        authority = json.loads((FIXTURES / "authority.json").read_text())
        self.assertEqual(ENDPOINT_SUFFIX, authority["endpoint_suffix"])
        self.assertEqual(443, authority["default_port"])
        self.assertEqual(Client(ENDPOINT.upper()).endpoint, ENDPOINT)
        self.assertEqual(Client(ENDPOINT.removesuffix(ENDPOINT_SUFFIX)).endpoint, ENDPOINT)

    def test_invalid_endpoint_token_and_trust(self):
        for value in ["https://" + ENDPOINT, "localhost", ENDPOINT + ":80", ENDPOINT + ".",
                      ENDPOINT.replace("ep-0", "ep-8"), ENDPOINT.replace(".db.service.internal.tiana.com", ".db.tiana.dev")]:
            with self.subTest(endpoint=value), self.assertRaises(ConnectError):
                Client(value)
        for token in ["", TOKEN + "\r\n", " leading", "trailing ", "x\t", "x\0", "x\x7f", "é", "x" * 4097]:
            with self.assertRaises(ConnectError) as caught:
                Client(ENDPOINT, token=token)
            self.assertNotIn(token or "tia_", repr(caught.exception))
        with self.assertRaises(ConnectError):
            Client(ENDPOINT, use_system_roots=False)
        with self.assertRaises(ConnectError):
            Client(ENDPOINT, ca_pem="not a certificate")
        with self.assertRaises(ConnectError):
            Client(ENDPOINT, connect_timeout=0)

    def test_secret_representation(self):
        token = SecretToken(TOKEN)
        self.assertEqual(repr(token), "SecretToken([REDACTED])")
        token.clear()
        self.assertEqual(token._value, b"\0" * 47)

    def test_opaque_tokens_preserve_bytes_and_clear_owned_storage(self):
        for value in [TOKEN, "tia_1" + "A" * 43, "session.other-format_+/==",
                      "tia_noncanonical", "!", "x" * 4096]:
            token = SecretToken(value)
            self.assertEqual(token._header(), b"Bearer " + value.encode("ascii"))
            self.assertEqual(repr(token), "SecretToken([REDACTED])")
            token.clear()
            self.assertEqual(token._value, b"\0" * len(value))
        for value in [None, 42, b"token"]:
            with self.assertRaises(ConnectError):
                SecretToken(value)

    def test_duplicate_success_headers_preserve_observed_200(self):
        with self.assertRaises(ConnectError) as caught:
            response_metadata([(b":status", b"200"), (b"tiana-tunnel-version", b"1"),
                               (b"tiana-request-id", b"req-12345678"),
                               (b"tiana-auth-mode", b"DISABLED"), (b"tiana-auth-mode", b"DISABLED")],
                              "req-12345678")
        self.assertTrue(caught.exception.outcome_unknown)


class ConnectTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.limit = asyncio.timeout(8)
        await self.limit.__aenter__()

    async def asyncTearDown(self):
        await self.limit.__aexit__(None, None, None)

    async def test_tls_wire_server_first_and_local_half_close(self):
        async with Peer() as peer, client(peer, token=TOKEN) as c:
            async with await c.connect("echo") as tunnel:
                self.assertEqual(await tunnel.readexactly(8), b"greeting")
                await tunnel.write(b"\0hello\xff")
                self.assertEqual(await tunnel.readexactly(7), b"\0hello\xff")
                await tunnel.write_eof()
                await tunnel.write_eof()
                self.assertEqual(await tunnel.read(), b"tail")
                with self.assertRaises(TunnelError):
                    await tunnel.write(b"late")
            self.assertEqual(peer.received, b"\0hello\xff")
            self.assertTrue(peer.ended.is_set())
            self.assertEqual(peer.sni, [ENDPOINT])
            self.assertEqual(peer.tls, [("TLSv1.3", "h2")])
            headers = peer.requests[0]
            self.assertEqual(set(dict(headers)), {b":method", b":authority", b"tiana-tunnel-version",
                             b"tiana-database-protocol", b"tiana-request-id", b"user-agent", b"proxy-authorization"})
            self.assertEqual(dict(headers)[b":authority"], (ENDPOINT + ":443").encode())
            self.assertEqual(dict(headers)[b":method"], b"CONNECT")
            auth = next(h for h in headers if h[0] == b"proxy-authorization")
            self.assertIsInstance(auth, NeverIndexedHeaderTuple)
            self.assertEqual(peer.settings[0][SettingCodes.HEADER_TABLE_SIZE], 0)
            self.assertEqual(peer.request_tables, [0])
            self.assertFalse(c._tunnels)

    async def test_opaque_token_forwarding_is_exact_and_never_indexed(self):
        for value in ["tia_1" + "A" * 43, "session.other-format_+/==", "x" * 4096]:
            async with Peer() as peer, client(peer, token=value) as c:
                async with await c.connect("echo") as tunnel:
                    await tunnel.write_eof()
                    await tunnel.read()
                auth = next(h for h in peer.requests[0] if h[0] == b"proxy-authorization")
                self.assertEqual(auth[1], b"Bearer " + value.encode("ascii"))
                self.assertIsInstance(auth, NeverIndexedHeaderTuple)

    async def test_remote_half_close_keeps_writes(self):
        async with Peer(mode="remote-first") as peer, client(peer) as c:
            async with await c.connect("echo") as tunnel:
                self.assertEqual(await tunnel.read(), b"greeting")
                await tunnel.write(b"after peer EOF")
                await tunnel.write_eof()
                await peer.ended.wait()
            self.assertEqual(peer.received, b"after peer EOF")

    async def test_barrier_and_cancel_pending(self):
        async with Peer(mode="delayed") as peer, client(peer) as c:
            pending = asyncio.create_task(c.connect("echo"))
            await peer.requested.wait()
            self.assertFalse(pending.done())
            self.assertEqual(peer.received, b"")
            pending.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await pending
            self.assertFalse(c._tunnels)
            peer.release.set()
            await peer.disconnected.wait()

    async def test_cancel_read_preserves_other_tunnel(self):
        async with Peer() as peer, client(peer) as c:
            first, second = await asyncio.gather(c.connect("echo"), c.connect("echo"))
            await first.readexactly(8)
            pending = asyncio.create_task(first.read(1))
            await asyncio.sleep(0)
            pending.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await pending
            self.assertTrue(first._driver.done())
            self.assertEqual(await second.readexactly(8), b"greeting")
            await second.write(b"survives")
            self.assertEqual(await second.readexactly(8), b"survives")
            await second.aclose()

    async def test_cancel_blocked_write(self):
        async with Peer(mode="blocked-write") as peer, client(peer) as c:
            tunnel = await c.connect("echo")
            pending = asyncio.create_task(tunnel.write(b"blocked"))
            await asyncio.sleep(0)
            self.assertFalse(pending.done())
            pending.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await pending
            self.assertTrue(tunnel._driver.done())
            self.assertFalse(c._tunnels)

    async def test_all_gateway_codes_and_retry_hints(self):
        cases = [(400, "MALFORMED_CONNECT", False), (400, "EARLY_TUNNEL_DATA", False),
                 (407, "AUTH_REQUIRED", False), (407, "ACCESS_DENIED", False),
                 (407, "AUTHORIZATION_EXPIRED", False), (504, "CALLER_DEADLINE", False),
                 (421, "ENDPOINT_MISMATCH", False), (429, "CONNECTION_LIMIT", True),
                 (503, "POLICY_UNAVAILABLE", True), (503, "INSTANCE_UNAVAILABLE", True),
                 (504, "ACTIVATION_TIMEOUT", True)]
        for status, code, retryable in cases:
            def response(_headers):
                return [(b":status", str(status).encode()), (b"tiana-error-code", code.encode()),
                        (b"tiana-retry-after-ms", b"1")]
            async with Peer(response=response) as peer, client(peer) as c:
                with self.assertRaises(GatewayError) as caught:
                    await c.connect("echo")
                error = caught.exception
                self.assertEqual((error.status, error.code, error.retryable), (status, code, retryable))
                self.assertFalse(error.outcome_unknown)
                self.assertNotIn(TOKEN, repr(error))
                self.assertEqual(len(peer.requests), 1)
                self.assertEqual(peer.received, b"")

    async def test_unknown_error_and_bad_retry_hint_are_redacted(self):
        for code, hint in [(TOKEN, b"1"), ("POLICY_UNAVAILABLE", b"0"),
                           ("POLICY_UNAVAILABLE", b"60001"), ("POLICY_UNAVAILABLE", b"1.5")]:
            async with Peer(response=lambda _: [(b":status", b"503"), (b"tiana-error-code", code.encode()),
                                                (b"tiana-retry-after-ms", hint)]) as peer, client(peer) as c:
                with self.assertRaises(GatewayError) as caught:
                    await c.connect("echo")
                self.assertFalse(caught.exception.retryable)
                self.assertNotIn(TOKEN, repr(caught.exception))

    async def test_malformed_success_and_committed_failure_no_replay(self):
        for change in [lambda h: h + [(b"unexpected", b"value")],
                       lambda h: h + [(b"tiana-auth-mode", b"DISABLED")],
                       lambda h: h[:-1] + [(b"tiana-request-id", b"req-wrong-id")]]:
            async with Peer(response=change) as peer, client(peer) as c:
                with self.assertRaises(ConnectError) as caught:
                    await c.connect("echo")
                self.assertEqual(caught.exception.code, "INVALID_RESPONSE")
                self.assertTrue(caught.exception.outcome_unknown)
                self.assertEqual(len(peer.requests), 1)
        for mode in ["reset", "goaway"]:
            async with Peer(mode=mode) as peer, client(peer) as c:
                with self.assertRaises(ConnectError) as caught:
                    tunnel = await c.connect("echo")
                    await tunnel.read(1)
                self.assertTrue(caught.exception.outcome_unknown)
                self.assertNotIn(TOKEN, repr(caught.exception))
                self.assertEqual(len(peer.requests), 1)

    async def test_tls_rejections(self):
        for opts in [dict(certificate="wrong"), dict(alpn="http/1.1"), dict(tls=ssl.TLSVersion.TLSv1_2)]:
            async with Peer(**opts) as peer, client(peer) as c:
                with self.assertRaises(ConnectError) as caught:
                    await c.connect("echo")
                self.assertEqual(caught.exception.phase, "tls")
                self.assertFalse(peer.requests)
        async with Peer(certificate="wrong") as peer:
            async with Client(ENDPOINT, gateway_address=peer.address, use_system_roots=False,
                              ca_file=FIXTURES / "wrong.pem") as c:
                with self.assertRaises(ConnectError) as caught:
                    await c.connect("echo")
                self.assertEqual(caught.exception.phase, "tls")
                self.assertFalse(peer.requests)

    async def test_tcp_failure_and_cancel_tls_handshake(self):
        listener = await asyncio.start_server(lambda _r, _w: None, "127.0.0.1", 0)
        address = listener.sockets[0].getsockname()[:2]
        listener.close()
        await listener.wait_closed()
        async with Client(ENDPOINT, gateway_address=address) as c:
            with self.assertRaises(ConnectError) as caught:
                await c.connect("echo")
            self.assertEqual(caught.exception.code, "GATEWAY_UNREACHABLE")

        started = asyncio.Event()
        stopped = asyncio.Event()

        async def stall(reader, writer):
            started.set()
            try:
                await reader.read()
            finally:
                writer.close()
                await writer.wait_closed()
                stopped.set()

        listener = await asyncio.start_server(stall, "127.0.0.1", 0)
        address = listener.sockets[0].getsockname()[:2]
        try:
            async with Client(ENDPOINT, gateway_address=address) as c:
                pending = asyncio.create_task(c.connect("echo"))
                await started.wait()
                pending.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await pending
                await stopped.wait()
                self.assertFalse(c._tunnels)
        finally:
            listener.close()
            await listener.wait_closed()

    async def test_connect_deadline_and_client_close_pending(self):
        async with Peer(mode="delayed") as peer, client(peer, response_timeout=0.05) as c:
            with self.assertRaises(ConnectError) as caught:
                await c.connect("echo")
            self.assertEqual(caught.exception.code, "TIMEOUT")
            self.assertFalse(c._tunnels)
            peer.release.set()
        async with Peer(mode="delayed") as peer:
            c = client(peer)
            pending = asyncio.create_task(c.connect("echo"))
            await peer.requested.wait()
            await c.aclose()
            with self.assertRaises(asyncio.CancelledError):
                await pending
            self.assertFalse(c._tunnels)
            peer.release.set()

    async def test_client_capacity_and_profile_rejection(self):
        async with Peer() as peer, client(peer, max_tunnels=1) as c:
            t = await c.connect("echo")
            for profile in ["echo", "new-profile"]:
                with self.assertRaises(ConnectError):
                    await c.connect(profile)
            self.assertEqual(len(peer.requests), 1)
            await t.aclose()

    async def test_read_bounds_and_incomplete_eof(self):
        async with Peer(mode="remote-first") as peer, client(peer) as c:
            async with await c.connect("echo") as tunnel:
                self.assertEqual(await tunnel.read(0), b"")
                with self.assertRaises(asyncio.IncompleteReadError) as caught:
                    await tunnel.readexactly(20)
                self.assertEqual(caught.exception.partial, b"greeting")

    async def test_hpack_debug_has_no_credentials_or_encoded_blocks(self):
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        logger = logging.getLogger("hpack")
        previous = logger.level
        logger.setLevel(logging.DEBUG)
        logger.addHandler(handler)
        try:
            async with Peer() as peer, client(peer, token=TOKEN) as c:
                async with await c.connect("echo") as tunnel:
                    await tunnel.readexactly(8)
            self.assertNotIn(TOKEN, output.getvalue())
            self.assertNotIn("proxy-authorization", output.getvalue())
        finally:
            logger.removeHandler(handler)
            logger.setLevel(previous)


if __name__ == "__main__":
    unittest.main()


class GenericProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_application_profiles_are_forwarded_without_allowlist(self):
        async with Peer() as peer, client(peer) as c:
            for name in ["my-application-v2", "App_1.0", "x" * 64]:
                async with await c.connect(name) as tunnel:
                    self.assertEqual(tunnel.protocol, name)
                    self.assertEqual(dict(peer.requests[-1])[b"tiana-database-protocol"], name.encode())
                    await tunnel.write(b"\0opaque\xff")
                    self.assertEqual(await tunnel.readexactly(16), b"greeting\0opaque\xff")

    async def test_invalid_protocols_fail_before_io(self):
        async with Peer() as peer, client(peer) as c:
            for name in [None, [], "", "x" * 65, "x/y", "x y", "é", "x\r\nx-injected: 1"]:
                with self.subTest(name=name), self.assertRaises(ConnectError) as caught:
                    await c.connect(name)
                self.assertEqual(caught.exception.code, "INVALID_PROTOCOL")
            with self.assertRaises(TypeError):
                await c.connect()
            self.assertFalse(peer.requests)

    async def test_stream_example_accepts_explicit_profile(self):
        import os
        import sys
        from pathlib import Path

        async with Peer() as peer:
            env = {k: v for k, v in os.environ.items() if not k.startswith("TIANA_")}
            env.update(TIANA_ENDPOINT=ENDPOINT, TIANA_PROTOCOL="custom-stream",
                       TIANA_CA_FILE=str(FIXTURES / "gateway.pem"),
                       TIANA_GATEWAY_ADDRESS=f"{peer.address[0]}:{peer.address[1]}")
            child = await asyncio.create_subprocess_exec(
                sys.executable, str(Path(__file__).parents[1] / "examples/stream.py"),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, env=env)
            try:
                async with asyncio.timeout(5):
                    output, errors = await child.communicate(b"hello")
                self.assertEqual(child.returncode, 0, errors.decode())
                self.assertEqual(output, b"greetinghellotail")
                self.assertEqual(dict(peer.requests[0])[b"tiana-database-protocol"], b"custom-stream")
            finally:
                if child.returncode is None:
                    child.kill()
                    await child.wait()
