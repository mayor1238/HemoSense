"""Pretend HemoSense reader for testing without hardware.

It speaks the same messages as the ESP32 firmware, and every reading is marked
simulated so the app labels the result "Simulated readings".

    pip install websockets
    python tools/virtual_reader.py --server ws://localhost:8000 --key dev-device-key
On the Nobus server:
    sudo docker compose exec app python tools/virtual_reader.py --server ws://localhost:8000 --key YOUR_DEVICE_KEY --speed 4
"""
import argparse
import asyncio
import json
import math
import random
import time

GROUPS = ["A+", "A-", "B+", "B-", "AB+", "AB-", "O+", "O-"]


class FakeReader:
    def __init__(self, reader_id="HS-VIRTUAL-01", group=None, speed=1.0):
        self.id, self.group, self.speed = reader_id, group, speed
        self.send = None                       # async function set by the transport
        self.calibrated = False
        self.running = False
        self.test_id = None
        self.sent: list[dict] = []
        self.task = None

    def info(self):
        msg = {"type": "info", "device_id": self.id, "firmware": "virtual-2.0", "simulate": True,
               "sensor_ok": True, "calibrated": self.calibrated, "state": "running" if self.running else "idle"}
        if self.running:
            msg["test_id"] = self.test_id
        return msg

    async def handle(self, m: dict):
        kind = m.get("type")
        if kind == "hello":
            await self.send(self.info())
        elif kind == "calibrate":
            if self.running:
                return await self.send({"type": "error", "msg": "Can't calibrate during a test"})
            await asyncio.sleep(0.6 / self.speed)
            self.calibrated = True
            await self.send({"type": "calibrated", "ok": True, "gain": 6.6e-10})
            await self.send(self.info())
        elif kind == "start":
            await self.start(m)
        elif kind == "stop" and self.running and m.get("test_id") == self.test_id:
            await self.finish("stopped")
        elif kind == "sync" and m.get("test_id") == self.test_id:
            for r in list(self.sent):
                await self.send(r)
            if not self.running and self.sent:
                await self.send({"type": "done", "test_id": self.test_id, "n": len(self.sent), "reason": "complete"})
        elif kind == "ping":
            await self.send({"type": "pong"})

    async def start(self, m):
        if self.running:
            return await self.send({"type": "error", "msg": "Reader is busy with another test"})
        if not self.calibrated:
            return await self.send({"type": "error", "msg": "Reader is not calibrated."})
        dur, iv = float(m.get("duration_s", 120)), float(m.get("interval_s", 2))
        self.test_id, self.sent, self.running = m.get("test_id"), [], True
        await self.send({"type": "ack", "cmd": "start", "test_id": self.test_id, "short_id": m.get("short_id")})
        group = self.group or random.choice(GROUPS)
        print(f"{m.get('short_id')}: simulating {group} for {dur:.0f} s")
        self.task = asyncio.create_task(self.sweep(group, dur, iv))

    async def sweep(self, group, dur, iv):
        react = ["A" in group, "B" in group, group.endswith("+")]
        base = [random.uniform(430, 640), random.uniform(480, 700), random.uniform(540, 760)]
        t0 = time.monotonic()
        for i in range(int(dur // iv) + 1):
            if not self.running:
                return
            t = i * iv
            curve = 1 / (1 + math.exp(-(t - 35) / 9))           # clumping builds up after ~20 s
            z = [round(base[k] * (1 + ((13 if k < 2 else 12) * curve * react[k] + random.gauss(0, 0.5)) / 100), 2)
                 for k in range(3)]
            re_ = [round(1e7 / (v + 10140), 1) for v in z]
            msg = {"type": "reading", "test_id": self.test_id, "i": i, "t_ms": int(t * 1000), "z": z,
                   "re": re_, "im": [round(-x * 0.05, 1) for x in re_]}
            self.sent.append(msg)
            await self.send(msg)
            delay = t0 + (i + 1) * iv / self.speed - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
        await self.finish("complete")

    async def finish(self, reason):
        if self.running:
            self.running = False
            await self.send({"type": "done", "test_id": self.test_id, "n": len(self.sent), "reason": reason})
            print(f"Test {reason}: {len(self.sent)} readings")


async def main():
    import websockets

    ap = argparse.ArgumentParser(description="Pretend HemoSense reader")
    ap.add_argument("--server", default="ws://localhost:8000")
    ap.add_argument("--key", default="dev-device-key")
    ap.add_argument("--id", default="HS-VIRTUAL-01")
    ap.add_argument("--group", choices=GROUPS, help="blood group to simulate (default: random)")
    ap.add_argument("--speed", type=float, default=1.0, help="4 = four times faster than real time")
    a = ap.parse_args()
    reader = FakeReader(a.id, a.group, a.speed)
    url = f"{a.server.rstrip('/')}/ws/reader?device_id={a.id}&key={a.key}"
    while True:
        try:
            async with websockets.connect(url, ping_interval=20) as ws:
                async def send(msg):
                    try:
                        await ws.send(json.dumps(msg))
                    except Exception:
                        pass
                reader.send = send
                print(f"Connected to {a.server} as {a.id}")
                await send(reader.info())
                async for text in ws:
                    m = json.loads(text)
                    if m.get("type") == "error":
                        print("Server:", m.get("msg"))
                    else:
                        asyncio.create_task(reader.handle(m))
        except (OSError, asyncio.TimeoutError, websockets.exceptions.WebSocketException) as e:
            print(f"Not connected ({e}). Retrying in 3 s...")
        await asyncio.sleep(3)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
