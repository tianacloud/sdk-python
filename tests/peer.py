"""Independent TLS/H2 peer for wire and stream lifecycle tests."""

import asyncio
import ssl
from contextlib import suppress
from pathlib import Path

from h2.config import H2Configuration
from h2.connection import H2Connection
from h2.events import DataReceived, RequestReceived, StreamEnded, StreamReset
from h2.exceptions import H2Error
from h2.settings import SettingCodes, Settings

from tiana_sdk._logging import private_headers

FIXTURES = Path(__file__).parent / "fixtures"
ENDPOINT = "ep-01j5c9m7q2v8x4k6n3r0t1w2yz.db.service.internal.tiana.com"
TOKEN = "tia_" + "A" * 43


class Peer:
    def __init__(self, *, mode="echo", response=None, certificate="gateway", alpn="h2",
                 tls=ssl.TLSVersion.TLSv1_3):
        self.mode = mode
        self.response = response
        self.requests = []
        self.received = bytearray()
        self.settings = []
        self.request_tables = []
        self.tls = []
        self.sni = []
        self.errors = []
        self.ended = asyncio.Event()
        self.reset = asyncio.Event()
        self.requested = asyncio.Event()
        self.release = asyncio.Event()
        self.disconnected = asyncio.Event()
        self.tasks = set()
        self.writers = set()
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.minimum_version = tls
        self.context.maximum_version = tls
        self.context.set_alpn_protocols([alpn])
        self.context.load_cert_chain(FIXTURES / f"{certificate}.pem", FIXTURES / f"{certificate}-key.pem")
        self.context.set_servername_callback(lambda _ssl, name, _ctx: self.sni.append(name))

    async def __aenter__(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0, ssl=self.context)
        self.address = self.server.sockets[0].getsockname()[:2]
        return self

    async def __aexit__(self, *_exc):
        self.server.close()
        await self.server.wait_closed()
        for writer in self.writers:
            writer.close()
        tasks = tuple(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        self.writers.add(writer)
        connection = H2Connection(H2Configuration(client_side=False, normalize_inbound_headers=False))
        if self.mode == "blocked-write":
            connection.local_settings = Settings(client=False, initial_values={SettingCodes.INITIAL_WINDOW_SIZE: 0})
        connection.initiate_connection()
        writer.write(connection.data_to_send())
        tls = writer.get_extra_info("ssl_object")
        self.tls.append((tls.version(), tls.selected_alpn_protocol()))
        try:
            while data := await reader.read(65536):
                with private_headers():
                    events = connection.receive_data(data)
                for event in events:
                    if isinstance(event, RequestReceived):
                        self.requests.append(event.headers)
                        self.request_tables.append(connection.decoder.header_table_size)
                        self.settings.append(dict(connection.remote_settings))
                        self.requested.set()
                        if self.mode == "delayed":
                            await self.release.wait()
                        request = dict(event.headers)
                        headers = [(b":status", b"200"), (b"tiana-tunnel-version", b"1"),
                                   (b"tiana-auth-mode", b"TOKEN_REQUIRED" if b"proxy-authorization" in request else b"DISABLED"),
                                   (b"tiana-request-id", request[b"tiana-request-id"])]
                        if self.response is not None:
                            headers = self.response(headers)
                        connection.send_headers(event.stream_id, headers)
                        if dict(headers)[b":status"] != b"200":
                            connection.send_data(event.stream_id, b"untrusted body " + TOKEN.encode(), end_stream=True)
                        elif self.mode == "reset":
                            connection.reset_stream(event.stream_id, error_code=2)
                        elif self.mode == "remote-first":
                            connection.send_data(event.stream_id, b"greeting", end_stream=True)
                        elif self.mode == "goaway":
                            connection.close_connection(last_stream_id=event.stream_id,
                                                        additional_data=TOKEN.encode())
                        elif self.mode != "blocked-write":
                            connection.send_data(event.stream_id, b"greeting")
                    elif isinstance(event, DataReceived):
                        self.received.extend(event.data)
                        connection.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
                        if self.mode in ("echo", "delayed"):
                            # Echo only within the peer's advertised send window.
                            remaining = event.data
                            while remaining:
                                size = min(len(remaining), connection.max_outbound_frame_size,
                                           connection.local_flow_control_window(event.stream_id))
                                if size == 0:
                                    self.errors.append("test echo window exhausted")
                                    return
                                connection.send_data(event.stream_id, remaining[:size])
                                remaining = remaining[size:]
                    elif isinstance(event, StreamEnded):
                        self.ended.set()
                        if self.mode in ("echo", "delayed"):
                            connection.send_data(event.stream_id, b"tail", end_stream=True)
                    elif isinstance(event, StreamReset):
                        self.reset.set()
                writer.write(connection.data_to_send())
                await writer.drain()
        except (OSError, H2Error):
            pass
        finally:
            writer.close()
            with suppress(OSError, TimeoutError):
                async with asyncio.timeout(1):
                    await writer.wait_closed()
            self.writers.discard(writer)
            self.tasks.discard(task)
            self.disconnected.set()
