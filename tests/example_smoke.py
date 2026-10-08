"""Exercise the source-distribution example with an installed-wheel interpreter."""

import asyncio
import json
import os
from pathlib import Path
import signal
import sys
import tempfile


async def run(python: str, example: Path, fixture: Path):
    ready = json.loads((fixture / "ready.json").read_text())
    env = {k: v for k, v in os.environ.items() if not k.startswith("TIANA_") and k != "PYTHONPATH"}
    env.update(TIANA_ENDPOINT=ready["endpoint"], TIANA_GATEWAY_ADDRESS=ready["anonymous_address"],
               TIANA_CA_FILE=str(fixture / "fixtures/gateway.pem"), TIANA_PROTOCOL=os.environ["TIANA_PROTOCOL"])
    results = []
    for mode in ("pipe_eof", "file_eof", "sigint_open_stdin"):
        with tempfile.TemporaryFile() as input_file:
            input_file.write(b"hello")
            input_file.seek(0)
            child = await asyncio.create_subprocess_exec(
                python, str(example), stdin=input_file if mode == "file_eof" else asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env, cwd="/tmp")
            try:
                async with asyncio.timeout(5):
                    if mode == "sigint_open_stdin":
                        assert await child.stdout.readexactly(len(ready["greeting"])) == ready["greeting"].encode()
                        child.send_signal(signal.SIGINT)
                        code = await child.wait()
                        assert code == 130, code
                        assert b"Traceback" not in await child.stderr.read()
                    else:
                        output, errors = await child.communicate(b"hello" if mode == "pipe_eof" else None)
                        assert child.returncode == 0, errors.decode()
                        assert output == (ready["greeting"] + "hello" + ready["eof_tail"]).encode()
                    results.append({"case": mode, "exit_code": child.returncode})
            finally:
                if child.returncode is None:
                    child.kill()
                    await child.wait()
    return {"installed_python": python, "source_example": str(example), "results": results}


if __name__ == "__main__":
    print(json.dumps(asyncio.run(run(sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3]))), indent=2))
