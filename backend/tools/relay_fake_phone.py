"""A phone-relay stand-in, so the relay can be exercised with no phone.

Connects to a RUNNING car-harness server exactly as the Android app's Relay
mode does -- one WebSocket, JSON data frames, base64 ELM bytes -- and answers
every command with a canned ELM327 reply. Local clients skip the pairing
token, so on the same machine this needs no pairing at all.

With this running, `POST /api/adapters/test {"typeId":"elm327-remote"}` should
come back ok=true: the whole desktop side of the relay, end to end.

Run:  .venv\\Scripts\\python.exe -m backend.tools.relay_fake_phone
      .venv\\Scripts\\python.exe -m backend.tools.relay_fake_phone --host 127.0.0.1 --port 8765
"""

import argparse
import asyncio
import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import websockets  # noqa: E402

# What the fake dongle says to anything. The ELM327 banner is what the
# elm327-remote adapter's test() looks for, and the '>' is the prompt every
# transport waits on.
CANNED_REPLY = b"ELM327 v1.5\r\r>"


def _frame(data: bytes) -> str:
    return json.dumps({"type": "data", "b64": base64.b64encode(data).decode("ascii")})


async def relay(url: str, device: str) -> None:
    async with websockets.connect(url) as ws:
        print(f"relay up: {url}")
        await ws.send(json.dumps({"type": "hello", "device": device}))
        while True:
            text = await ws.recv()
            try:
                msg = json.loads(text)
            except ValueError:
                continue
            if msg.get("type") != "data":
                continue
            command = base64.b64decode(msg.get("b64") or "")
            print(f"  <- {command!r}   -> {CANNED_REPLY!r}")
            await ws.send(_frame(CANNED_REPLY))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--name", default="fake")
    parser.add_argument("--token", default="", help="only needed from another machine")
    parser.add_argument("--device", default="Fake dongle")
    args = parser.parse_args()

    url = f"ws://{args.host}:{args.port}/api/remote/ws?name={args.name}"
    if args.token:
        url += f"&token={args.token}"
    try:
        asyncio.run(relay(url, args.device))
    except KeyboardInterrupt:
        return 0
    except OSError as exc:
        print(f"could not reach the server: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
