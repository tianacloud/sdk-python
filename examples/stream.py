"""Relay stdin/stdout through one native Tiana tunnel."""

import asyncio
import os
import stat
import sys

from tiana_sdk import Client, ConnectError


async def main():
    address = None
    if physical := os.environ.get("TIANA_GATEWAY_ADDRESS"):
        host, port = physical.rsplit(":", 1)
        address = (host, int(port))
    input_transport = None
    if stat.S_ISREG(os.fstat(sys.stdin.fileno()).st_mode):
        async def read_input():
            return sys.stdin.buffer.read1(16 * 1024)
    else:
        input_reader = asyncio.StreamReader()
        input_transport, _ = await asyncio.get_running_loop().connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(input_reader), sys.stdin.buffer)

        async def read_input():
            return await input_reader.read(16 * 1024)
    try:
        await relay(address, read_input)
    finally:
        if input_transport is not None:
            input_transport.close()


async def relay(address, read_input):
    async with Client(
        os.environ["TIANA_ENDPOINT"], token=os.environ.get("TIANA_TOKEN"),
        ca_file=os.environ.get("TIANA_CA_FILE"), gateway_address=address,
    ) as client:
        async with await client.connect(os.environ["TIANA_PROTOCOL"]) as tunnel:
            async def upload():
                while data := await read_input():
                    await tunnel.write(data)
                await tunnel.write_eof()

            async def download():
                while data := await tunnel.read(16 * 1024):
                    sys.stdout.buffer.write(data)
                    sys.stdout.buffer.flush()

            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(upload())
                tasks.create_task(download())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except ConnectError as error:
        print(error, file=sys.stderr)
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        raise SystemExit(130) from None
