"""TLS/H2 transport with one owned connection per tunnel."""

import asyncio
import math
import secrets
import ssl
from contextlib import suppress
from os import PathLike

from h2.config import H2Configuration
from h2.connection import H2Connection
from h2.errors import ErrorCodes
from h2.events import (
    ConnectionTerminated, DataReceived, InformationalResponseReceived,
    ResponseReceived, StreamEnded, StreamReset, TrailersReceived,
)
from h2.exceptions import H2Error
from h2.settings import SettingCodes, Settings
from hpack import NeverIndexedHeaderTuple

from ._logging import private_headers
from ._protocol import SecretToken, endpoint_host, response_metadata, validate_protocol
from .errors import ConnectError, TunnelError


class Client:
    """An Endpoint-bound client. Use as an async context manager to release resources."""

    def __init__(
        self,
        endpoint: str,
        *,
        token: str | None = None,
        ca_file: str | PathLike[str] | None = None,
        ca_pem: str | None = None,
        use_system_roots: bool = True,
        gateway_address: tuple[str, int] | None = None,
        connect_timeout: float = 10.0,
        response_timeout: float = 60.0,
        max_tunnels: int = 16,
    ):
        self.endpoint = endpoint_host(endpoint)
        if (
            not isinstance(max_tunnels, int) or isinstance(max_tunnels, bool) or max_tunnels < 1
            or any(not isinstance(t, (int, float)) or not math.isfinite(t) or t <= 0
                   for t in (connect_timeout, response_timeout))
        ):
            raise ConnectError("INVALID_CONFIGURATION", "configuration")
        if gateway_address is not None and (
            not isinstance(gateway_address, tuple) or len(gateway_address) != 2
            or not isinstance(gateway_address[0], str) or not gateway_address[0]
            or not isinstance(gateway_address[1], int) or not 1 <= gateway_address[1] <= 65535
        ):
            raise ConnectError("INVALID_GATEWAY_ADDRESS", "configuration")
        if not use_system_roots and ca_file is None and ca_pem is None:
            raise ConnectError("MISSING_TRUST_ROOT", "configuration")
        try:
            context = (ssl.create_default_context() if use_system_roots
                       else ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT))
            if ca_file is not None or ca_pem is not None:
                context.load_verify_locations(cafile=ca_file, cadata=ca_pem)
            context.minimum_version = ssl.TLSVersion.TLSv1_3
            context.maximum_version = ssl.TLSVersion.TLSv1_3
            context.set_alpn_protocols(["h2"])
        except (OSError, ValueError, TypeError):
            raise ConnectError("INVALID_TRUST_ROOT", "configuration") from None
        self._tls = context
        self._token = SecretToken(token) if token is not None else None
        self._address = gateway_address or (self.endpoint, 443)
        self._connect_timeout = connect_timeout
        self._response_timeout = response_timeout
        self._max_tunnels = max_tunnels
        self._tunnels: set[Tunnel] = set()
        self._closed = False

    async def connect(self, protocol: str) -> "Tunnel":
        """Open one tunnel, returning only after complete validated 200 headers."""
        validate_protocol(protocol)
        if self._closed:
            raise ConnectError("CLIENT_CLOSED", "configuration")
        if len(self._tunnels) >= self._max_tunnels:
            raise ConnectError("LOCAL_CONNECTION_LIMIT", "configuration")
        tunnel = Tunnel(self, protocol)
        self._tunnels.add(tunnel)
        tunnel._connect_task = asyncio.current_task()
        try:
            await tunnel._open()
            return tunnel
        except BaseException:
            tunnel.close()
            await tunnel.wait_closed()
            raise
        finally:
            tunnel._connect_task = None

    def close(self) -> None:
        """Stop new connects and close all active or pending tunnels."""
        self._closed = True
        if self._token is not None:
            self._token.clear()
            self._token = None
        for tunnel in tuple(self._tunnels):
            tunnel.close()

    async def aclose(self) -> None:
        tunnels = tuple(self._tunnels)
        self.close()
        await asyncio.gather(*(t.wait_closed() for t in tunnels))

    async def __aenter__(self) -> "Client":
        if self._closed:
            raise ConnectError("CLIENT_CLOSED", "configuration")
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.aclose()


class Tunnel:
    """An ordered async byte stream. Await writes for transport backpressure.

    One reader and one writer may run concurrently. Cancellation of an I/O
    coroutine closes this tunnel and propagates CancelledError.
    """

    def __init__(self, client: Client, protocol: str):
        self.endpoint = client.endpoint
        self.protocol = protocol
        self.request_id = "req-" + secrets.token_urlsafe(18)
        self.auth_mode: str | None = None
        self._client = client
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._driver: asyncio.Task | None = None
        self._connect_task: asyncio.Task | None = None
        self._h2 = H2Connection(H2Configuration(client_side=True))
        self._h2.local_settings = Settings(client=True, initial_values={
            SettingCodes.HEADER_TABLE_SIZE: 0,
            SettingCodes.ENABLE_PUSH: 0,
            SettingCodes.MAX_HEADER_LIST_SIZE: 16 * 1024,
        })
        self._h2.decoder.max_header_list_size = 16 * 1024
        self._buffer = bytearray()
        self._changed = asyncio.Event()
        self._response = asyncio.Event()
        self._read_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._error: ConnectError | None = None
        self._accepted = False
        self._closed = False
        self._local_eof = False
        self._remote_eof = False

    async def _open(self) -> None:
        phase = "tcp"
        try:
            async with asyncio.timeout(self._client._connect_timeout):
                self._reader, self._writer = await asyncio.open_connection(*self._client._address)
            phase = "tls"
            async with asyncio.timeout(self._client._connect_timeout):
                await self._writer.start_tls(self._client._tls, server_hostname=self.endpoint)
            tls = self._writer.get_extra_info("ssl_object")
            if tls.version() != "TLSv1.3" or tls.selected_alpn_protocol() != "h2":
                raise ConnectError("TLS_NEGOTIATION_FAILED", "tls")
            phase = "response"
            self._h2.initiate_connection()
            headers = [
                (b":method", b"CONNECT"),
                (b":authority", (self.endpoint + ":443").encode()),
                (b"tiana-tunnel-version", b"1"),
                (b"tiana-database-protocol", self.protocol.encode()),
                (b"tiana-request-id", self.request_id.encode()),
                (b"user-agent", b"tiana-sdk-python/0.1.0.dev0"),
            ]
            if self._client._token is not None:
                headers.append(NeverIndexedHeaderTuple(b"proxy-authorization", self._client._token._header()))
            with private_headers():
                self._h2.encoder.header_table_size = 0
                self._h2.send_headers(1, headers, end_stream=False)
            headers.clear()
            self._driver = asyncio.create_task(self._receive(), name="tiana-tunnel")
            async with asyncio.timeout(self._client._response_timeout):
                await self._flush()
                await self._response.wait()
            if self._error is not None:
                raise self._error
        except TimeoutError:
            raise ConnectError("TIMEOUT", phase, outcome_unknown=self._accepted) from None
        except ssl.SSLError:
            raise ConnectError("TLS_FAILED", "tls") from None
        except OSError:
            raise ConnectError("GATEWAY_UNREACHABLE" if phase == "tcp" else "CONNECTION_FAILED", phase) from None
        except H2Error:
            raise ConnectError("HTTP2_FAILED", "http2", outcome_unknown=self._accepted) from None

    async def _flush(self) -> None:
        self._writer.write(self._h2.data_to_send())
        await self._writer.drain()

    def _fail(self, error: ConnectError) -> None:
        if self._error is None:
            self._error = error
        self._response.set()
        self._changed.set()

    def _transport_error(self, code: str) -> ConnectError:
        return TunnelError(code) if self._accepted else ConnectError(code, "http2")

    async def _receive(self) -> None:
        try:
            while not self._closed:
                data = await self._reader.read(16 * 1024)
                if not data:
                    if not (self._local_eof and self._remote_eof):
                        self._fail(self._transport_error("UNEXPECTED_EOF"))
                    break
                with private_headers():
                    events = self._h2.receive_data(data)
                for event in events:
                    if isinstance(event, ResponseReceived):
                        self._accepted |= (b":status", b"200") in event.headers
                        self.auth_mode = response_metadata(event.headers, self.request_id)
                        self._response.set()
                    elif isinstance(event, DataReceived):
                        if self.auth_mode is None:
                            raise self._transport_error("INVALID_RESPONSE")
                        self._buffer.extend(event.data)
                        padding = event.flow_controlled_length - len(event.data)
                        if padding:
                            self._h2.acknowledge_received_data(padding, 1)
                    elif isinstance(event, StreamEnded):
                        self._remote_eof = True
                    elif isinstance(event, StreamReset):
                        raise self._transport_error("STREAM_RESET")
                    elif isinstance(event, ConnectionTerminated):
                        raise self._transport_error("GOAWAY")
                    elif isinstance(event, (InformationalResponseReceived, TrailersReceived)):
                        raise self._transport_error("INVALID_RESPONSE")
                self._changed.set()
                await self._flush()
                if self._local_eof and self._remote_eof:
                    break
        except asyncio.CancelledError:
            raise
        except ConnectError as error:
            if self._accepted and not error.outcome_unknown:
                error = ConnectError(error.code, error.phase, outcome_unknown=True)
            self._fail(error)
        except (OSError, H2Error, ValueError):
            self._fail(self._transport_error("CONNECTION_FAILED"))
        finally:
            self._changed.set()
            self._writer.close()
            try:
                async with asyncio.timeout(1):
                    await asyncio.shield(self._writer.wait_closed())
            except (TimeoutError, OSError):
                self._writer.transport.abort()
            self._client._tunnels.discard(self)

    async def _read(self, n: int) -> bytes:
        while not self._buffer:
            if self._error is not None:
                raise self._error
            if self._closed:
                raise TunnelError("TUNNEL_CLOSED")
            if self._remote_eof:
                return b""
            self._changed.clear()
            await self._changed.wait()
        count = min(n, len(self._buffer))
        chunk = bytes(self._buffer[:count])
        del self._buffer[:count]
        if not self._writer.is_closing():
            self._h2.acknowledge_received_data(count, 1)
            await self._flush()
        return chunk

    async def read(self, n: int = -1) -> bytes:
        """Read up to n bytes, or through EOF for -1; zero returns immediately."""
        if n < -1:
            raise ValueError("n must be -1 or nonnegative")
        if n == 0:
            return b""
        try:
            async with self._read_lock:
                if n >= 0:
                    return await self._read(n)
                chunks = []
                while chunk := await self._read(64 * 1024):
                    chunks.append(chunk)
                return b"".join(chunks)
        except asyncio.CancelledError:
            await self.aclose()
            raise
        except (OSError, H2Error):
            await self.aclose()
            raise TunnelError("READ_FAILED") from None

    async def readexactly(self, n: int) -> bytes:
        """Read n bytes, raising asyncio.IncompleteReadError on a clean early EOF."""
        if n < 0:
            raise ValueError("n must be nonnegative")
        result = bytearray()
        try:
            async with self._read_lock:
                while len(result) < n:
                    chunk = await self._read(n - len(result))
                    if not chunk:
                        raise asyncio.IncompleteReadError(bytes(result), n)
                    result.extend(chunk)
                return bytes(result)
        except asyncio.CancelledError:
            await self.aclose()
            raise
        except (OSError, H2Error):
            await self.aclose()
            raise TunnelError("READ_FAILED") from None

    def _check_write(self) -> None:
        if self._error is not None:
            raise self._error
        if self._closed or self._local_eof:
            raise TunnelError("WRITE_CLOSED")

    async def write(self, data: bytes | bytearray | memoryview) -> None:
        """Write all bytes in order, awaiting HTTP/2 and TLS backpressure."""
        view = memoryview(data).cast("B")
        try:
            async with self._write_lock:
                self._check_write()
                while view:
                    self._check_write()
                    count = min(len(view), 16 * 1024, self._h2.max_outbound_frame_size,
                                self._h2.local_flow_control_window(1))
                    if count <= 0:
                        self._changed.clear()
                        await self._changed.wait()
                        continue
                    self._h2.send_data(1, view[:count].tobytes())
                    view = view[count:]
                    await self._flush()
        except asyncio.CancelledError:
            await self.aclose()
            raise
        except (OSError, H2Error):
            await self.aclose()
            raise TunnelError("WRITE_FAILED") from None

    async def write_eof(self) -> None:
        """Half-close writes with END_STREAM, keeping reads available."""
        try:
            async with self._write_lock:
                if self._local_eof:
                    return
                self._check_write()
                self._h2.end_stream(1)
                self._local_eof = True
                await self._flush()
        except asyncio.CancelledError:
            await self.aclose()
            raise
        except (OSError, H2Error):
            await self.aclose()
            raise TunnelError("WRITE_FAILED") from None

    def close(self) -> None:
        """Close both directions. Call wait_closed or aclose to await cleanup."""
        if self._closed:
            return
        self._closed = True
        self._buffer.clear()
        self._changed.set()
        self._response.set()
        current = asyncio.current_task()
        if self._connect_task is not None and self._connect_task is not current:
            self._connect_task.cancel()
        if self._writer is not None and not self._writer.is_closing():
            with suppress(H2Error, OSError):
                if not (self._local_eof and self._remote_eof):
                    self._h2.reset_stream(1, error_code=ErrorCodes.CANCEL)
                    self._writer.write(self._h2.data_to_send())
            self._writer.close()
        if self._driver is not None and self._driver is not current:
            self._driver.cancel()

    async def wait_closed(self) -> None:
        current = asyncio.current_task()
        for task in (self._connect_task, self._driver):
            if task is not None and task is not current:
                with suppress(asyncio.CancelledError):
                    await task
        if self._writer is not None:
            try:
                async with asyncio.timeout(1):
                    await asyncio.shield(self._writer.wait_closed())
            except (TimeoutError, OSError):
                self._writer.transport.abort()
        self._client._tunnels.discard(self)

    async def aclose(self) -> None:
        self.close()
        await self.wait_closed()

    async def __aenter__(self) -> "Tunnel":
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.aclose()
