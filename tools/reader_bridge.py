"""Reader bridge: links the HemoSense reader (its own WiFi, WebSocket on port 81) to the
HemoSense server on Nobus. The reader's firmware is not changed.

The laptop running this must be on two networks:
  - WiFi: the reader's network HemoSense-XXXX  (reader at ws://192.168.4.1:81)
  - Internet: phone USB tethering or a cable

    pip install websockets
    python tools/reader_bridge.py --server wss://YOUR-LINK --key YOUR_DEVICE_KEY
"""
import argparse
import asyncio
import json

import websockets


def log_line(direction: str, text: str):
    try:
        m = json.loads(text)
    except ValueError:
        return
    if m.get("type") == "reading":
        print(f"  reading {m.get('i'):>3}  Anti-A {m['z'][0]:>9}  Anti-B {m['z'][1]:>9}  Anti-D {m['z'][2]:>9}")
    else:
        print(f"  {direction} {m.get('type')}")


async def link(a):
    print(f"Looking for the reader at {a.reader} ...")
    async with websockets.connect(a.reader, ping_interval=20, ping_timeout=20, open_timeout=10) as reader:
        first = await asyncio.wait_for(reader.recv(), 10)        # firmware sends "info" on connect
        info = json.loads(first)
        rid = a.id or info.get("device_id") or "HS-DEV-001"
        print(f"Reader {rid} found (firmware {info.get('firmware')}, calibrated: {info.get('calibrated')})")
        url = f"{a.server.rstrip('/')}/ws/reader?device_id={rid}&key={a.key}"
        async with websockets.connect(url, ping_interval=20, open_timeout=15) as server:
            print("Connected to HemoSense on Nobus. Keep this window open.")
            await server.send(first)

            async def up():
                async for text in reader:
                    await server.send(text)
                    log_line("reader ->", text)

            async def down():
                async for text in server:
                    try:
                        m = json.loads(text)
                    except ValueError:
                        continue
                    if m.get("type") == "error":
                        print("Server says:", m.get("msg"))
                        continue
                    await reader.send(text)
                    log_line("server ->", text)

            tasks = [asyncio.create_task(up()), asyncio.create_task(down())]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            for t in done:
                t.result()


async def main():
    ap = argparse.ArgumentParser(description="Bridge the HemoSense reader to the server")
    ap.add_argument("--server", required=True, help="wss://your-link")
    ap.add_argument("--key", required=True, help="DEVICE_KEY from the server's .env")
    ap.add_argument("--reader", default="ws://192.168.4.1:81", help="reader WebSocket address")
    ap.add_argument("--id", help="name to show in the app (default: the reader's own ID)")
    a = ap.parse_args()
    while True:
        try:
            await link(a)
            print("Link closed.")
        except (OSError, asyncio.TimeoutError, ValueError, websockets.exceptions.WebSocketException) as e:
            print(f"Link down ({type(e).__name__}: {e}). Retrying in 3 s...")
        await asyncio.sleep(3)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
